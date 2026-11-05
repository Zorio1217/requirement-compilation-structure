"""Recover gateway failures after SDK retries without entering model-response repair."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import APIStatusError
from infrastructure.shared.agent.discovery import ContextIndex
from infrastructure.shared.agent.optimization import optimized_run
from infrastructure.shared.agent.proposals import propose_json


def gateway_failure(status=503):
    response = httpx.Response(status, request=httpx.Request('POST', 'https://test.invalid/chat/completions'))
    cause = APIStatusError('gateway unavailable', response=response, body={'error': 'gateway unavailable'})
    error = RuntimeError(f'Text model request failed: Error code: {status}')
    error.__cause__ = cause
    return error


class DesignServiceRecovery(unittest.TestCase):
    def test_502_then_503_recovers_with_identical_request_and_no_repair_feedback(self):
        requests = []
        replies = iter([gateway_failure(502), gateway_failure(503), {'apis': {}}])
        def generate(prompt, request):
            requests.append((prompt, deepcopy(request)))
            reply = next(replies)
            if isinstance(reply, Exception): raise reply
            return reply
        accept = Mock(side_effect=lambda value: value['apis'])
        with patch('time.sleep') as sleep:
            result, _ = propose_json(Mock(generate_json=generate), 'Design', {'requirement': 'REQ-3.1'},
                accept, Mock(), 'REQ-3.1', max_attempts=1)
        self.assertEqual(result, {})
        self.assertEqual(requests, [requests[0]] * 3)
        self.assertNotIn('correction', requests[0][1])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10])
        accept.assert_called_once()

    def test_gateway_failure_during_discovery_keeps_resolved_context(self):
        requests = []
        replies = iter([{'context_request': {'kind': 'schema', 'ids': ['Note']}},
            gateway_failure(), {'apis': {}}])
        def generate(prompt, request):
            requests.append(deepcopy(request))
            reply = next(replies)
            if isinstance(reply, Exception): raise reply
            return reply
        index = ContextIndex(lambda: {'schema': {'Note': {'description': 'notes', 'fields': {}}}})
        with patch('time.sleep'):
            propose_json(Mock(generate_json=generate), 'Design', {}, lambda value: value['apis'],
                Mock(), 'REQ-3.1', max_attempts=1, context_index=index)
        self.assertEqual(len(requests), 3)
        self.assertIn('context_result', requests[1])
        self.assertEqual(requests[1], requests[2])
        self.assertNotIn('correction', requests[2])

    def test_persistent_gateway_failure_stops_and_never_calls_acceptance(self):
        failure = gateway_failure()
        model = Mock(generate_json=Mock(side_effect=failure))
        accept = Mock()
        with patch('time.sleep') as sleep, self.assertRaises(RuntimeError) as caught:
            propose_json(model, 'Design', {}, accept, Mock(), 'REQ-3.1')
        self.assertIs(caught.exception, failure)
        self.assertEqual(model.generate_json.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        accept.assert_not_called()

    def test_other_http_errors_and_plain_runtime_errors_are_not_retried(self):
        for failure in [*(gateway_failure(status) for status in (400, 401, 403, 429)), RuntimeError('compiler bug')]:
            model = Mock(generate_json=Mock(side_effect=failure))
            with self.subTest(error=str(failure)), patch('time.sleep') as sleep, self.assertRaises(RuntimeError):
                propose_json(model, 'Design', {}, lambda value: value, Mock(), 'REQ-3.1')
            model.generate_json.assert_called_once()
            sleep.assert_not_called()

    def test_retries_respect_optimized_call_budget_without_sleeping_when_exhausted(self):
        model = Mock(generate_json=Mock(side_effect=gateway_failure()))
        with optimized_run(True, max_calls=1), patch('time.sleep') as sleep, self.assertRaises(RuntimeError):
            propose_json(model, 'Design', {}, lambda value: value, Mock(), 'REQ-3.1')
        model.generate_json.assert_called_once()
        sleep.assert_not_called()

    def test_retry_limit_is_shared_across_discovery_and_repair_rounds(self):
        replies = [gateway_failure(), {'context_request': {'kind': 'schema', 'ids': ['Note']}},
            gateway_failure(), {}, gateway_failure()]
        model = Mock(generate_json=Mock(side_effect=replies))
        index = ContextIndex(lambda: {'schema': {'Note': {'fields': {}}}})
        with patch('time.sleep') as sleep, self.assertRaises(RuntimeError):
            propose_json(model, 'Design', {}, lambda value: value['apis'], Mock(), 'REQ-3.1',
                max_attempts=2, context_index=index)
        self.assertEqual(model.generate_json.call_count, 5)
        self.assertEqual(sleep.call_count, 2)

    def test_backend_stage_publishes_mapping_after_service_recovers(self):
        from infrastructure.pipeline import backend_apis as stage
        from infrastructure.utils.files import read_artifact
        from test_backend_contracts import prepare_artifacts, conditions
        with TemporaryDirectory() as tmp:
            arc, _, design = prepare_artifacts(Path(tmp))
            attempts = []
            def generate(_, request):
                rid = request['requirement']['id']
                attempts.append(rid)
                if len(attempts) == 1: raise gateway_failure()
                ids = design['requirements'][rid]
                return {'apis': {aid: {**design['apis'][aid], **conditions(aid)} for aid in ids},
                    'requirement_apis': ids}
            with patch.object(stage, 'TextModel', return_value=Mock(generate_json=generate)), \
                    patch.object(stage, 'DemoConsole'), patch('time.sleep'):
                stage.design_backend_apis(arc, max_attempts=1)
            self.assertEqual(attempts, ['REQ-A', 'REQ-A', 'REQ-B', 'REQ-C'])
            self.assertEqual(read_artifact(arc, 'backend_apis.json')['requirements']['REQ-A'], ['API-SAVE'])


if __name__ == '__main__':
    unittest.main()
