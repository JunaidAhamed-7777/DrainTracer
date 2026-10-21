"""Running-program inventory and selection identity checks."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401 (adds the repository root to sys.path)
import psutil

from src import running_processes as inventory


def _process(pid: int, name: str | None, created: float | None = 10.0):
    return SimpleNamespace(info={"pid": pid, "name": name, "create_time": created})


class InventoryTests(unittest.TestCase):
    def test_names_sort_case_insensitively_then_by_pid_and_exclude_idle_and_self(self) -> None:
        processes = [_process(5, "z.exe"), _process(4, "App.exe"), _process(3, "app.exe"),
                     _process(0, "Idle"), _process(99, "DrainTracer")]
        with (
            mock.patch.object(inventory.os, "getpid", return_value=99),
            mock.patch.object(inventory.psutil, "process_iter", return_value=processes) as scan,
        ):
            result = inventory.list_running_processes()
        self.assertEqual([(row.pid, row.name) for row in result],
                         [(3, "app.exe"), (4, "App.exe"), (5, "z.exe")])
        scan.assert_called_once_with(["pid", "name", "create_time"], ad_value=None)

    def test_unavailable_names_remain_visible_and_exited_rows_are_skipped(self) -> None:
        gone = mock.Mock()
        type(gone).info = mock.PropertyMock(side_effect=psutil.NoSuchProcess(7))
        denied = mock.Mock()
        type(denied).info = mock.PropertyMock(side_effect=psutil.AccessDenied(8))
        with mock.patch.object(inventory.psutil, "process_iter", return_value=[
            gone, denied, _process(6, None, None), _process(5, "", 20.0),
        ]):
            result = inventory.list_running_processes()
        self.assertEqual([(row.pid, row.name) for row in result],
                         [(5, "Unknown program"), (6, "Unknown program")])
        self.assertIsNone(result[1].create_time)

    def test_global_enumeration_failure_is_not_an_empty_list(self) -> None:
        def fail_during_iteration():
            yield _process(3, "first.exe")
            raise OSError("process enumeration unavailable")

        with (
            mock.patch.object(inventory.psutil, "process_iter", return_value=fail_during_iteration()),
            self.assertRaisesRegex(OSError, "enumeration unavailable"),
        ):
            inventory.list_running_processes()


class IdentityTests(unittest.TestCase):
    def test_current_identity_is_accepted_without_controlling_the_process(self) -> None:
        with mock.patch.object(inventory.psutil, "Process") as process:
            process.return_value.create_time.return_value = 10.0
            process.return_value.is_running.return_value = True
            self.assertTrue(inventory.is_process_current(inventory.RunningProcess(3, "app", 10.0)))
        self.assertEqual(process.return_value.method_calls,
                         [mock.call.create_time(), mock.call.is_running()])

    def test_reused_missing_or_inaccessible_identity_is_rejected(self) -> None:
        row = inventory.RunningProcess(3, "app", 10.0)
        with mock.patch.object(inventory.psutil, "Process") as process:
            process.return_value.create_time.return_value = 11.0
            self.assertFalse(inventory.is_process_current(row))
        for error in (psutil.NoSuchProcess(3), psutil.AccessDenied(3), OSError("unavailable")):
            with (
                self.subTest(error=error),
                mock.patch.object(inventory.psutil, "Process", side_effect=error),
            ):
                self.assertFalse(inventory.is_process_current(row))
        with mock.patch.object(inventory.psutil, "Process") as process:
            self.assertFalse(inventory.is_process_current(inventory.RunningProcess(3, "app", None)))
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
