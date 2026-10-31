"""A frozen host component budget cannot hide a bad quantile or changed source."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "torture"))

from verify_host_budget import (
    SAMPLES,
    breaches,
    ceilings,
    distribution,
    load_budget,
    signature,
)


class HostBudgetTests(unittest.TestCase):
    def summary(self):
        return {"refresh": distribution([.1] * (SAMPLES - 1) + [3]),
                "cached": distribution([.002] * (SAMPLES - 1) + [.3])}

    def test_nearest_rank_p99_retains_maximum_outlier_separately(self):
        result = self.summary()["refresh"]
        self.assertEqual(result["p99"], .1)
        self.assertEqual(result["max"], 3)
        for values in ([.1] * (SAMPLES - 1), [float("nan")] * SAMPLES, [True] * SAMPLES):
            with self.subTest(length=len(values)), self.assertRaises(ValueError):
                distribution(values)

    def test_frozen_margins_and_all_four_independent_breach_checks(self):
        summary = self.summary()
        limits = ceilings([summary] * 3)
        self.assertEqual(limits, {"refresh.p99": 1.0, "refresh.max": 6.0, "cached.p99": .1, "cached.max": .6})
        for key, limit in limits.items():
            changed = copy.deepcopy(summary)
            path, statistic = key.split(".")
            changed[path][statistic] = limit + .001
            self.assertEqual([row["metric"] for row in breaches(changed, limits)], [key])

    def test_verification_refuses_changed_source_environment_and_ceilings(self):
        env = {key: key for key in ("os", "release", "kernel_version", "python", "psutil", "rich",
                                    "logical_cpus", "physical_memory_bytes")}
        record = {"source": {"commit": "a" * 40, "sha256": {"file": "b" * 64}}, "environment": env}
        value = {"host_budget_schema": 1, "source": record["source"], "environment_signature": signature(env),
                 "samples_per_replicate": 200, "replicates": 3, "interval_seconds": 5.0,
                 "reference_summaries": [self.summary()] * 3}
        value["ceilings_ms"] = ceilings(value["reference_summaries"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            path.write_text(json.dumps(value))
            self.assertEqual(load_budget(path, record), value)
            for mutate in (lambda b: b["source"].update(commit="c" * 40),
                           lambda b: b["environment_signature"].update(python="other"),
                           lambda b: b["ceilings_ms"].update(**{"refresh.max": 100})):
                changed = copy.deepcopy(value)
                mutate(changed)
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    load_budget(path, record)


if __name__ == "__main__":
    unittest.main()
