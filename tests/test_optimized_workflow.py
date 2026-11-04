"""Behavioral regressions for opt-in joint designs, bounded tools and real SQL lowering."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from infrastructure.shared.agent.optimization import optimized_run, policy
from infrastructure.shared.agent.planning import feature_plan
from infrastructure.shared.agent.checks import current_first
from infrastructure.shared.agent.packet import task_files
from infrastructure.shared.agent.tools import FileTools
from infrastructure.shared.agent.loop import run_file_agent, AgentExhausted
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.shared.agent.discovery import ContextIndex
from infrastructure.stages.database.lowering import lower_table
from infrastructure.utils.files import save_json, save_text, read_artifact
from infrastructure.pipeline.optimized_design import (schema_and_obligations, consume_obligations,
    ui_and_triggers, consume_triggers)
from infrastructure.shared.project.initialize import initialize_project


def field(kind='string', primary=False, **values):
    return {**dict(description='value', type=kind, nullable=False, primary_key=primary,
        unique=False, references=None, constraints=[]), **values}


def requirements():
    return {'id': 'ROOT', 'children': [
        {'id': rid, 'name': rid, 'description': {'text': 'Display saved notes',
         'image': [{'id': 'IMG', 'reference': 'page.png', 'page': 'notes'}]},
         'scenarios': [{'id': 'SCN-'+rid, 'steps': [{'id': 'STEP-'+rid,
            'Given': 'A saved note', 'When': 'Open notes', 'Then': 'Show the note'}]}]}
        for rid in ('A', 'B')]}


class OptimizedWorkflow(unittest.TestCase):
    def test_scope_restores_policy_even_after_failure(self):
        self.assertFalse(policy().enabled)
        with self.assertRaises(RuntimeError):
            with optimized_run(True, max_calls=1):
                self.assertEqual(policy().max_calls, 1)
                raise RuntimeError('test')
        self.assertFalse(policy().enabled)

    def test_feature_groups_preserve_ids_order_and_bound(self):
        raw = requirements()
        self.assertEqual(feature_plan(raw)['groups'][0]['requirements'], ['A','B'])
        self.assertEqual(len(feature_plan(raw, 1)['groups']), 2)
        raw['children'][1]['description']['image'][0]['page'] = 'other'
        self.assertEqual(len(feature_plan(raw)['groups']), 2)

    def test_current_failure_skips_unrelated_regression(self):
        run = Mock(return_value={'returncode': 1, 'failed': 1})
        self.assertEqual(current_first(run, ['new'], ['old','new'])['failed'], 1)
        run.assert_called_once_with(['new'])

    def test_current_pass_still_requires_all_regressions(self):
        run = Mock(side_effect=[{'returncode':0,'passed':1}, {'returncode':1,'failed':1}])
        self.assertEqual(current_first(run, ['new'], ['old','new'])['returncode'], 1)
        self.assertEqual([c.args[0] for c in run.call_args_list], [['new'],['old']])

    def test_successful_regression_counts_are_aggregated(self):
        run=Mock(side_effect=[{'returncode':0,'passed':1,'tests':1},
                             {'returncode':0,'passed':2,'tests':2}])
        result=current_first(run,['new'],['old','new'])
        self.assertEqual((result['passed'],result['tests']),(3,3))

    def test_ddl_checks_real_sqlite_constraints_without_model(self):
        entity={'fields':{'id':field('integer',True), 'name':field(unique=True),
                          'enabled':field('boolean'), 'metadata':field('json')}}
        sql=lower_table('Note',entity)
        self.assertIsNotNone(sql)
        with sqlite3.connect(':memory:') as db:
            db.executescript(sql)
            db.execute('INSERT INTO Note(name,enabled,metadata) VALUES(?,?,?)',('n',1,'{}'))
            for values in [('n',0,'{}'), ('b',2,'{}'), ('c',1,'bad'), (None,1,'{}')]:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('INSERT INTO Note(name,enabled,metadata) VALUES(?,?,?)',values)

    def test_prose_constraints_are_not_silently_dropped(self):
        entity={'fields':{'name':field()}}
        entity['fields']['name']['constraints']=['At least three characters']
        self.assertIsNone(lower_table('Note',entity))

    def test_foreign_keys_preserve_declared_relationship(self):
        entity={'fields':{'parent_id':field(references='Parent.id')}}
        sql=lower_table('Child',entity)
        with sqlite3.connect(':memory:') as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('CREATE TABLE Parent(id TEXT PRIMARY KEY)')
            db.executescript(sql)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO Child VALUES ('missing')")

    def test_packet_has_explicit_excerpts_and_preserves_permissions(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);save_text(root/'large.ts','const value = 1;\n'*1000)
            files=FileTools(root,references=['large.ts'])
            packet=task_files(files,max_characters=100,per_file=100)
            self.assertTrue(packet['large.ts']['excerpt'])
            self.assertFalse(packet['large.ts']['writable'])
            self.assertLessEqual(len(packet['large.ts']['content']),100)
            self.assertEqual(packet['large.ts']['total_lines'],1000)

    def test_model_call_budget_stops_reads_without_running_gate(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);save_text(root/'a.ts','hello\n')
            model=Mock(generate_json=Mock(return_value={'tool_calls':[{'tool':'read_file','path':'a.ts'}]}))
            gate=Mock()
            with optimized_run(True,max_calls=1), self.assertRaises(AgentExhausted):
                run_file_agent(model,'Read',{},FileTools(root,references=['a.ts']),gate,10,Mock(),'budget')
            self.assertEqual(model.generate_json.call_count,1)
            gate.assert_not_called()

    def test_read_budget_counts_discovery_and_reads_across_edit_rounds(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);save_text(root/'a.ts','hello\n')
            model=Mock(generate_json=Mock(return_value={'tool_calls':[{'tool':'read_file','path':'a.ts'}]}))
            with optimized_run(True,max_calls=20,max_reads=1), self.assertRaises(AgentExhausted):
                run_file_agent(model,'Read',{},FileTools(root,references=['a.ts']),Mock(),10,Mock(),'budget')
            self.assertEqual(model.generate_json.call_count,2)

    def test_design_discovery_also_has_call_budget(self):
        model=Mock(generate_json=Mock(return_value={'context_request':{'kind':'schema','ids':['Note']}}))
        with optimized_run(True,max_calls=1), self.assertRaisesRegex(RuntimeError,'budget exhausted'):
            propose_json(model,'design',{},lambda x:x,Mock(),'budget',
                context_index=ContextIndex({'schema':{'Note':{'description':'notes'}}}))
        self.assertEqual(model.generate_json.call_count,1)

    def test_file_edits_still_need_real_gate_and_refresh_current_source(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);save_text(root/'a.ts','const value = 1;\n')
            model=Mock(generate_json=Mock(side_effect=[
                {'tool_calls':[{'tool':'edit_file','path':'a.ts','operation':'replace','old_content':'1','new_content':'2'}]},
                {'tool_calls':[{'tool':'edit_file','path':'a.ts','operation':'replace','old_content':'2','new_content':'3'}]}]))
            def gate():
                if '3' not in (root/'a.ts').read_text(): raise ValueError('Expected value 3')
                return {'green':True}
            with optimized_run(True):
                result=run_file_agent(model,'Implement',{},FileTools(root,writable=['a.ts']),gate,3,Mock(),'repair')
            self.assertEqual(result['turns'],2)
            self.assertIn('2',model.generate_json.call_args_list[1].args[1]['files']['a.ts']['content'])
            metrics=json.loads((root/'.arc/agent_metrics.jsonl').read_text())
            self.assertEqual(metrics['model_calls'],2)

    def test_grouped_schema_and_effects_one_call_and_stale_pair_rejected(self):
        with TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            arc=Path(tmp);raw=requirements();save_json(arc/'requirements.json',raw)
            proposal={'entities':{'Note':{'description':'note','fields':{'id':field('integer',True)}}},
                'requirements':{rid:{'related_fields':['Note.id'],'obligations':{
                    'requirement':{'read':['Note.id'],'write':[]},
                    'steps':{'STEP-'+rid:{'read':['Note.id'],'write':[]}}}} for rid in ('A','B')}}
            model=Mock(generate_json=Mock(return_value=proposal))
            with optimized_run(True),patch('infrastructure.pipeline.optimized_design.TextModel',return_value=model):
                schema_and_obligations(arc)
                result=consume_obligations(arc)
            self.assertEqual(model.generate_json.call_count,1)
            self.assertEqual(result['requirements']['B']['read'],['Note.id'])
            schema=read_artifact(arc,'db_schema.json');schema['entities']['Other']={'fields':{}}
            save_json(arc/'db_schema.json',schema)
            with self.assertRaisesRegex(ValueError,'inputs changed'):consume_obligations(arc)

    def test_joint_design_rejects_missing_step_instead_of_publishing_partial_pair(self):
        with TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            arc=Path(tmp);save_json(arc/'requirements.json',requirements())
            broken={'entities':{},'requirements':{rid:{'related_fields':[],
                'obligations':{'requirement':{'read':[],'write':[]},'steps':{}}} for rid in ('A','B')}}
            model=Mock(generate_json=Mock(return_value=broken))
            with optimized_run(True,max_calls=1),patch('infrastructure.pipeline.optimized_design.TextModel',return_value=model):
                with self.assertRaises(RuntimeError):schema_and_obligations(arc)
            self.assertFalse((arc/'db_schema.json').exists())

    def test_joint_frontend_is_reused_only_for_matching_contracts(self):
        with TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            root=Path(tmp);source=root/'input';source.mkdir();(source/'page.png').write_bytes(b'image')
            arc=root/'output/.arc';raw=requirements();save_json(arc/'requirements.json',raw)
            save_json(arc/'visual_analysis.json',{'page.png':{'layout':'notes'}})
            schema={'entities':{},'requirements':{'A':[],'B':[]}}
            backend={'apis':{},'requirements':{'ROOT':[],'A':[],'B':[]}}
            effects={'requirements':{rid:{'read':[],'write':[]} for rid in ('ROOT','A','B')},
                'steps':{'STEP-'+rid:{'read':[],'write':[]} for rid in ('A','B')}}
            for name,value in [('db_schema.json',schema),('backend_apis.json',backend),('db_obligations.json',effects)]:save_json(arc/name,value)
            proposal={'pages':{'notes':{'name':'Notes','route':'/','root':'UI-root','images':['IMG']}},
                'elements':{'UI-root':{'type':'page','parent':None,'data':[],'mode':[],'prop':{}}},
                'triggers':{},'requirements':{rid:{'requirement_ui':['UI-root'],
                    'step_ui':{'STEP-'+rid:['UI-root']},'requirement_triggers':[],
                    'step_triggers':{'STEP-'+rid:[]}} for rid in ('A','B')}}
            model=Mock(generate_json=Mock(return_value=proposal))
            with optimized_run(True),patch('infrastructure.pipeline.optimized_design.TextModel',return_value=model):
                ui_and_triggers(source,arc)
                result=consume_triggers(source,arc)
            self.assertEqual(model.generate_json.call_count,1)
            self.assertEqual(result['requirements']['B'],[])
            save_json(arc/'shared_conventions.json',{'new':{'description':'changed'}})
            with self.assertRaisesRegex(ValueError,'inputs changed'):consume_triggers(source,arc)

    def test_cli_flag_preserves_original_required_arguments(self):
        import main
        with TemporaryDirectory() as tmp:
            root=Path(tmp);save_text(root/'input/requirements.yaml','id: ROOT\n')
            args=main.parse_args([str(root/'input'),'--output-dir',str(root/'output'),'--optimize','--agent-max-calls','8'])
            self.assertTrue(args.optimize);self.assertEqual(args.agent_max_calls,8)

    def test_compiler_dispatch_uses_joint_stages_and_leaves_original_default(self):
        from infrastructure.compiler import Compiler
        from infrastructure.pipeline.optimized_design import optimized_stage
        original = Mock(return_value={'old':True})
        self.assertIs(optimized_stage('06_implement_backend_tdd',original),original)
        with TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            compiler=Compiler(Path(tmp)/'input',Path(tmp)/'output')
            with patch('infrastructure.compiler.console_log'), patch('infrastructure.compiler.checkpoint'):
                compiler._run('02_build_db_schema',original,compiler.arc_dir)
                self.assertEqual(original.call_count,1)
                with optimized_run(True),patch('infrastructure.pipeline.optimized_design.schema_and_obligations',return_value={'joint':True}) as joint:
                    result=compiler._run('02_build_db_schema',original,compiler.arc_dir)
            self.assertEqual(result,{'joint':True})
            joint.assert_called_once_with(compiler.arc_dir)

    def test_three_identical_failures_stop_unproductive_repairs(self):
        with TemporaryDirectory() as tmp:
            files=FileTools(Path(tmp))
            model=Mock(generate_json=Mock(return_value={'tool_calls':[]}))
            gate=Mock(side_effect=ValueError('unchanged failure'))
            with optimized_run(True),self.assertRaises(AgentExhausted):
                run_file_agent(model,'Implement',{},files,gate,10,Mock(),'stalled')
            self.assertEqual(model.generate_json.call_count,3)
            self.assertIn('repair_focus',model.generate_json.call_args_list[2].args[1])
            metrics=json.loads((Path(tmp)/'.arc/agent_metrics.jsonl').read_text())
            self.assertEqual(metrics['status'],'STALLED')

    def test_truncated_pinned_file_retains_explicit_read_evidence(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);save_text(root/'a.ts','const prefix = 0;\n'*1000+'const tail = 1;\n')
            model=Mock(generate_json=Mock(side_effect=[
                {'tool_calls':[{'tool':'read_file','path':'a.ts','start_line':1001,'end_line':1001}]},
                {'tool_calls':[]}]))
            with optimized_run(True):
                run_file_agent(model,'Inspect',{},FileTools(root,references=['a.ts']),Mock(return_value={}),1,Mock(),'read')
            request=model.generate_json.call_args_list[1].args[1]
            self.assertTrue(request['files']['a.ts']['excerpt'])
            self.assertEqual(request['read_files']['a.ts'][0]['content'],'const tail = 1;\n')

    def test_original_unbounded_read_behavior_is_unchanged_without_flag(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            for i in range(7): save_text(root/f'{i}.ts',str(i))
            replies=[{'tool_calls':[{'tool':'read_file','path':f'{i}.ts'}]} for i in range(7)]
            model=Mock(generate_json=Mock(side_effect=[*replies,{'tool_calls':[]}]))
            result=run_file_agent(model,'Read',{},FileTools(root,references=[f'{i}.ts' for i in range(7)]),
                Mock(return_value={}),1,Mock(),'baseline')
            self.assertEqual(result['turns'],1)
            self.assertEqual(model.generate_json.call_count,8)

if __name__=='__main__':unittest.main()
