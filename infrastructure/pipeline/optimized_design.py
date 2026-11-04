"""Grouped joint designs retain the original stage artifacts and independent checks."""
from copy import deepcopy
from hashlib import sha256
import json
from infrastructure.shared.agent.optimization import policy
from infrastructure.shared.agent.planning import publish_plan
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.shared.agent.discovery import ContextIndex
from infrastructure.shared.models.text import TextModel
from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.stages.backend.context import requirement_context
from infrastructure.stages.database.schema import apply_patch, check_related_fields
from infrastructure.stages.database.obligations.records import normalize_inference, normalize_obligations, check_symbols
from infrastructure.stages.frontend.ui.design import empty_design, apply_proposal, normalize_design, data_symbols
from infrastructure.stages.frontend.ui.synthesis import bind_requirement_image_pages
from infrastructure.stages.frontend.triggers.design import empty_triggers, apply_decomposition, validate_coverage
from infrastructure.shared.agent.conventions import load_conventions
from infrastructure.utils.files import save_json, read_artifact
from infrastructure.utils.presentation.console import DemoConsole


def digest(*values):
    return sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def schema_and_obligations(arc_dir, pause=False):
    from infrastructure.pipeline.db_schema import INSTRUCTIONS as SCHEMA_RULES
    from infrastructure.pipeline.db_obligations import INSTRUCTIONS as EFFECT_RULES
    (arc_dir / 'joint_database.json').unlink(missing_ok=True)
    raw = read_artifact(arc_dir, 'requirements.json')
    tree = RequirementTree(raw)
    plan = publish_plan(raw, arc_dir, policy().group_size)
    model, console = TextModel(), DemoConsole(pause)
    schema = {'entities': {}, 'requirements': {}}
    obligations = {'requirements': {rid: {'read': [], 'write': []} for rid in tree.requirements},
        'steps': {sid: {'read': [], 'write': []} for sid in tree.steps}}
    instructions = ('Jointly design the shared schema and data obligations for this bounded feature group. '
        'The following single-requirement rules apply to EACH supplied requirement. '
        'First decide shared entities, then associate fields and direct effects separately by original ID. '
        'Return {"entities": {...}, "requirements": {"REQ-ID": '
        '{"related_fields": [...], "obligations": {"requirement": {"read":[],"write":[]}, "steps":{...}}}}}. '
        'Include exactly every supplied requirement and step. Parent nodes remain organizational.\n'
        + SCHEMA_RULES + '\n' + EFFECT_RULES +
        '\nJOINT OUTPUT OVERRIDE: use the outer entities and requirements object described above; '
        'do not return the individual-stage outer response shapes.')
    for group in plan['groups']:
        ids = group['requirements']
        def accept(response):
            if set(response['requirements']) != set(ids):
                raise ValueError('Return exactly the supplied requirement IDs')
            updated, effects = apply_patch(schema, response), deepcopy(obligations)
            for rid in ids:
                record = response['requirements'][rid]
                check_related_fields(updated, record['related_fields'])
                updated['requirements'][rid] = sorted(set(record['related_fields']))
                inferred = normalize_inference(record['obligations'], tree.own_steps[rid])
                check_symbols([inferred['requirement'], *inferred['steps'].values()], updated)
                effects['requirements'][rid] = inferred['requirement']
                effects['steps'].update(inferred['steps'])
            return updated, effects
        # Only a bounded group of original scenarios and the current shared model.
        from infrastructure.shared.rag.schema import SchemaRetriever
        selected = {}
        for rid in ids:
            context = requirement_context(tree, rid)
            selected.update(SchemaRetriever().retrieve(json.dumps(context, ensure_ascii=False), schema, 6)['entities'])
        request = {'feature': group, 'requirements': [requirement_context(tree, rid) for rid in ids],
            'symbol_inventory': {name: list(entity['fields']) for name, entity in schema['entities'].items()},
            'retrieved_schema': {'entities': selected}}
        (schema, obligations), _ = propose_json(model, instructions, request, accept, console,
            'Joint database design / ' + group['id'], context_index=ContextIndex(lambda: {'schema': schema['entities']}))
    # Publish only a completely validated pair. Later stages never consume half a joint result.
    normalize_obligations(obligations, tree)
    save_json(arc_dir / 'db_schema.json', schema)
    save_json(arc_dir / 'db_obligations.json', obligations)
    save_json(arc_dir / 'joint_database.json', {'digest': digest(raw, schema, obligations)})
    return schema


