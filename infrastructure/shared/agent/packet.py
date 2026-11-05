"""Bound initial source excerpts while keeping exact reads available and permissions intact."""
import json
import re
from infrastructure.utils.files import read_source


def task_files(files, max_characters=36000, per_file=10000, *, task=None, feedback=None):
    """Spend the same source budget on failures/targets before unrelated references."""
    task = task or {}
    evidence = json.dumps(feedback, default=str).replace('\\\\', '/') if feedback else ''
    targets = {task.get(key) for key in ('implementation_path', 'test_path') if isinstance(task.get(key), str)}
    if isinstance(task.get('file_target'), dict) and isinstance(task['file_target'].get('path'), str):
        targets.add(task['file_target']['path'])
    for key in ('implementation_paths', 'page_paths'):
        if isinstance(task.get(key), dict):
            targets.update(value for value in task[key].values() if isinstance(value, str))
    def priority(path):
        if path in evidence: return (0, path)
        if path in targets: return (1, path)
        if path == task.get('support_module') or '/db/seeds/' in path: return (2, path)
        return (3 if files.can_write(path) else 4, path)
    result, remaining = {}, max_characters
    for path in sorted(files.files, key=priority):
        target = files.path(path)
        writable = files.can_write(path)
        if not target.is_file():
            result[path] = {'content': None, 'writable': writable}
            continue
        text = read_source(target)
        limit = min(per_file, remaining)
        if len(text) <= limit:
            result[path] = {'content': text, 'writable': writable}
            remaining -= len(text)
        else:
            # Only complete lines: old_content can still be copied exactly.
            lines, selected, size = text.splitlines(keepends=True), [], 0
            location = re.search(re.escape(path) + r':(\d+)(?::\d+)?', evidence)
            line_number = int(location[1]) if location else 0
            start = line_number - 1 if 0 < line_number <= len(lines) else 0
            # Reserve most of a small excerpt for the failing line and following code.
            preceding = 0
            while start > 0 and line_number and start > line_number - 13:
                width = len(lines[start - 1])
                if preceding + width > limit // 3: break
                preceding += width
                start -= 1
            for line in lines[start:]:
                if size + len(line) > limit: break
                selected.append(line)
                size += len(line)
            result[path] = {'content': ''.join(selected), 'writable': writable,
                'excerpt': True, 'start_line': start + 1, 'end_line': start + len(selected),
                'total_lines': len(lines), 'instruction': 'Partial source; batch read_file ranges for missing evidence before editing.'}
            remaining -= size
    return result


def distinct_reads(read_files, source_state):
    """Retain every read not already represented by the current pinned file excerpt."""
    result = {}
    for path, excerpts in read_files.items():
        pinned = source_state.get(path)
        if pinned is None or pinned.get('content') is None:
            result[path] = excerpts
            continue
        if not pinned.get('excerpt'):
            continue
        lines = pinned['content'].splitlines(keepends=True)
        kept = []
        for excerpt in excerpts:
            start, end = excerpt['start_line'], excerpt['end_line']
            covered = pinned['start_line'] <= start <= end <= pinned['end_line']
            if not covered or ''.join(lines[start-pinned['start_line']:end-pinned['start_line']+1]) != excerpt['content']:
                kept.append(excerpt)
        if kept: result[path] = kept
    return result
