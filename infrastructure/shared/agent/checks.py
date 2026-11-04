"""Run current behavior before accumulated regression; never cache passing results."""

def current_first(run, current, regression):
    current, regression = list(dict.fromkeys(current)), list(dict.fromkeys(regression))
    result = run(current)
    if result.get('returncode') or result.get('failed') or result.get('fail') or result.get('errors') or result.get('skipped') or result.get('flaky'):
        return result
    remaining = [path for path in regression if path not in current]
    if remaining:
        other = run(remaining)
        if other.get('returncode') or other.get('failed') or other.get('fail') or other.get('errors') or other.get('skipped') or other.get('flaky'):
            return other
        result = {**result}
        for key in ('passed', 'failed', 'pass', 'fail', 'tests', 'skipped', 'flaky'):
            if isinstance(result.get(key), (int, float)) and isinstance(other.get(key), (int, float)):
                result[key] += other[key]
    return result
