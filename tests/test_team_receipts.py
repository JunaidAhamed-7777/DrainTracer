"""W4 receipt projection keeps policy qualifications and drops private paths/text."""

import copy
import json
import unittest
from types import SimpleNamespace

from src.team_models import TeamError
from src.team_receipts import comparison_text, prepare_approval, prepare_receipt
from src.team_transport import SharingError


def check_result():
    return {"baseline_schema": 1, "revision": 1, "audit_head": "b" * 64, "source_commit": "c" * 40,
            "exit_code": 0, "passed": True, "findings": [], "suppressions": [],
            "comparison": {"mode": "repeated", "strict": True, "comparability": "comparable",
                           "measured_verdict": "unchanged", "over_budget": False, "caveats": [],
                           "before": ["C:/PrivateSeed123/before.json"], "after": ["C:/PrivateSeed123/after.json"],
                           "resources": {name: {"verdict": "unchanged", "drift_before": {"median": 0},
                                                "drift_after": {"median": 0}} for name in ("memory", "handles", "threads")}}}


class ReceiptProjectionTests(unittest.TestCase):
    def receipt(self, result):
        return prepare_receipt(result, repository="Owner/Repo", workload_id="workload", approval_id="d" * 32)

    def test_projection_and_digest_never_include_or_hash_private_paths_or_opaque_text(self):
        result = check_result()
        result["opaque"] = "PrivateSeed123"
        first = self.receipt(result)
        result["comparison"]["before"] = ["D:/OtherSecret456/file.json"]
        result["opaque"] = "AnotherSecret"
        second = self.receipt(result)
        self.assertEqual(first, second)
        self.assertNotIn("PrivateSeed123", json.dumps(first))
        self.assertEqual(first["metrics"]["private_bytes"]["unit"], "bytes/second")
        self.assertEqual(first["basis"], "client-computed")
        self.assertIn("not confidence intervals", comparison_text({"kind": "comparison_receipt", "value": first}))

    def test_inconclusive_unknown_and_budget_outcomes_are_preserved(self):
        result = check_result()
        result.update(exit_code=3, passed=False)
        result["comparison"]["measured_verdict"] = "inconclusive"
        result["comparison"]["resources"]["memory"].update(verdict="unknown", drift_after={"median": None})
        value = self.receipt(result)
        self.assertEqual(value["outcome"], "inconclusive")
        self.assertIsNone(value["metrics"]["private_bytes"]["after"])
        result = check_result()
        result.update(exit_code=4, passed=False)
        result["comparison"]["over_budget"] = True
        value = self.receipt(result)
        self.assertEqual(value["outcome"], "regression")
        self.assertTrue(any("budget was exceeded" in item for item in value["qualifications"]))

    def test_inconsistent_passes_malformed_fields_and_unsuppressed_regressions_are_refused(self):
        for mutate in (
            lambda r: r.update(passed=False), lambda r: r.update(findings=None),
            lambda r: r["comparison"].update(comparability=[]),
            lambda r: r["comparison"].update(caveats=[{"blocks": "insufficient coverage"}]),
            lambda r: r["comparison"]["resources"]["memory"].update(verdict="regressed"),
            lambda r: r["comparison"]["resources"]["memory"].update(verdict=[]),
            lambda r: r["comparison"]["resources"]["memory"].update(drift_after={"median": float("nan")}),
        ):
            result = check_result()
            mutate(result)
            with self.subTest(result=result), self.assertRaises((SharingError, TeamError)):
                self.receipt(result)

    def test_active_scoped_exception_is_visible_and_wrong_scope_cannot_create_a_pass(self):
        result = check_result()
        result["comparison"]["resources"]["memory"]["verdict"] = "regressed"
        result["findings"] = [{"resource": "memory", "kind": "regression", "suppressed_by": [1]}]
        result["suppressions"] = [{"sequence": 1, "active": True, "resource": "memory", "kind": "regression",
                                   "baseline_revision": 1, "workload_id": "workload"}]
        self.assertTrue(any("exceptions influenced" in item for item in self.receipt(result)["qualifications"]))
        for change in ({"baseline_revision": 2}, {"workload_id": "other"}, {"active": False}):
            changed = copy.deepcopy(result)
            changed["suppressions"][0].update(change)
            with self.subTest(change=change), self.assertRaises(SharingError):
                self.receipt(changed)

    def test_approval_uses_validated_store_revision_and_latest_policy_audit_head(self):
        verified = []
        store = SimpleNamespace(reports=lambda: verified.append(True),
                                state={"revision": 2, "workload_id": "workload"},
                                audit=[{"action": "promote", "source_commit": "a" * 40, "digest": "a" * 64},
                                       {"action": "suppress", "source_commit": "c" * 40, "digest": "b" * 64}])
        value = prepare_approval(store, "Owner/Repo")
        self.assertEqual(verified, [True])
        self.assertEqual(value["source_commit"], "a" * 40)
        self.assertEqual(value["audit_head"], "b" * 64)
        self.assertEqual(value["baseline_revision"], 2)


if __name__ == "__main__":
    unittest.main()
