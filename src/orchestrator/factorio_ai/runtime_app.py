from __future__ import annotations

# Import the optimized runtime first so persistence, resume hardening, semantic memory,
# phase-specific tools, and partial-execution recovery are installed.
from . import optimized_app as optimized  # noqa: F401
from . import app as base
from . import world_model as world_model


# A developed Factorio base spans several production/resource districts. Keep enough
# compact semantic memory to reuse an atlas without pushing raw surveys into every turn.
world_model.MAX_SECTION_ENTRIES = max(world_model.MAX_SECTION_ENTRIES, 16)
world_model.MAX_CONTEXT_ENTRIES_PER_SECTION = max(
    world_model.MAX_CONTEXT_ENTRIES_PER_SECTION,
    4,
)
world_model.MAX_CONTEXT_CHARS = max(world_model.MAX_CONTEXT_CHARS, 28_000)

world_model._TOOL_SECTIONS.update(
    {
        "trace_item_flow": ("architecture", {"logistics", "production"}),
        "get_existing_factory_summary": (
            "architecture",
            {"geometry", "logistics", "production", "power"},
        ),
        "count_item_in_world": ("areas", {"inventory", "logistics", "production"}),
        "get_electric_network": ("power", {"power"}),
    }
)

# Resume semantics: the orchestrator itself freshly revalidates a persisted plan. Do not
# make the model redundantly submit an unchanged PLAN_VALID checkpoint again.
from . import checkpoint_policy as checkpoint_policy  # noqa: E402,F401

# Rate semantics must be installed before the structural plan wrapper captures the base
# validator. target_rate_per_second is sustained design throughput, while machine maximum
# speed is only capacity headroom.
from . import throughput_semantics as throughput_semantics  # noqa: E402,F401

# Structural safety remains strict: destructive removals and source-trunk splitter taps
# must be explicit and geometrically safe.
from . import plan_execution_guard as plan_execution_guard  # noqa: E402,F401

# Mixed electric-pole chains must obey the shorter pole's real copper-wire reach.
from . import power_reach_guard as power_reach_guard  # noqa: E402,F401

# Ordinary logistics are reasoning-led. Sol may choose local belt/underground/pole geometry
# inside the validated work area instead of replaying an exact stale route polyline.
from . import architectural_execution_guard as architectural_execution_guard  # noqa: E402,F401

# Let the agent safely correct only additive entities that it previously placed itself;
# pre-existing factory infrastructure remains protected.
from . import owned_additive_guard as owned_additive_guard  # noqa: E402,F401

# Keep pre-generated mutation chains live-world-aware: pure range misses are recovered by
# walking, while a real collision stops later mutations in that response before the agent
# builds a disconnected continuation past an unseen obstacle.
from . import sequential_execution as sequential_execution  # noqa: E402,F401

# Planning and execution have separate cloud-turn and mutation allowances. This module
# changes budget only; it deliberately contains no execution strategy or coordinate policy.
from . import execution_budget_guard as execution_budget_guard  # noqa: E402,F401


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
