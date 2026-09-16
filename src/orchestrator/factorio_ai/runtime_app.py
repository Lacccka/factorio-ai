from __future__ import annotations

# Import the optimized runtime first so persistence, resume hardening, semantic memory,
# phase-specific tools, and partial-execution recovery are installed.
from . import optimized_app as optimized  # noqa: F401
from . import app as base
from . import world_model as world_model


# A developed Factorio base spans several production/resource districts. The original
# semantic-memory limits were tuned for local tasks and retained too few survey regions,
# so later planning could incorrectly infer that an upstream source did not exist simply
# because its sector had fallen out of the tiny working set.
#
# Keep more regional facts on disk while still bounding the prompt. Four entries per
# section are enough to expose a compact cross-base atlas without returning to raw scans.
world_model.MAX_SECTION_ENTRIES = max(world_model.MAX_SECTION_ENTRIES, 16)
world_model.MAX_CONTEXT_ENTRIES_PER_SECTION = max(
    world_model.MAX_CONTEXT_ENTRIES_PER_SECTION,
    4,
)
world_model.MAX_CONTEXT_CHARS = max(world_model.MAX_CONTEXT_CHARS, 28_000)

# Whole-base surveys learn important facts through targeted tools after broad regional
# scans. Persist those results into the same durable semantic sections so a later goal
# does not lose facts such as an off-bus stone-brick source merely because the recipe was
# confirmed by trace_item_flow rather than survey_factory_layout.
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

# Keep strict validation for dangerous/structural operations: destructive removals and
# two-tile splitter taps into existing trunks must be explicit and geometrically safe.
from . import plan_execution_guard as plan_execution_guard  # noqa: E402,F401

# Validate mixed pole chains using Factorio's actual shorter-end wire reach. This remains
# an architectural feasibility invariant, not per-tile execution micromanagement.
from . import power_reach_guard as power_reach_guard  # noqa: E402,F401

# Exact additive placements can still be batched after PLAN_VALID and execution gets its
# own turn allowance so planning cannot consume the complete runtime budget.
from . import execution_batch_guard as execution_batch_guard  # noqa: E402,F401

# PLAN_VALID is an architectural contract. Ordinary belt/underground geometry and small
# pole shifts may adapt locally to live obstacles while sources, taps, destructive changes,
# machines and inserter relationships stay protected.
from . import architectural_execution_guard as architectural_execution_guard  # noqa: E402,F401


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
