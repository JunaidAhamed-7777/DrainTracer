"""Linux resource semantics, permission handling, and live /proc sampling."""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401
import psutil

from helpers import make_process
from src.handle_types import CATEGORIES, HandleClassifier
from src.leak_detector import LeakDetector
from src.process_runner import ProcessManager


class LinuxCounterTests(unittest.TestCase):
    def test_descriptors_are_counted_when_windows_handles_do_not_exist(self) -> None:
        proc = SimpleNamespace(num_fds=mock.Mock(return_value=17))
        self.assertEqual(LeakDetector._safe_num_handles(proc), 17)
        proc.num_fds.side_effect = psutil.AccessDenied(7)
        self.assertIsNone(LeakDetector._safe_num_handles(proc))

    def test_private_memory_uses_uss_without_substituting_rss(self) -> None:
        memory = SimpleNamespace(rss=100, vms=1000)
        proc = SimpleNamespace(memory_full_info=mock.Mock(return_value=SimpleNamespace(uss=60)))
        self.assertEqual(LeakDetector._private_memory(proc, memory), 60)
        proc.memory_full_info.side_effect = psutil.AccessDenied(7)
        self.assertIsNone(LeakDetector._private_memory(proc, memory))
        proc.memory_full_info.return_value = memory
        proc.memory_full_info.side_effect = None
        self.assertIsNone(LeakDetector._private_memory(proc, memory))

    def test_new_psutil_footprint_api_is_preferred(self) -> None:
        proc = SimpleNamespace(memory_footprint=mock.Mock(return_value=SimpleNamespace(uss=30)))
        self.assertEqual(LeakDetector._private_memory(proc, SimpleNamespace(rss=100)), 30)

    def test_private_memory_permission_does_not_discard_other_metrics(self) -> None:
        proc = mock.MagicMock(pid=7)
        proc.name.return_value = "worker"
        proc.status.return_value = psutil.STATUS_SLEEPING
        proc.memory_info.return_value = SimpleNamespace(rss=100, vms=1000)
        del proc.memory_footprint
        del proc.num_handles
        proc.memory_full_info.side_effect = psutil.AccessDenied(7)
        proc.num_fds.return_value = 9
        proc.num_threads.return_value = 2
        proc.cpu_times.return_value = SimpleNamespace(user=0.0, system=0.0)
        proc.io_counters.return_value = SimpleNamespace(read_bytes=0, write_bytes=0)
        metrics = LeakDetector(ProcessManager(None))._sample_process(proc)
        self.assertEqual(metrics.private_bytes, 0)
        self.assertFalse(metrics.measured("private_bytes"))
        self.assertTrue(metrics.measured("rss_bytes"))
        self.assertTrue(metrics.measured("handles"))
        self.assertEqual((metrics.rss_bytes, metrics.handles, metrics.threads), (100, 9, 2))
        self.assertFalse(metrics.access_denied)

    def test_inaccessible_descriptor_directory_does_not_disable_classifier(self) -> None:
        with mock.patch("src.handle_types.sys.platform", "linux"):
            classifier = HandleClassifier()
        with mock.patch("src.handle_types.os.scandir", side_effect=PermissionError("protected")):
            for _ in range(4):
                self.assertEqual(classifier.classify([7]), {})
        self.assertTrue(classifier.available)
        self.assertIn("protected", classifier.last_error)

    def test_partial_classification_has_no_false_tree_total_or_growth(self) -> None:
        counts = {category: 0 for category in CATEGORIES}
        counts["file"] = 4
        classifier = mock.Mock(available=True)
        classifier.classify.return_value = {1: counts}
        detector = LeakDetector(ProcessManager(None), classifier=classifier)
        detector._handle_type_history.append((1.0, counts))
        classified, total = detector._classify_handles([make_process(1), make_process(2)])
        self.assertIsNone(total)
        self.assertEqual(classified[0].handle_types, counts)
        self.assertIsNone(classified[1].handle_types)
        self.assertFalse(detector._handle_type_history)


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux /proc")
class LiveLinuxMetricsTests(unittest.TestCase):
    def test_files_unconnected_sockets_and_pipes_are_classified(self) -> None:
        classifier = HandleClassifier()
        pid = os.getpid()
        before = classifier.classify([pid])
        self.assertIn(pid, before, classifier.last_error)
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as cleanup:
            for index in range(12):
                cleanup.enter_context(open(os.path.join(directory, str(index)), "w"))
            for _ in range(5):
                cleanup.enter_context(socket.socket(socket.AF_UNIX))
            for descriptor in os.pipe():
                cleanup.callback(os.close, descriptor)
            cleanup.callback(os.close, os.eventfd(0, os.EFD_CLOEXEC))
            after = classifier.classify([pid])
        self.assertIn(pid, after, classifier.last_error)
        growth = {key: after[pid][key] - before[pid][key] for key in CATEGORIES}
        self.assertEqual(growth["file"], 12)
        self.assertEqual(growth["socket"], 5)
        self.assertEqual(growth["other"], 2)
        self.assertEqual(growth["event"], 1)

    def test_live_sampler_tracks_private_memory_and_descriptor_growth(self) -> None:
        detector = LeakDetector(ProcessManager(None))
        proc = psutil.Process()
        before = detector._sample_process(proc)
        self.assertTrue(before.measured("private_bytes"))
        self.assertTrue(before.measured("handles"))
        with contextlib.ExitStack() as cleanup:
            for _ in range(8):
                cleanup.enter_context(open(os.devnull, "rb"))
            memory = bytearray(16 * 1024 * 1024)
            after = detector._sample_process(proc)
            self.assertGreaterEqual(after.private_bytes - before.private_bytes, 12 * 1024 * 1024)
            self.assertGreaterEqual(after.handles - before.handles, 8)
            self.assertEqual(len(memory), 16 * 1024 * 1024)
        final = detector._sample_process(proc)
        self.assertEqual(after.handles - final.handles, 8)

    def test_exited_pid_is_unknown_and_does_not_poison_future_passes(self) -> None:
        classifier = HandleClassifier()
        self.assertEqual(classifier.classify([2**30]), {})
        self.assertIn(os.getpid(), classifier.classify([os.getpid()]))


if __name__ == "__main__":
    unittest.main()
