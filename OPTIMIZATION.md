# Experimental workflow optimization

This version is opt-in. Omitting `--optimize` retains the original pipeline and acceptance policy.

```bash
uv run main.py input --output-dir outputs/optimized --optimize --model-log
```

Use a fresh output directory for each comparison. Keep task inputs, model, provider and runtime limits identical.

## What changes

1. **Feature groups.** A deterministic planner groups consecutive leaf requirements using explicit requirement references and shared page hints, at most three per group. Original IDs and document order are preserved. Joint design tasks operate on the group; implementation tasks still run per API/page/requirement, with related requirement briefs and group dependencies supplied. This first experiment does not combine all code edits into one large task or infer dependency order from vague lexical similarity. Inspect `.arc/feature_plan.json`.
2. **Joint design calls.** Optimized stage 02 produces both database schema and per-step read/write obligations. Stage 03 validates that pair without another model request. Optimized stage 07 jointly designs UI and interactions using the existing backend contracts. Stage 09 validates the saved joint design. Both original artifacts remain available, with checks on symbols, per-step ownership, parent links, frontend variables and API effect coverage. Original requirement scenarios remain the source of behavioral expectations. Joint artifacts are only published after complete validation. Signatures invalidate reuse when source requirements, schema, contracts, conventions or visual analysis change; rerun optimized 02 or 07 when instructed. A failed rerun invalidates the old joint marker.
3. **Deterministic SQL.** Ordinary fully specified tables are lowered by code and syntax-checked with SQLite. Foreign keys, nullability, uniqueness, primary keys, booleans and JSON validity are retained. Natural-language constraints, unusual identifiers or composite primary keys fall back to the original model path, rather than silently dropping constraints. Existing table files are preserved; real database initialization/runtime checks and model repair remain mandatory. This version does not generate generic form/API implementations or replace domain logic with templates.
4. **Bounded task packets.** Initial pinned file contents are limited to 36,000 characters total and 10,000 per file, on complete lines. Explicit excerpt metadata tells the model to batch exact reads for missing evidence. Full original requirement scenarios are not shortened. Duplicate full pinned-file reads are omitted; explicit reads of truncated files remain available and stale excerpts are discarded. Each task permits at most 24 `generate_json` calls and six read/discovery request batches. Both editing and design loops enforce the budgets. Provider-internal HTTP retries are not counted as separate logical calls. Use `--agent-max-calls 32 --agent-max-reads 8` if the task legitimately needs more evidence. Exhaustion reports failure, never success. Three identical consecutive failures stop the unproductive task after a warning to reconsider its cause. File-agent counts and duration are recorded in `.arc/agent_metrics.jsonl`, including failure outcomes.
5. **Current-first checks.** Compile first, run the current API/page/requirement suite before accumulated regressions, and stop immediately when it fails. On success, all previously accepted suites still run; final full verification and independent original-requirement acceptance remain. E2E failures avoid unnecessary UI checks on that round. Independent acceptance repairs also check the current case first. This prioritizes early failure detection, rather than attempting an unsafe dependency-based test skipping scheme. Two separate test invocations on a passing round can add startup overhead; the expected benefit is on repeated failing rounds and must be measured.

The public stage names and generated application deployment contract are unchanged. Single-stage optimized runs require their joint-design predecessors. Do not mix original stage 02/07 artifacts with optimized stage 03/09 without regenerating the corresponding pair.

## Verification and limits

`tests/test_optimized_workflow.py` exercises grouped handoffs and call counts with a fake model; actual SQLite constraints; stale joint-artifact rejection; bounded reads and queries; source freshness; stopping repeated failures; regression acceptance; CLI and compiler dispatch. It does not call a paid model, npm or a browser.

The source repository has pre-existing failing/import-incompatible tests (including references to removed numbered modules and outdated fixture contracts). Compare failures to the unmodified baseline rather than interpreting all failures as new regressions. The console waiting indicator test can depend on terminal rendering and scheduling.

A paid full website-generation run was not performed: no usable model key/configuration was available in the session. There are no measured claims of improved Bookstack score, total tokens or wall time. Review joint prompt quality, grouping size and budgets with real task runs before enabling this as the default.

## Suggested A/B run

```bash
uv run main.py input --output-dir outputs/baseline --model-log
uv run main.py input --output-dir outputs/optimized --optimize --model-log
```

Inspect `.arc/compilation.json` and `.arc/independent_acceptance.json` before comparing cost. `model_calls.jsonl` records provider-reported usage and per-call latency; `.arc/agent_metrics.jsonl` records logical task calls, reads, edit rounds and task duration for optimized runs. Compare the same external/official business tests and account for unfinished/failed runs. Lower cost caused by early task failure is not an efficiency improvement.
