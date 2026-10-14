"""The system process table: parsing, live agreement with psutil, fallback."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

import helpers  # noqa: F401  (adds the repository root to sys.path)
import psutil

from src.process_table import (
    ProcessEntry,
    ProcessTable,
    _SystemProcessInformation,
    _SystemThreadInformation,
    parse_process_table,
)


def _table(processes: list[tuple[int, list[tuple[int, int]]]]) -> bytes:
    """Build SYSTEM_PROCESS_INFORMATION records of (pid, [(state, reason), ...])."""
    records = []
    for pid, threads in processes:
        header = _SystemProcessInformation()
        header.NumberOfThreads = len(threads)
        header.UniqueProcessId = pid
        body = bytearray(bytes(header))
        for state, reason in threads:
            thread = _SystemThreadInformation()
            thread.ThreadState = state
            thread.WaitReason = reason
            body += bytes(thread)
        records.append(body)
    data = bytearray()
    for index, record in enumerate(records):
        if index < len(records) - 1:
            record[0:4] = len(record).to_bytes(4, "little")  # NextEntryOffset
        data += record
    return bytes(data)


class ParseTests(unittest.TestCase):
    def test_wanted_processes_are_decoded(self) -> None:
        raw = _table([(4, [(2, 0)]), (10, [(5, 5), (5, 5)]), (20, [(5, 5), (2, 0)])])
        self.assertEqual(
            parse_process_table(raw, {10, 20, 99}),
            {10: ProcessEntry(2, True), 20: ProcessEntry(2, False)},
        )

    def test_malformed_tables_raise(self) -> None:
        raw = _table([(10, [(5, 5)]), (20, [(2, 0)])])
        with self.assertRaises(ValueError):
            parse_process_table(raw[:-8], {20})  # thread list cut short
        with self.assertRaises(ValueError):
            parse_process_table(raw[:10], {10})  # record cut short
        overlapping = bytearray(raw)
        overlapping[0:4] = (8).to_bytes(4, "little")
        with self.assertRaises(ValueError):
            parse_process_table(bytes(overlapping), {10})

    def test_failures_disable_the_table(self) -> None:
        from unittest import mock

        table = ProcessTable()
        table._enabled = True
        with mock.patch.object(table, "_read", side_effect=OSError("denied")):
            for _ in range(3):
                self.assertIsNone(table.snapshot([1]))
        self.assertFalse(table.available)


@unittest.skipUnless(sys.platform == "win32", "the system process table is Windows-only")
class LiveTests(unittest.TestCase):
    def test_thread_counts_match_psutil(self) -> None:
        entries = ProcessTable().snapshot([os.getpid()])
        self.assertIsNotNone(entries)
        self.assertEqual(entries[os.getpid()].threads, psutil.Process().num_threads())
        self.assertFalse(entries[os.getpid()].suspended)

    def test_a_suspended_process_is_reported_suspended(self) -> None:
        python = getattr(sys, "_base_executable", None) or sys.executable
        child = subprocess.Popen([python, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.kill)
        time.sleep(0.3)
        psutil.Process(child.pid).suspend()
        try:
            entry = ProcessTable().snapshot([child.pid])[child.pid]
            self.assertTrue(entry.suspended)
        finally:
            psutil.Process(child.pid).resume()
        self.assertFalse(ProcessTable().snapshot([child.pid])[child.pid].suspended)

    def test_the_sampler_falls_back_to_psutil(self) -> None:
        from src.leak_detector import LeakDetector
        from src.process_runner import ProcessManager

        python = getattr(sys, "_base_executable", None) or sys.executable
        child = subprocess.Popen([python, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.kill)
        manager = ProcessManager(None)
        manager.attach(child.pid)
        self.addCleanup(manager.detach)
        detector = LeakDetector(manager, handle_types_interval=1000)
        with_table = detector.sample_once().processes[0]
        detector._process_table._enabled = False
        without = detector.sample_once().processes[0]
        self.assertEqual(with_table.threads, without.threads)
        self.assertEqual(with_table.status, without.status)


if __name__ == "__main__":
    unittest.main()
