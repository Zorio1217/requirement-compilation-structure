"""Bounded feature groups derived from explicit references and page evidence."""
import json
import re
from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.utils.files import save_json


def feature_plan(raw, maximum=3):
    tree = RequirementTree(raw)
    evidence = {}
    def visit(node):
        desc = node.get('description', {})
        pages = {str(i.get('page') or i.get('reference')) for i in desc.get('image', [])
            if isinstance(i, dict)} if isinstance(desc, dict) else set()
        text = json.dumps(node.get('scenarios', []), ensure_ascii=False)
        refs = set(re.findall(r'ref\[requirement\]\(([^)]+)\)', text))
        evidence[node['id']] = (pages, refs)
        for child in node.get('children', []):
            visit(child)
    visit(raw)
    groups = []
    # Keep document order and bounded groups; avoid giant connected components.
    for rid in tree.leaves:
        group = groups[-1] if groups else []
        pages, refs = evidence[rid]
        related = any(pages & evidence[other][0] or other in refs or rid in evidence[other][1]
            for other in group)
        if not group or len(group) >= maximum or not related:
            groups.append([rid])
        else:
            group.append(rid)
    return {'groups': [{'id': f'feature-{i+1}', 'requirements': group,
        'dependencies': sorted({ref for rid in group for ref in evidence[rid][1]
            if ref in tree.requirements and ref not in group})}
        for i, group in enumerate(groups)]}


def publish_plan(raw, arc_dir, maximum=3):
    result = feature_plan(raw, maximum)
    save_json(arc_dir / 'feature_plan.json', result)
    return result


def task_feature(output, task):
    path = output / '.arc/feature_plan.json'
    if not path.is_file():
        return None
    ids = set()
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('id', 'requirement_id') and isinstance(child, str): ids.add(child)
                visit(child)
        elif isinstance(value, list):
            for child in value: visit(child)
    visit(task)
    plan = json.loads(path.read_text(encoding='utf-8'))
    matches = [dict(group) for group in plan['groups'] if ids.intersection(group['requirements'])]
    source = output / '.arc/requirements.json'
    if source.is_file():
        tree = RequirementTree(json.loads(source.read_text(encoding='utf-8-sig')))
        for group in matches:
            group['related_requirement_briefs'] = [tree.requirements[rid] for rid in group['requirements']
                if rid not in ids]
    return matches
