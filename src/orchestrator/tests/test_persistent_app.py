from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from factorio_ai import app as base
from factorio_ai.persistence import PersistentRunState
from factorio_ai.persistent_app import (
    MAX_FORCED_PLAN_CONTINUATIONS,
    _checkpoint_for_run,
    _should_force_plan_continue,
)


class PersistentPlanGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = PersistentRunState(Path(self.tempdir.name), "player-one", "Build defense hub")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _metrics(self) -> base.RunMetrics:
        return base.RunMetrics(started_at=0.0)

    def test_invalid_checkpoint_seeds_attempt_counter_and_forces_continuation(self):
        self.store.save_plan_checkpoint(
            4,
            {"production_blocks": [{"id": "ammo"}], "placements": []},
            {
                "status": "PLAN_INVALID",
                "issue_count": 1,
                "warning_count": 0,
                "issues": [{"code": "power_network_disconnected"}],
                "warnings": [],
            },
        )
        metrics = self._metrics()

        checkpoint = _checkpoint_for_run(self.store, metrics, True)

        self.assertIsNotNone(checkpoint)
        self.assertEqual(metrics.plan_validation_attempts, 4)
        self.assertTrue(_should_force_plan_continue(True, metrics, self.store, 0))

    def test_valid_plan_or_retry_cap_allows_completion(self):
        self.store.save_plan_checkpoint(
            2,
            {"production_blocks": [{"id": "ammo"}], "placements": []},
            {
                "status": "PLAN_INVALID",
                "issue_count": 1,
                "warning_count": 0,
                "issues": [{"code": "entity_without_power_coverage"}],
                "warnings": [],
            },
        )
        metrics = self._metrics()
        _checkpoint_for_run(self.store, metrics, True)

        self.assertFalse(
            _should_force_plan_continue(
                True,
                metrics,
                self.store,
                MAX_FORCED_PLAN_CONTINUATIONS,
            )
        )

        metrics.plan_validated = True
        self.assertFalse(_should_force_plan_continue(True, metrics, self.store, 0))

    def test_normal_non_gated_run_is_never_forced(self):
        metrics = self._metrics()
        setattr(metrics, "_latest_plan_validation", {"status": "PLAN_INVALID", "issue_count": 3})
        self.assertFalse(_should_force_plan_continue(False, metrics, self.store, 0))


if __name__ == "__main__":
    unittest.main()
