"""Fixed-budget source selection must improve repair evidence without weakening checks."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from infrastructure.shared.agent.feedback import compact_feedback
from infrastructure.shared.agent.loop import AgentExhausted, run_file_agent
from infrastructure.shared.agent.optimization import optimized_run
from infrastructure.shared.agent.packet import task_files, distinct_reads
from infrastructure.shared.agent.tools import FileTools
from infrastructure.utils.files import save_text


CASES = [
    {'id': 'email', 'function': 'normalizeEmail', 'parameters': 'email',
     'old': 'return email.trim();', 'new': 'return email.trim().toLowerCase();',
     'requirement': 'Normalize login email by trimming whitespace and converting to lowercase.',
     'checks': [[[' BookStack_User@Example.com '], 'bookstack_user@example.com'], [['A@B.COM'], 'a@b.com'], [[''], '']]},
    {'id': 'draft', 'function': 'canDeleteDraft', 'parameters': 'draft, accountId',
     'old': 'return draft.status === "draft";',
     'new': 'return draft.status === "draft" && draft.owner_id === accountId;',
     'requirement': 'A draft is deletable only by its owning account; published pages are not drafts.',
     'checks': [[[{'status': 'draft', 'owner_id': 'a'}, 'a'], True],
                [[{'status': 'draft', 'owner_id': 'b'}, 'a'], False],
                [[{'status': 'published', 'owner_id': 'a'}, 'a'], False]]},
    {'id': 'recent', 'function': 'recentPublishedPages', 'parameters': 'pages',
     'old': 'return pages.filter(page => page.status === "published").slice(0, 5);',
     'new': 'return pages.filter(page => page.status === "published").sort((a, b) => b.updated_at - a.updated_at).slice(0, 5);',
     'requirement': 'Return at most five published pages ordered by updated_at descending.',
     'checks': [[[[]], []], [[[{'id': 1, 'status': 'draft', 'updated_at': 99}]], []],
                [[[{'id': i, 'status': 'published', 'updated_at': i} for i in range(7)]],
                 [{'id': i, 'status': 'published', 'updated_at': i} for i in (6, 5, 4, 3, 2)]]]},
]


def prepare_case(root, case):
    target = f'backend/src/api/z-{case["id"]}.mjs'
    noise = [f'backend/src/api/a-reference-{i}.ts' for i in range(4)]
    for path in noise:
        save_text(root/path, '// Historical reference for unrelated endpoints. Preserve existing behavior.\n' * 300)
    prefix = ''.join(f'// Keep unrelated declaration {i:04d}.\n' for i in range(1200))
    save_text(root/target, prefix + f'export function {case["function"]}({case["parameters"]}) {{\n  {case["old"]}\n}}\n')
    return target, 1202, FileTools(root, writable=[target], references=noise)


def node_gate(root, target, case):
    script = ('import assert from "node:assert/strict"; '
        + f'const module = await import({json.dumps((root/target).as_uri())}); '
        + f'const cases = {json.dumps(case["checks"])}; '
        + f'for (const [args, expected] of cases) assert.deepEqual(module[{json.dumps(case["function"])}](...args), expected); '
        + 'console.log(JSON.stringify({passed: cases.length}));')
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    if result.returncode: raise ValueError(result.stderr)
    return json.loads(result.stdout)


class ContextEfficiency(unittest.TestCase):
    def test_failure_line_is_visible_before_alphabetical_noise_within_the_same_budget(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); case = CASES[0]
            target, line, tools = prepare_case(root, case)
            packet = task_files(tools, max_characters=1000, per_file=1000,
                task={'implementation_path': target}, feedback={'error': f'{target}:{line}: wrong result'})
            self.assertEqual(next(iter(packet)), target)
            self.assertIn(case['old'], packet[target]['content'])
            actual = (root/target).read_text().splitlines(keepends=True)
            view = packet[target]
            self.assertEqual(view['content'], ''.join(actual[view['start_line']-1:view['end_line']]))
            self.assertLessEqual(sum(len(x['content'] or '') for x in packet.values()), 1000)

    def test_fixture_and_seed_sources_precede_other_references(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ['a-unrelated.ts', 'backend/src/db/seeds/Account.sql', 'backend/tests/support/context.ts']
            for path in paths: save_text(root/path, 'evidence\n' * 20)
            tools = FileTools(root, references=paths)
            packet = task_files(tools, max_characters=400, per_file=200,
                task={'support_module': 'backend/tests/support/context.ts'})
            self.assertTrue(packet[paths[1]]['content'])
            self.assertTrue(packet[paths[2]]['content'])
            self.assertFalse(packet[paths[1]]['writable'])

    def test_failure_paths_cannot_expand_the_available_file_scope(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);save_text(root/'allowed.ts','allowed\n');save_text(root/'private.ts','not selected\n')
            packet=task_files(FileTools(root,references=['allowed.ts']),feedback='private.ts:1 failed')
            self.assertEqual(set(packet),{'allowed.ts'})

    def test_windows_error_path_is_normalized_for_the_failing_excerpt(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);target,line,tools=prepare_case(root,CASES[0])
            packet=task_files(tools,feedback={'error':target.replace('/','\\')+f':{line}: failed'})
            self.assertIn(CASES[0]['old'],packet[target]['content'])

    def test_reads_are_deduplicated_only_when_the_exact_current_text_is_visible(self):
        reads={'one.ts':[{'start_line':10,'end_line':10,'content':'ten\n'},
                         {'start_line':20,'end_line':20,'content':'twenty\n'}]}
        state={'one.ts':{'content':'ten\neleven\n','excerpt':True,'start_line':10,'end_line':11}}
        self.assertEqual(distinct_reads(reads,state)['one.ts'],[reads['one.ts'][1]])
        state['one.ts']['content']='different\neleven\n'
        self.assertEqual(distinct_reads(reads,state),reads)

    def test_successful_output_compacts_but_zero_exit_with_failure_evidence_is_kept(self):
        self.assertEqual(compact_feedback({'returncode':0,'output':'passed\n'*1000}),{'returncode':0})
        for key,value in [('error','browser exception'),('errors',['browser exception']),('failures',['wrong value']),('failed',1),('flaky',1),('skipped',1)]:
            report={'returncode':0,key:value,'output':'diagnostic'}
            self.assertEqual(compact_feedback(report)[key],value)

    def test_failure_compaction_preserves_assertion_stack_and_does_not_mutate_the_full_report(self):
        diagnostic='src/page.tsx:183 expected a visible heading; received none'
        report={'typecheck':{'returncode':0,'output':'compiled\n'*1000},
            'tests':{'returncode':1,'failures':[diagnostic],'output':diagnostic*200,'files':{'report':'duplicate'}}}
        original=deepcopy(report)
        compact=compact_feedback(report)
        self.assertIn(diagnostic,json.dumps(compact))
        self.assertLess(len(json.dumps(compact)),len(json.dumps(report))//10)
        self.assertEqual(report,original)

    @unittest.skipUnless(shutil.which('node'),'Node.js required for actual acceptance')
    def test_three_repairs_pass_actual_node_checks_with_one_model_call_each(self):
        for case in CASES:
            with self.subTest(case=case['id']),TemporaryDirectory() as tmp:
                root=Path(tmp);target,line,tools=prepare_case(root,case)
                def generate(_,request):
                    if case['old'] in request['files'][target]['content']:
                        return {'tool_calls':[{'tool':'edit_file','path':target,'operation':'replace',
                            'old_content':case['old'],'new_content':case['new']}]}
                    return {'tool_calls':[{'tool':'read_file','path':target,'start_line':line-1,'end_line':line+1}]}
                model=Mock(generate_json=Mock(side_effect=generate))
                with optimized_run(True,max_calls=1,max_reads=1):
                    result=run_file_agent(model,'Implement',{'implementation_path':target},tools,
                        lambda:node_gate(root,target,case),1,Mock(),case['id'],feedback={'error':f'{target}:{line}: wrong business result'})
                self.assertEqual(result['checks']['passed'],3)
                self.assertEqual(model.generate_json.call_count,1)

    @unittest.skipUnless(shutil.which('node'),'Node.js required for actual acceptance')
    def test_smaller_context_does_not_allow_a_broken_implementation_to_pass(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);target,line,tools=prepare_case(root,CASES[1])
            with optimized_run(True,max_calls=1),self.assertRaises(AgentExhausted):
                run_file_agent(Mock(generate_json=Mock(return_value={'tool_calls':[]})),'Implement',
                    {'implementation_path':target},tools,lambda:node_gate(root,target,CASES[1]),1,Mock(),'no fake green',
                    feedback={'error':f'{target}:{line}: other account must be rejected'})


if __name__=='__main__':unittest.main()
