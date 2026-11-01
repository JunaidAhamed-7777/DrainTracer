"""Project record boundaries reject opaque data and preserve exact binding values."""

from __future__ import annotations

import copy
import math
import unittest
from unittest import mock

import helpers  # noqa: F401

from src.team_models import TeamError
from src.team_records import validate_record_value

SECRET = "PrivateBindingSeed987654"


def approval():
    return {"record_schema": 1, "repository": "Acme/Repo", "workload_id": "CaseSensitive-あ",
            "source_commit": "a" * 40, "baseline_revision": 1, "audit_head": "b" * 64, "note": ""}


def receipt():
    result = approval()
    result.pop("note")
    result.update(approval_id="c" * 32, outcome="inconclusive", comparison_sha256="d" * 64,
                  basis="client-computed", metrics={"private_bytes": {"verdict": "inconclusive", "before": None,
                                                                    "after": 123, "unit": "bytes"}},
                  qualifications=["coverage incomplete"])
    return result


class RecordValidationTests(unittest.TestCase):
    def invalid(self, kind, value):
        with self.assertRaises(TeamError) as caught:
            validate_record_value(kind, value)
        self.assertEqual(caught.exception.code, "invalid")
        self.assertEqual(str(caught.exception), "invalid team record")
        self.assertNotIn(SECRET, repr(caught.exception))

    def test_binding_values_are_preserved_without_normalization_and_outputs_are_detached(self):
        value = approval()
        value.update(repository=" Acme/Repo ", workload_id="e\u0301", note="  preserve spacing  ", source_commit="a" * 64)
        result = validate_record_value("baseline_approval", value)
        self.assertEqual(result, value)
        self.assertIsNot(result, value)
        self.assertEqual(result["workload_id"], "e\u0301")
        result["note"] = "changed"
        self.assertEqual(value["note"], "  preserve spacing  ")
        value = receipt()
        original = copy.deepcopy(value)
        result = validate_record_value("comparison_receipt", value)
        result["metrics"]["private_bytes"]["after"] = 999
        result["qualifications"].append("new")
        self.assertEqual(value, original)
        self.assertIs(type(original["metrics"]["private_bytes"]["after"]), int)

    def test_all_standard_metric_unit_pairs_accept_nullable_finite_signed_values(self):
        units = {"private_bytes": ("bytes", "bytes/second"), "rss_bytes": ("bytes", "bytes/second"),
                 "vms_bytes": ("bytes", "bytes/second"), "handles": ("count", "count/second"),
                 "threads": ("count", "count/second"), "cpu_percent": ("percent",),
                 "io_read_bytes_per_sec": ("bytes/second",), "io_write_bytes_per_sec": ("bytes/second",)}
        value = receipt()
        value["metrics"] = {name: {"verdict": "unchanged", "before": None, "after": -1.5, "unit": options[0]}
                            for name, options in units.items()}
        self.assertEqual(validate_record_value("comparison_receipt", value), value)
        for name, options in units.items():
            for unit in options:
                with self.subTest(metric=name, unit=unit):
                    value["metrics"][name]["unit"] = unit
                    self.assertEqual(validate_record_value("comparison_receipt", value), value)
        value["metrics"] = {}
        value["qualifications"] = []
        self.assertEqual(validate_record_value("comparison_receipt", value), value)

    def test_schema_revision_and_digest_bindings_are_strict(self):
        for field, invalid_values in {
            "record_schema": (True, False, 1.0, 2, "1"),
            "baseline_revision": (True, 0, -1, 2**31, 1.0),
            "source_commit": ("a" * 39, "A" * 40, "a" * 41, "a" * 65, SECRET),
            "audit_head": ("b" * 63, "B" * 64, 123),
            "repository": ("", " " * 2, "x" * 257),
            "workload_id": ("", "x" * 129),
        }.items():
            for invalid in invalid_values:
                with self.subTest(field=field, value_type=type(invalid).__name__):
                    value = approval()
                    value[field] = invalid
                    self.invalid("baseline_approval", value)
        value = approval()
        value["baseline_revision"] = 2**31 - 1
        self.assertEqual(validate_record_value("baseline_approval", value), value)

    def test_receipt_cannot_change_assertion_basis_or_smuggle_unknown_payloads(self):
        for field, invalid_values in {
            "approval_id": ("c" * 31, "c" * 33, "C" * 32, "cccccccc-cccc-cccc-cccc-cccccccccccc"),
            "comparison_sha256": ("d" * 63, "D" * 64, True),
            "outcome": ("clear", "PASS", {"value": SECRET}),
            "basis": ("server-verified", "client-computed\n", SECRET),
        }.items():
            for invalid in invalid_values:
                with self.subTest(field=field):
                    value = receipt()
                    value[field] = invalid
                    self.invalid("comparison_receipt", value)
        for kind, value in (("baseline_approval", approval()), ("comparison_receipt", receipt())):
            for field in ("project_id", "token", "path", "raw_report", "metadata"):
                changed = dict(value, **{field: {"value": SECRET}})
                self.invalid(kind, changed)
            for field in value:
                changed = dict(value)
                changed.pop(field)
                self.invalid(kind, changed)
        self.invalid(SECRET, approval())
        self.invalid(True, approval())

    def test_controls_surrogates_and_formatting_characters_never_reach_encoding(self):
        for control in ("\0", "\n", "\t", "\x7f", "\x85", "\u202e", "\u200b", "\ud800"):
            for field in ("repository", "workload_id", "note"):
                with self.subTest(field=field, control=ord(control)):
                    value = approval()
                    value[field] = SECRET + control
                    self.invalid("baseline_approval", value)
            value = receipt()
            value["qualifications"] = [SECRET + control]
            self.invalid("comparison_receipt", value)

    def test_nonfinite_huge_boolean_and_opaque_metric_values_are_refused_before_expansion(self):
        nested = None
        for _ in range(100):
            nested = [nested]
        values = (True, math.nan, math.inf, -math.inf, 10**5000, "123", {"value": SECRET}, nested)
        for invalid in values:
            with self.subTest(value_type=type(invalid).__name__):
                value = receipt()
                value["metrics"]["private_bytes"]["before"] = invalid
                with mock.patch("src.team_records.json.dumps") as encode:
                    self.invalid("comparison_receipt", value)
                encode.assert_not_called()
        value = receipt()
        value["metrics"]["private_bytes"]["before"] = value
        self.invalid("comparison_receipt", value)

    def test_metric_keys_shapes_units_and_qualification_limits_are_enforced(self):
        value = receipt()
        value["metrics"]["unknown"] = value["metrics"]["private_bytes"]
        self.invalid("comparison_receipt", value)
        for update in ({"unit": "count"}, {"unit": "bytes/s"}, {"verdict": "PASS"}, {"extra": SECRET}):
            value = receipt()
            value["metrics"]["private_bytes"].update(update)
            self.invalid("comparison_receipt", value)
        for invalid in (("not", "list"), ["x"] * 17, ["x" * 201], [{"value": SECRET}]):
            value = receipt()
            value["qualifications"] = invalid
            self.invalid("comparison_receipt", value)
        value = approval()
        value["note"] = "x" * 501
        self.invalid("baseline_approval", value)

    def test_utf8_byte_bound_and_depth_guard_refuse_without_truncation(self):
        value = receipt()
        value["repository"] = "😀" * 256
        value["workload_id"] = "😀" * 128
        value["qualifications"] = ["😀" * 200] * 16
        value["metrics"] = {name: {"verdict": "unchanged", "before": 10**300, "after": 10**300, "unit": unit}
                            for name, unit in (("private_bytes", "bytes"), ("rss_bytes", "bytes"),
                                               ("vms_bytes", "bytes"), ("handles", "count"), ("threads", "count"),
                                               ("cpu_percent", "percent"), ("io_read_bytes_per_sec", "bytes/second"),
                                               ("io_write_bytes_per_sec", "bytes/second"))}
        original = copy.deepcopy(value)
        self.invalid("comparison_receipt", value)
        self.assertEqual(value, original)
        with mock.patch("src.team_records.MAX_RECORD_DEPTH", 3):
            self.invalid("comparison_receipt", receipt())


if __name__ == "__main__":
    unittest.main()
