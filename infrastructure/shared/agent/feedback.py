"""Keep real failure evidence without carrying successful regression output."""


class CheckFailure(ValueError):
    def __init__(self, message, report):
        self.feedback = report
        super().__init__(message + ': ' + str(report))


def compact_feedback(item):
    if isinstance(item, dict):
        failed = any(item.get(key) for key in ('error', 'errors', 'failures', 'failed', 'fail', 'skipped', 'flaky', 'todo'))
        if item.get('returncode') == 0 and not failed and item.get('status') not in {'FAILED', 'INTERRUPTED'}:
            return {'returncode': 0}
        return {key: compact_feedback(child) for key, child in item.items()
            if not (key in {'output', 'stdout', 'files'} and item.get('failures'))}
    if isinstance(item, list):
        return [compact_feedback(child) for child in item]
    return item
