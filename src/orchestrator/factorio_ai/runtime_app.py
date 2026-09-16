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


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()