def consume_obligations(arc_dir, pause=False):
    raw, schema, effects = (read_artifact(arc_dir, name) for name in
        ('requirements.json', 'db_schema.json', 'db_obligations.json'))
    if read_artifact(arc_dir, 'joint_database.json')['digest'] != digest(raw, schema, effects):
        raise ValueError('Joint database inputs changed; rerun optimized stage 02')
    tree = RequirementTree(raw)
    normalize_obligations(effects, tree)
    for records in effects.values(): check_symbols(records.values(), schema)
    return effects


def ui_and_triggers(input_dir, arc_dir, pause=False):
    from infrastructure.stages.frontend.context import load_frontend_context
    from infrastructure.stages.frontend.ui.design import INSTRUCTIONS as UI_RULES
    from infrastructure.stages.frontend.triggers.prompts import INSTRUCTIONS as TRIGGER_RULES
    from infrastructure.stages.backend.primitives import validate_design, ObligationCoverageError
    (arc_dir / 'joint_frontend.json').unlink(missing_ok=True)
    tree, schema, backend, images, contexts = load_frontend_context(arc_dir, input_dir)
    raw = read_artifact(arc_dir, 'requirements.json')
    effects = read_artifact(arc_dir, 'db_obligations.json')
    try:
        validate_design(backend, effects, tree=tree)
    except (KeyError, ObligationCoverageError) as error:
        # Preserve presentation when upstream API design is only partially available.
        (arc_dir / 'frontend_triggers.json').unlink(missing_ok=True)
        DemoConsole(pause).show('Incomplete backend | Use independent UI design', str(error))
        from infrastructure.pipeline.ui_design import design_frontend_ui
        return design_frontend_ui(input_dir, arc_dir, pause=pause)
    plan = publish_plan(raw, arc_dir, policy().group_size)
    model, console = TextModel(json_mode=False), DemoConsole(pause)
    ui, triggers = empty_design(), empty_triggers(tree)
    accepted = []
    rules = ('Jointly design UI and interactions for ONE bounded feature group. '
        'Reuse shared pages, variables and operations across its requirements. '
        'Return {"pages":{}, "elements":{}, "triggers":{}, "requirements": '
        '{"REQ-ID":{"requirement_ui":[],"step_ui":{},"requirement_triggers":[],"step_triggers":{}}}}. '
        'Pages/elements/triggers are shared incremental updates. Associate every original requirement '
        'and every step separately; preserve earlier behavior.\n' + UI_RULES + '\n' + TRIGGER_RULES +
        '\nJOINT OUTPUT OVERRIDE: unlike the isolated UI stage, declare triggers now, alongside '
        'pages/elements, using the combined outer shape above. Return no implementation or tests.')
    for group in plan['groups']:
        ids = group['requirements']
        api_ids = {aid for rid in ids for aid in backend['requirements'].get(rid, [])}
        def accept(response):
            if set(response['requirements']) != set(ids):
                raise ValueError('Return exactly the supplied requirement IDs')
            next_ui, next_triggers = deepcopy(ui), deepcopy(triggers)
            for rid in ids:
                record = response['requirements'][rid]
                if set(record['step_ui']) != set(tree.own_steps[rid]) or set(record['step_triggers']) != set(tree.own_steps[rid]):
                    raise ValueError('Associate every original step explicitly')
                next_ui = apply_proposal(next_ui, {**response, **record}, rid, tree)
                bind_requirement_image_pages(next_ui, contexts[rid], rid)
                next_triggers = apply_decomposition(next_triggers, {**response, **record}, rid)
            validate_ui_links(next_ui, next_triggers, tree, [*accepted, *ids], backend)
            validate_coverage(next_triggers, backend, effects, tree, [*accepted, *ids])
            return next_ui, next_triggers
        # Supply relevant existing page subtrees; discover the rest by exact IDs.
        from infrastructure.shared.rag.pages import PageRetriever
        retrieved = {}
        for rid in ids: retrieved.update(PageRetriever().retrieve(contexts[rid], ui, 3)['pages'])
        from infrastructure.shared.agent.conventions import select_conventions
        related_elements = {uid for page in retrieved.values() for uid in page['elements']}
        related_triggers = {tid: trigger for tid, trigger in triggers['triggers'].items()
            if trigger['source'] in related_elements}
        request = {'feature': group, 'requirements': [contexts[rid] for rid in ids],
            'retrieved_pages': retrieved,
            'page_inventory': {pid: {'name': p['name'], 'route': p['route'], 'root': p['root']} for pid,p in ui['pages'].items()},
            'backend_apis': {aid: backend['apis'][aid] for aid in api_ids},
            'required_obligations': {rid: effects['requirements'][rid] for rid in ids},
            'step_obligations': {sid: effects['steps'][sid] for rid in ids for sid in tree.own_steps[rid]},
            'shared_conventions': select_conventions(load_conventions(arc_dir), requirements=ids), 'existing_triggers': related_triggers}
        (ui, triggers), _ = propose_json(model, rules, request, accept, console,
            'Joint UI/interaction design / ' + group['id'], context_index=ContextIndex(lambda: {
                'page': ui['pages'], 'ui_element': ui['elements'], 'ui_data': data_symbols(ui['elements']),
                'api': backend['apis'], 'trigger': triggers['triggers'], 'convention': load_conventions(arc_dir)}))
        accepted.extend(ids)
    normalize_design(ui, tree)
    save_json(arc_dir / 'ui_design.json', ui)
    save_json(arc_dir / 'frontend_triggers.json', triggers)
    save_json(arc_dir / 'joint_frontend.json', {'digest': digest(raw, schema, backend, effects,
        read_artifact(arc_dir, 'visual_analysis.json'), load_conventions(arc_dir), ui, triggers)})
    return ui


