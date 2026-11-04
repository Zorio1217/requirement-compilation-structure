"""Bound initial source excerpts while keeping exact reads available and permissions intact."""
from infrastructure.utils.files import read_source


def task_files(files, max_characters=36000, per_file=10000):
    # Build a bounded initial packet instead of loading all pinned source text first.
    result, remaining = {}, max_characters
    for path in sorted(files.files):
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
            for line in lines:
                if size + len(line) > limit: break
                selected.append(line)
                size += len(line)
            result[path] = {'content': ''.join(selected), 'writable': writable,
                'excerpt': True, 'start_line': 1, 'end_line': len(selected),
                'total_lines': len(lines), 'instruction': 'Partial source; batch read_file ranges for missing evidence before editing.'}
            remaining -= size
    return result
