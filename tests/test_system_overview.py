"""Resource rankings keep permission failures and reused PIDs isolated."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401 (adds the repository root to sys.path)

from src import system_overview as overview
from src.process_table import ProcessEntry


def _process(name: str, cpu: float, rss: int, handles: int, threads: int) -> mock.Mock:
    process = mock.Mock()
    process.oneshot.side_effect = nullcontext
    process.status.return_value = "running"
    process.name.return_value = name
    process.create_time.return_value = 1.0
    process.cpu_times.return_value = SimpleNamespace(user=cpu, system=0.5)
    process.memory_info.return_value = SimpleNamespace(rss=rss)
    process.num_handles.return_value = handles
    process.num_fds.return_value = handles
    process.num_threads.return_value = threads
    return process


def _ranked(rows: tuple[overview.RankedProcess, ...]) -> list[tuple[int, int | float]]:
    return [(row.process.pid, row.value) for row in rows]


class SystemOverviewTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.object(overview.ProcessTable, "snapshot", return_value=None)
        self._thread_table = patch.start()
        self.addCleanup(patch.stop)

    def test_limits_and_ranks_each_resource_independently(self) -> None:
        processes = {
            10: _process("alpha", 1.0, 100, 20, 6),
            20: _process("beta", 4.0, 200, 10, 6),
            30: _process("gamma", 1.0, 200, 30, 2),
        }
        sampler = overview.SystemOverviewSampler(limit=2)
        with (
            mock.patch.multiple(
                overview.psutil, WINDOWS=True,
                cpu_percent=mock.Mock(side_effect=[0.0, 31.5]),
                virtual_memory=mock.Mock(return_value=SimpleNamespace(total=1000, available=400)),
                pids=mock.Mock(return_value=[30, 20, 10, 0, 99]),
                Process=mock.Mock(side_effect=processes.__getitem__),
            ),
            mock.patch.object(overview.os, "getpid", return_value=99),
            mock.patch.object(overview.time, "monotonic", side_effect=[1.0] * 4 + [2.0] * 4),
        ):
            initial = sampler.sample()
            for process, value in zip(processes.values(), (1.25, 6.5, 1.5)):
                process.cpu_times.return_value = SimpleNamespace(user=value, system=0.5)
            next_sample = sampler.sample()

        self.assertIsNone(initial.cpu_percent)
        self.assertTrue(initial.cpu_warming_up)
        self.assertEqual(initial.top_cpu, ())
        self.assertEqual((initial.used_memory_bytes, initial.total_memory_bytes), (600, 1000))
        self.assertEqual(next_sample.cpu_percent, 31.5)
        self.assertFalse(next_sample.cpu_warming_up)
        self.assertEqual(next_sample.handle_label, "Handles")
        self.assertEqual(_ranked(next_sample.top_cpu), [(20, 250.0), (30, 50.0)])
        self.assertEqual(_ranked(next_sample.top_memory), [(20, 200), (30, 200)])
        self.assertEqual(_ranked(next_sample.top_handles), [(30, 30), (10, 20)])
        self.assertEqual(_ranked(next_sample.top_threads), [(10, 6), (20, 6)])

    def test_linux_skips_exits_and_keeps_readable_metrics_for_protected_processes(self) -> None:
        protected = _process("protected", 1.0, 100, 30, 5)
        protected.name.side_effect = overview.psutil.AccessDenied(10)
        protected.memory_info.side_effect = overview.psutil.AccessDenied(10)
        protected.num_fds.side_effect = PermissionError("cannot list FDs")
        gone = _process("gone", 10.0, 1000, 300, 50)
        gone.num_threads.side_effect = overview.psutil.NoSuchProcess(20)
        zombie = _process("zombie", 10.0, 1000, 300, 50)
        zombie.status.return_value = overview.psutil.STATUS_ZOMBIE
        live = _process("live", 1.0, 100, 10, 1)
        processes = {10: protected, 20: gone, 30: zombie, 40: live}
        with mock.patch.multiple(
            overview.psutil, WINDOWS=False,
            cpu_percent=mock.Mock(side_effect=OSError("host CPU unavailable")),
            virtual_memory=mock.Mock(side_effect=OSError("host memory unavailable")),
            pids=mock.Mock(return_value=list(processes)),
            Process=mock.Mock(side_effect=processes.__getitem__),
        ):
            result = overview.SystemOverviewSampler().sample()

        self.assertIsNone(result.cpu_percent)
        self.assertIsNone(result.total_memory_bytes)
        self.assertEqual(result.handle_label, "Open FDs")
        self.assertEqual(_ranked(result.top_memory), [(40, 100)])
        self.assertEqual(_ranked(result.top_handles), [(40, 10)])
        self.assertEqual(_ranked(result.top_threads), [(10, 5), (40, 1)])
        self.assertEqual(result.top_threads[0].process.name, "Unknown program")
        live.num_handles.assert_not_called()
        self._thread_table.assert_not_called()

    def test_windows_uses_batched_thread_counts_with_per_process_fallback(self) -> None:
        processes = {10: _process("first", 0.0, 10, 10, 10), 20: _process("second", 0.0, 20, 20, 20)}
        self._thread_table.return_value = {10: ProcessEntry(threads=30, suspended=False)}
        with mock.patch.multiple(
            overview.psutil, WINDOWS=True,
            pids=mock.Mock(return_value=list(processes)),
            Process=mock.Mock(side_effect=processes.__getitem__),
        ):
            snapshot = overview.SystemOverviewSampler().sample()

        self._thread_table.assert_called_once_with([10, 20])
        processes[10].num_threads.assert_not_called()
        processes[20].num_threads.assert_called_once_with()
        self.assertEqual(_ranked(snapshot.top_threads), [(10, 30), (20, 20)])

    def test_reused_pid_waits_for_its_own_cpu_baseline(self) -> None:
        process = _process("app", 1.0, 100, 20, 6)
        process.create_time.side_effect = [1.0, 2.0, 2.0]
        process.cpu_times.side_effect = [SimpleNamespace(user=value, system=0.0) for value in (1, 5, 6)]
        sampler = overview.SystemOverviewSampler()
        with (
            mock.patch.multiple(
                overview.psutil, pids=mock.Mock(return_value=[10]), Process=mock.Mock(return_value=process),
            ),
            mock.patch.object(overview.time, "monotonic", side_effect=[1.0, 1.0, 2.0, 2.0, 3.0, 3.0]),
        ):
            sampler.sample()
            self.assertEqual(sampler.sample().top_cpu, ())
            self.assertEqual(_ranked(sampler.sample().top_cpu), [(10, 100.0)])

    def test_stopping_a_scan_skips_the_remaining_processes(self) -> None:
        process = mock.Mock(return_value=_process("first", 1.0, 100, 20, 6))
        stop = mock.Mock(side_effect=[False, True])
        with mock.patch.multiple(
            overview.psutil, pids=mock.Mock(return_value=[10, 20]), Process=process,
        ):
            sample = overview.SystemOverviewSampler().sample(should_stop=stop)

        process.assert_called_once_with(10)
        self.assertEqual(_ranked(sample.top_memory), [(10, 100)])

    def test_host_rates_measure_elapsed_time_and_recover_from_reset_and_missing_counters(self) -> None:
        def disk(read: int, written: int) -> SimpleNamespace:
            return SimpleNamespace(read_bytes=read, write_bytes=written)

        def network(received: int, sent: int) -> SimpleNamespace:
            return SimpleNamespace(bytes_recv=received, bytes_sent=sent)

        with (
            mock.patch.multiple(
                overview.psutil, pids=mock.Mock(return_value=[]),
                disk_io_counters=mock.Mock(side_effect=[
                    disk(40, 30), disk(60, 30), OSError(), disk(5, 7), disk(11, 15),
                ]),
                net_io_counters=mock.Mock(side_effect=[
                    network(20, 90), network(22, 102), network(18, 1), network(20, 7), None,
                ]),
                swap_memory=mock.Mock(return_value=SimpleNamespace(total=500, used=125)),
            ),
            mock.patch.object(overview.time, "monotonic", side_effect=[0, 2, 3, 4, 6]),
        ):
            sampler = overview.SystemOverviewSampler()
            first, second, third, fourth, fifth = [sampler.sample() for _ in range(5)]

        self.assertEqual((first.used_swap_bytes, first.total_swap_bytes), (125, 500))
        self.assertEqual(first.disk, overview.TransferRates())
        self.assertEqual(first.network, overview.TransferRates())
        self.assertEqual(second.disk, overview.TransferRates(10, 0))
        self.assertEqual(second.network, overview.TransferRates(1, 6))
        self.assertIsNone(third.disk)
        self.assertEqual(third.network, overview.TransferRates())
        self.assertEqual(fourth.disk, overview.TransferRates())
        self.assertEqual(fourth.network, overview.TransferRates(2, 6))
        self.assertEqual(fifth.disk, overview.TransferRates(3, 4))
        self.assertIsNone(fifth.network)


if __name__ == "__main__":
    unittest.main()