def validate_ui_links(ui, triggers, tree, ids, backend):
    """Validate references before accepting a joint UI/behavior proposal."""
    elements, pages = ui['elements'], ui['pages']
    for pid, page in pages.items():
        if page['root'] not in elements or elements[page['root']]['parent'] is not None:
            raise ValueError(f'Invalid page root {pid}')
    for uid, element in elements.items():
        seen, current = set(), uid
        while current is not None:
            if current in seen or current not in elements:
                raise ValueError(f'Invalid/cyclic element parent for {uid}')
            seen.add(current)
            current = elements[current]['parent']
    variables = data_symbols(elements)
    for rid in ids:
        if any(uid not in elements for uid in ui['requirements'][rid]):
            raise ValueError(f'Unknown UI element in {rid}')
        for sid in tree.own_steps[rid]:
            if any(uid not in elements for uid in ui['steps'][sid]):
                raise ValueError(f'Unknown step UI element in {sid}')
            if any(tid not in triggers['triggers'] for tid in triggers['steps'][sid]):
                raise ValueError(f'Unknown step trigger in {sid}')
        for tid in triggers['requirements'][rid]:
            trigger = triggers['triggers'][tid]
            for mode in ('read', 'write'):
                if any(var not in variables for var in trigger[mode]):
                    raise ValueError(f'Undeclared frontend variable in {tid}')
            if trigger['source'] not in elements:
                raise ValueError(f'Unknown trigger source {tid}')
            for action in trigger['action']:
                if action['kind'] == 'api' and action['api'] not in backend['apis']:
                    raise ValueError(f'Unknown API in {tid}')
                if action['kind'] == 'ui' and action['ui'] not in elements:
                    raise ValueError(f'Unknown UI action in {tid}')


def consume_triggers(input_dir, arc_dir, pause=False):
    if not (arc_dir / 'joint_frontend.json').is_file():
        from infrastructure.pipeline.frontend_triggers import design_frontend_triggers
        return design_frontend_triggers(input_dir, arc_dir, pause=pause)
    raw, schema, backend, effects, visual, ui, triggers = (read_artifact(arc_dir, name) for name in
        ('requirements.json', 'db_schema.json', 'backend_apis.json', 'db_obligations.json',
         'visual_analysis.json', 'ui_design.json', 'frontend_triggers.json'))
    if read_artifact(arc_dir, 'joint_frontend.json')['digest'] != digest(raw, schema, backend, effects,
            visual, load_conventions(arc_dir), ui, triggers):
        raise ValueError('Joint frontend inputs changed; rerun optimized stage 07')
    tree = RequirementTree(raw)
    validate_ui_links(ui, triggers, tree, tree.leaves, backend)
    validate_coverage(triggers, backend, effects, tree, tree.leaves)
    return triggers


def optimized_stage(stage, original):
    return {'02_build_db_schema': schema_and_obligations,
        '03_infer_db_obligations': consume_obligations,
        '07_design_ui': ui_and_triggers,
        '09_design_frontend_triggers': consume_triggers}.get(stage, original)
