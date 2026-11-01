"""Bounded diagnostics for private installed-wheel operating fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager, suppress
from pathlib import Path

MAX_EVIDENCE_BYTES = 64 * 1024
MAX_OUTPUT_CHARACTERS = 8192
MAX_DIAGNOSTIC_BYTES = 1024 * 1024


def _evidence_context(root: Path) -> dict:
    """Read only the expected private fixture file, never a receipt-supplied path."""
    try:
        private_root = root.resolve(strict=True)
        expected = private_root / "exercise" / "operating-evidence.json"
        for path in (expected.parent, expected):
            metadata = path.lstat()
            if (stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                return {"status": "linked_path"}
        evidence = expected.resolve(strict=True)
        evidence.relative_to(private_root)
        if not stat.S_ISREG(evidence.stat().st_mode):
            return {"status": "nonregular"}
        with evidence.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                return {"status": "nonregular"}
            if metadata.st_size > MAX_EVIDENCE_BYTES:
                return {"status": "oversized", "limit_bytes": MAX_EVIDENCE_BYTES}
            raw = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES:
            return {"status": "oversized", "limit_bytes": MAX_EVIDENCE_BYTES}
        document = json.loads(raw)
        if not isinstance(document, dict):
            return {"status": "malformed"}
        # This is diagnostic context, never acceptance evidence. Nonfinite or
        # excessively nested data must not prevent the original failure.
        json.dumps(document, allow_nan=False)
        return {"status": "available", "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), "document": document}
    except Exception as error:  # noqa: BLE001 - diagnostics must not replace the original failure
        return {"status": "unavailable", "error_type": type(error).__name__}


def _output_context(value: str) -> dict:
    return {"text": value[:MAX_OUTPUT_CHARACTERS],
            "omitted_characters": max(0, len(value) - MAX_OUTPUT_CHARACTERS)}


@contextmanager
def retain_probe_failure(root: Path, result):
    """Emit context before TemporaryDirectory cleanup, then re-raise unchanged."""
    try:
        yield
    except Exception:
        # Even a diagnostic serialization/write failure must leave the exact
        # command/assertion exception and its traceback authoritative.
        with suppress(Exception):
            context = {"returncode": result.returncode,
                       "stdout": _output_context(result.stdout),
                       "stderr": _output_context(result.stderr),
                       "evidence": _evidence_context(root)}
            rendered = json.dumps(context, ensure_ascii=True, allow_nan=False)
            if len(rendered.encode("ascii")) > MAX_DIAGNOSTIC_BYTES:
                rendered = json.dumps({"returncode": result.returncode,
                                       "evidence": {"status": "diagnostic_output_exceeded_limit"}})
            print("Installed operating failure context: " + rendered, file=sys.stderr)
        raise
