from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

# Keep this test file alphabetically last: importing optimized_app intentionally installs
# runtime monkey patches on the shared base/persistence modules.
from factorio_ai import optimized_app  # noqa: F401
from factorio_ai.persistence import PersistentRunState


class OptimizedRuntimeWiringTests(unittest.TestCase):
    def test_persistent_store_emits_semantic_context_and_precise_invalidation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = PersistentRunState(Path(tempdir), "player-one", "Build circuits")
            store.record_observation(
                "survey_factory_layout",
                {"radius": 60},
                json.dumps(
                    {
                        "status": "ok",
                        "center_x": 0,
                        "center_y": 0,
                        "radius": 60,
                        "belt_runs": [
                            {
                                "axis": "horizontal",
                                "flow_direction": "east",
                                "fixed_coordinate": 3.5,
                                "start_coordinate": -10,
                                "end_coordinate": 40,
                                "tile_count": 51,
                            }
                        ],
                        "assembler_zones": [],
                    }
                ),
            )
            context, count = store.factory_context()
            self.assertEqual(count, 1)
            self.assertIn("SEMANTIC FACTORY WORLD MODEL", context)
            self.assertTrue((Path(tempdir) / "world_model.json").exists())

            # Runtime policy must not stale physical architecture merely because the
            # crafted inventory item happens to be a belt.
            store.mark_world_mutation("craft", {"item": "transport-belt", "count": 10})
            context, _ = store.factory_context()
            payload = json.loads(context.splitlines()[-1])
            entry = payload["sections"]["architecture"][0]
            self.assertNotIn("may_be_affected_by", entry)


if __name__ == "__main__":
    unittest.main()
