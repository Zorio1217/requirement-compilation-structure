"""Replay tool/fixture failures observed in submission3 without calling a model."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import json
import os
import sqlite3
import subprocess
import unittest

from infrastructure.shared.agent.discovery import ContextIndex
from infrastructure.shared.agent.loop import AgentExhausted, run_file_agent
from infrastructure.shared.agent.optimization import optimized_run
from infrastructure.shared.agent.tools import FileTools, FileToolError
from infrastructure.stages.backend.context import supplied_sources
from infrastructure.stages.backend.runtime import prepare_runtime
from infrastructure.stages.backend.testing import BackendChecks
from infrastructure.shared.project.initialize import initialize_project
from infrastructure.utils.files import save_text


class GenerationLogRegressions(unittest.TestCase):
    def test_wrapped_tool_arguments_read_then_edit_through_the_normal_gate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_text(root/'source.ts', 'const value = 1;\nconst retained = 0;\n')
            model = Mock(generate_json=Mock(side_effect=[
                {'tool_calls': [{'tool': 'read_file', 'arguments': {'path': 'source.ts'}}]},
                {'tool_calls': [{'tool': 'edit_file', 'parameters': {'path': 'source.ts',
                    'operation': 'replace', 'old_content': 'const value = 1;', 'new_content': 'const value = 2;'}}]}]))
            def gate():
                self.assertEqual((root/'source.ts').read_text(), 'const value = 2;\nconst retained = 0;\n')
                return {'checked': True}
            with optimized_run(True):
                result = run_file_agent(model, 'Implement', {}, FileTools(root, writable=['source.ts']),
                    gate, 1, Mock(), 'wrapped calls')
            self.assertTrue(result['checks']['checked'])
            self.assertEqual(model.generate_json.call_count, 2)

    def test_wrapped_batch_cannot_bypass_readonly_files_or_leave_partial_writes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in ('editable.ts', 'readonly.ts'): save_text(root/path, 'const value = 1;\nconst other = 0;\n')
            tools = FileTools(root, writable=['editable.ts'], references=['readonly.ts'])
            calls = [{'tool': 'edit_file', 'arguments': {'path': path, 'operation': 'replace',
                'old_content': 'const value = 1;', 'new_content': 'const value = 2;'}} for path in ('editable.ts', 'readonly.ts')]
            with self.assertRaises(FileToolError): tools.execute(calls)
            self.assertIn('value = 1', (root/'editable.ts').read_text())
            self.assertIn('value = 1', (root/'readonly.ts').read_text())

    def test_ambiguous_wrappers_and_invalid_tool_names_are_still_rejected(self):
        with TemporaryDirectory() as tmp:
            tools = FileTools(Path(tmp))
            for call in [
                {'tool': 'read_file', 'path': 'one.ts', 'arguments': {'path': 'two.ts'}},
                {'tool': ['read_file'], 'arguments': {'path': 'one.ts'}},
                {'tool': 'tool', 'arguments': {'path': 'one.ts'}},
                {'tool': 'read_file', 'arguments': {'tool': 'edit_file', 'path': 'one.ts'}}]:
                with self.subTest(call=call), self.assertRaises(FileToolError): tools.effect([call])

    def test_exact_file_discovery_ids_become_permission_checked_reads(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); save_text(root/'seed.sql', "INSERT INTO Note VALUES (1, 'existing');\n")
            model = Mock(generate_json=Mock(side_effect=[
                {'context_request': {'kind': 'file', 'ids': ['seed.sql']}}, {'tool_calls': []}]))
            with optimized_run(True):
                run_file_agent(model, 'Inspect', {}, FileTools(root, readable=['seed.sql']),
                    Mock(return_value={}), 1, Mock(), 'file discovery', context_index=ContextIndex(lambda: {'file': {}}))
            request = model.generate_json.call_args_list[1].args[1]
            self.assertIn('existing', request['read_files']['seed.sql'][0]['content'])
            self.assertFalse(request['read_files']['seed.sql'][0]['writable'])

    def test_file_discovery_ids_cannot_expand_read_permissions_or_escape_workspace(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); save_text(root/'private.txt', 'DO_NOT_EXPOSE')
            for path in ('private.txt', '../outside.txt'):
                model = Mock(generate_json=Mock(return_value={'context_request': {'kind': 'file', 'ids': [path]}}))
                gate = Mock()
                with self.subTest(path=path), optimized_run(True), self.assertRaises(AgentExhausted):
                    run_file_agent(model, 'Inspect', {}, FileTools(root, readable=[]), gate, 1, Mock(),
                        'protected read', context_index=ContextIndex(lambda: {'file': {}}))
                gate.assert_not_called()
                self.assertNotIn('DO_NOT_EXPOSE', json.dumps(model.generate_json.call_args.args[1]))

    def test_distinct_missing_paths_do_not_trigger_the_identical_error_stall(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            replies = [{'tool_calls': [{'tool': 'read_file', 'path': f'missing-{i}.ts'}]} for i in range(3)]
            model = Mock(generate_json=Mock(side_effect=[*replies, {'tool_calls': []}]))
            gate = Mock(return_value={'checked': True})
            with optimized_run(True):
                result = run_file_agent(model, 'Inspect', {}, FileTools(root), gate, 4, Mock(), 'distinct paths')
            self.assertEqual(result['turns'], 4)
            gate.assert_called_once()

    def test_budget_exhaustion_reports_the_actual_reason_in_the_visible_error(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); save_text(root/'source.ts', 'value\n')
            model = Mock(generate_json=Mock(return_value={'tool_calls': [{'tool': 'read_file', 'path': 'source.ts'}]}))
            with optimized_run(True, max_calls=1), self.assertRaisesRegex(AgentExhausted, 'Model call budget exhausted'):
                run_file_agent(model, 'Inspect', {}, FileTools(root, references=['source.ts']),
                    Mock(), 10, Mock(), 'budget')

    def test_backend_context_includes_related_seed_data_readonly(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); initialize_project(root)
            table = 'CREATE TABLE Account (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL);\n'
            seed = "INSERT INTO Account(id,email) VALUES(1,'existing@example.com');\n"
            save_text(root/'backend/src/db/tables/Account.sql', table)
            save_text(root/'backend/src/db/seeds/Account.sql', seed)
            schema = {'entities': {'Account': {'fields': {'id': {'references': None}, 'email': {'references': None}}}}}
            sources = supplied_sources(root, {'read': ['Account.id', 'Account.email'], 'write': []}, schema)
            path = 'backend/src/db/seeds/Account.sql'
            self.assertEqual(sources[path], seed)
            tools = FileTools(root, references=sources)
            self.assertFalse(tools.can_write(path))
            # Replay the logged UNIQUE collision, then reuse the actual seeded identity.
            db = sqlite3.connect(':memory:')
            self.addCleanup(db.close)
            db.executescript(table+seed)
            with self.assertRaises(sqlite3.IntegrityError): db.executescript(seed)
            self.assertEqual(db.execute('SELECT id FROM Account WHERE email=?', ('existing@example.com',)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM Account').fetchone()[0], 1)


class RealSeedFixtureReplay(unittest.TestCase):
    def test_real_server_restores_and_reuses_seeded_account_without_duplicate_inserts(self):
        modules = Path(os.environ.get('ARC_API_TEST_NODE_MODULES', 'node_modules')).resolve()
        if not all((modules/name).exists() for name in ('better-sqlite3', 'tsx', 'typescript', 'express')):
            self.skipTest('Set ARC_API_TEST_NODE_MODULES to installed generated-app dependencies')
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            initialize_project(root)
            if os.name == 'nt':
                subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                    'New-Item -ItemType Junction -Path $env:ARC_FIXTURE_LINK -Target $env:ARC_FIXTURE_MODULES | Out-Null'],
                    env={**os.environ, 'ARC_FIXTURE_LINK': str(root/'node_modules'),
                        'ARC_FIXTURE_MODULES': str(modules)}, check=True, capture_output=True)
            else:
                (root/'node_modules').symlink_to(modules, target_is_directory=True)
            prepare_runtime(root)
            save_text(root/'backend/src/db/tables/Account.sql',
                'CREATE TABLE IF NOT EXISTS Account (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL);\n')
            save_text(root/'backend/src/db/seeds/Account.sql', "INSERT INTO Account VALUES(1,'existing@example.com');\n")
            for relative, variable in [('backend/src/db/initialize.ts', 'tableFiles'), ('backend/src/db/seed.ts', 'seedFiles')]:
                path = root/relative
                save_text(path, path.read_text().replace(f'{variable}: readonly string[] = []',
                    f'{variable}: readonly string[] = ["Account.sql"]'))
            test_path = 'backend/tests/seed-reuse.test.ts'
            save_text(root/test_path, r'''import { test } from "node:test";
import assert from "node:assert/strict";
import { createApiTest, fixturePasswordHash } from "./support/context.js";
test("seeded identity exists and is reused before a real HTTP request", async t => {
  const { database, baseURL } = createApiTest(t);
  assert.throws(() => database.prepare("INSERT INTO Account(email) VALUES(?)").run("existing@example.com"), /UNIQUE constraint failed: Account.email/);
  const account = database.prepare("SELECT id FROM Account WHERE email=?").get("existing@example.com") as { id: number };
  assert.equal(account.id, 1);
  assert.match(fixturePasswordHash("example-password"), /^scrypt\$/);
  const response = await fetch(new URL("/api/health", baseURL));
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: "ok" });
});
test("the next case receives exactly one restored startup account", t => {
  const { database } = createApiTest(t);
  assert.equal((database.prepare("SELECT COUNT(*) AS count FROM Account").get() as { count: number }).count, 1);
});
''')
            checks = BackendChecks(root, timeout=60)
            compiled = checks.typecheck([test_path])
            self.assertEqual(compiled['returncode'], 0, compiled['output'])
            result = checks.tests([test_path])
            self.assertEqual(result['returncode'], 0, result['output'])
            self.assertEqual(result['pass'], 2)


if __name__ == '__main__':
    unittest.main()
