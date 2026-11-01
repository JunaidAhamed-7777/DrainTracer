"""Bounded acceptance-test polling of a known owned checkpoint writer."""

from __future__ import annotations

import time

from src.api import _version
from src.checkpoints import CheckpointError, read_checkpoint
from src.report_contract import ReportContractError


def wait_for_checkpoint(path, *, writer, samples, session_id, timeout=6):
    """Wait for an owned live writer; stopped-writer reads remain strict.

    Windows replacement can briefly deny a concurrent ordinary file open.
    Only the actual OS open errors are transient here, never schema, digest,
    session-identity, parsing or other validation failures.
    """
    deadline = time.monotonic() + timeout
    last_open_error = None
    while True:
        try:
            report = read_checkpoint(path, app_version=_version(), session_id=session_id)
        except CheckpointError as error:
            contract_error = error.__cause__
            open_error = contract_error.__context__ if isinstance(contract_error, ReportContractError) else None
            if not isinstance(open_error, (PermissionError, FileNotFoundError)) or writer.poll() is not None:
                raise
            last_open_error = error
        else:
            if report["session_facts"]["samples"]["taken"] >= samples:
                return report
        if time.monotonic() >= deadline:
            raise AssertionError(
                "live checkpoint did not reach its required sample count within the bounded wait"
            ) from last_open_error
        time.sleep(0.02)
