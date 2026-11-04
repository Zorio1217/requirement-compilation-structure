"""An incomplete backend must not prevent every page from being designed."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from infrastructure.pipeline.optimized_design import ui_and_triggers, consume_triggers
from infrastructure.shared.agent.optimization import optimized_run
from infrastructure.shared.agent.tasks import StageIncomplete
from infrastructure.utils.files import read_artifact, save_json


class PartialBackendUI(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / 'input'
        self.source.mkdir()
        self.arc = Path(temporary.name) / 'output/.arc'
        raw = {'id': 'ROOT', 'children': [{'id': rid, 'description': 'Display a page',
            'scenarios': [{'id': 'SCN-' + rid, 'steps': [{'id': 'STEP-' + rid}]}]}
            for rid in ('REQ-3.1', 'GOOD')]}
        self.backend = {'apis': {}, 'requirements': {'ROOT': [], 'GOOD': []}}
        effects = {'requirements': {'ROOT': {'read': [], 'write': []},
            'REQ-3.1': {'read': ['Note.id'], 'write': []}, 'GOOD': {'read': [], 'write': []}},
            'steps': {'STEP-REQ-3.1': {'read': ['Note.id'], 'write': []},
                'STEP-GOOD': {'read': [], 'write': []}}}
        for name, value in [('requirements.json', raw), ('visual_analysis.json', {}),
                ('db_schema.json', {'entities': {'Note': {'fields': {'id': {}}}}}),
                ('backend_apis.json', self.backend), ('db_obligations.json', effects)]:
            save_json(self.arc / name, value)

    def ui_proposal(self, _, request):
        rid = request['requirement']['id']
        return {'pages': {'home': {'name': 'Home', 'route': '/', 'root': 'UI-home', 'images': []}},
            'elements': {'UI-home': {'type': 'page', 'parent': None, 'data': [], 'mode': [], 'prop': {}}},
            'requirement_ui': ['UI-home'], 'step_ui': {'STEP-' + rid: ['UI-home']}}

    def test_missing_req_mapping_creates_ui_and_later_valid_triggers(self):
        save_json(self.arc / 'joint_frontend.json', {'digest': 'stale'})
        save_json(self.arc / 'frontend_triggers.json', {'triggers': {'STALE': {}}})
        model = Mock(generate_json=Mock(side_effect=self.ui_proposal))
        with redirect_stdout(StringIO()), optimized_run(True), \
                patch('infrastructure.pipeline.optimized_design.TextModel') as joint, \
                patch('infrastructure.pipeline.ui_design.TextModel', return_value=model):
            ui_and_triggers(self.source, self.arc)
        joint.assert_not_called()
        self.assertEqual(model.generate_json.call_count, 2)
        ui = read_artifact(self.arc, 'ui_design.json')
        self.assertEqual(ui['requirements']['REQ-3.1'], ['UI-home'])
        self.assertEqual(ui['requirements']['GOOD'], ['UI-home'])
        self.assertFalse((self.arc / 'joint_frontend.json').exists())
        self.assertFalse((self.arc / 'frontend_triggers.json').exists())
        triggers = Mock(generate_json=Mock(return_value={'triggers': {}, 'requirement_triggers': [],
            'step_triggers': {'STEP-GOOD': []}}))
        with redirect_stdout(StringIO()), optimized_run(True), \
                patch('infrastructure.pipeline.frontend_triggers.TextModel', return_value=triggers):
            with self.assertRaises(StageIncomplete) as caught:
                consume_triggers(self.source, self.arc)
        self.assertEqual(caught.exception.failures[0]['task'], 'REQ-3.1')
        self.assertEqual(triggers.generate_json.call_count, 1)
        self.assertEqual(triggers.generate_json.call_args.args[1]['requirement']['id'], 'GOOD')
        self.assertTrue((self.arc / 'frontend_triggers.json').is_file())

    def test_insufficient_backend_effects_also_fall_back_to_ui(self):
        self.backend['requirements']['REQ-3.1'] = []
        save_json(self.arc / 'backend_apis.json', self.backend)
        model = Mock(generate_json=Mock(side_effect=self.ui_proposal))
        with redirect_stdout(StringIO()), optimized_run(True), \
                patch('infrastructure.pipeline.ui_design.TextModel', return_value=model):
            ui_and_triggers(self.source, self.arc)
        self.assertIn('home', read_artifact(self.arc, 'ui_design.json')['pages'])
        self.assertFalse((self.arc / 'joint_frontend.json').exists())

    def test_failed_independent_ui_is_not_reported_as_success(self):
        def propose(instructions, request):
            return {} if request['requirement']['id'] == 'REQ-3.1' else self.ui_proposal(instructions, request)
        model = Mock(generate_json=Mock(side_effect=propose))
        with redirect_stdout(StringIO()), optimized_run(True, max_calls=1), \
                patch('infrastructure.pipeline.ui_design.TextModel', return_value=model):
            with self.assertRaises(StageIncomplete):
                ui_and_triggers(self.source, self.arc)
        self.assertEqual(read_artifact(self.arc, 'ui_design.json')['requirements']['GOOD'], ['UI-home'])

    def test_filesystem_errors_are_not_hidden_by_ui_fallback(self):
        with patch('infrastructure.stages.backend.primitives.validate_design', side_effect=OSError('disk error')), \
                patch('infrastructure.pipeline.ui_design.design_frontend_ui') as fallback:
            with self.assertRaises(OSError):
                ui_and_triggers(self.source, self.arc)
        fallback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
