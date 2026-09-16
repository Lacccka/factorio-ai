# Semantic world memory and token control

The orchestrator treats a long-lived Factorio save as a persistent world, not as a sequence of unrelated chats.

## Local state

Three git-ignored files under `state/` now have separate responsibilities:

- `factory_knowledge.json` — bounded raw observations retained for compatibility, diagnostics and migration.
- `world_model.json` — compact semantic facts used in new model prompts.
- `active_factory_plan.json` — exact-goal plan/validation/execution checkpoint.

`world_model.json` is deliberately smaller than the raw observation cache. It currently keeps bounded sections for factory architecture, resource scans, power topology, buildable areas and local area summaries. A previous `survey_factory_layout` result, for example, is converted to a compact set of significant belt runs and assembler zones instead of replaying the full MCP response on every later run.

Existing `factory_knowledge.json` files are migrated lazily: the first optimized run derives semantic facts from compatible stored observations and starts using `world_model.json` without requiring a full rediscovery pass.

## World revisions and selective revalidation

Successful AI mutations advance the existing `world_revision`. The semantic model stores the revision at which each fact was observed and a short mutation journal classified by subsystem, such as geometry, logistics, production, power, resources, research or inventory.

An old fact is not considered globally invalid simply because the revision increased. A fact is annotated with `may_be_affected_by` only when a later mutation touches one of the same semantic domains. For example:

- crafting transport belts changes inventory but does not invalidate remembered physical bus geometry;
- placing a belt can mark architecture/logistics facts as locally suspect;
- placing an electric pole can mark power/geometry facts while leaving a remote ore-patch observation reusable.

The model is instructed to revalidate only the affected local area/subsystem when the current goal depends on it. Human players can still change the save independently, so exact placement and occupancy are checked live before consequential construction.

## Phase-specific tool surfaces

With `--plan-gated`, the cloud model no longer receives the entire MCP schema on every turn.

The active tool surface changes by phase:

- `plan` — architecture, recipes/rates, flow, power, placement preflight and `submit_factory_plan`;
- `resume` — the existing validator-issue-specific resume gate remains authoritative;
- `execute` — build/mine/configure/move/wait operations plus narrow local verification and plan resubmission;
- `verify` — read-only checks after mutation budget exhaustion.

This reduces both repeated tool-schema input and the model's action search space. Set `AGENT_PHASE_TOOL_FILTER=0` temporarily if an upstream MCP rename/tool addition needs debugging.

## OpenAI context controls

The optimized entrypoint keeps the existing OpenAI stateful Responses flow, stable prompt-cache key and automatic context compaction. `OPENAI_CONTEXT_COMPACT_TOKENS=80000` remains the default threshold; set it to `0` only when diagnosing compaction behavior.

The intended primary model is GPT-5.6 Sol. The optimization strategy is to give Sol a smaller, more durable representation of the same world rather than delegating planning to a weaker model.

## After pulling

Because the console-script entrypoint now targets the optimized orchestrator, reinstall the editable package once:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ./src/orchestrator
```

Recommended configuration:

```dotenv
AI_PROVIDER=openai
OPENAI_MODEL=gpt-5.6-sol
OPENAI_CONTEXT_COMPACT_TOKENS=80000
AGENT_PHASE_TOOL_FILTER=1
```

The first run can create/migrate `state/world_model.json`. Subsequent goals in the same save reuse that world knowledge even when the goal text differs; only the active plan checkpoint remains exact-goal scoped.
