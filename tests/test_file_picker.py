"""Desktop picker selection and headless Linux fallback."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401 (adds the repository root to sys.path)

from src import file_picker


class FilePickerTests(unittest.TestCase):
    def test_linux_uses_optional_tkinter_and_never_windows_api(self) -> None:
        with (
            mock.patch.object(file_picker.sys, "platform", "linux"),
            mock.patch.object(file_picker, "_select_with_tkinter", return_value="/tmp/target") as tkinter,
            mock.patch.object(file_picker, "_select_with_ctypes") as windows,
            mock.patch.object(file_picker.os.path, "isfile", return_value=True),
        ):
            self.assertEqual(file_picker.select_target_file(), "/tmp/target")
        tkinter.assert_called_once_with()
        windows.assert_not_called()

    def test_missing_tk_or_display_provides_path_fallback(self) -> None:
        for error in (ImportError("no tkinter"), RuntimeError("no display")):
            with (
                self.subTest(error=error),
                mock.patch.object(file_picker.sys, "platform", "linux"),
                mock.patch.object(file_picker, "_select_with_tkinter", side_effect=error),
                self.assertRaisesRegex(OSError, "enter the target path"),
            ):
                file_picker.select_target_file()

    def test_linux_cancellation_is_not_an_error(self) -> None:
        with (
            mock.patch.object(file_picker.sys, "platform", "linux"),
            mock.patch.object(file_picker, "_select_with_tkinter", return_value=None),
        ):
            self.assertIsNone(file_picker.select_target_file())

    def test_linux_filters_include_extensionless_targets_and_destroy_dialog(self) -> None:
        root = mock.Mock()
        dialog = mock.Mock()
        dialog.askopenfilename.return_value = ""
        tkinter = SimpleNamespace(Tk=lambda: root, filedialog=dialog)
        with (
            mock.patch.dict("sys.modules", {"tkinter": tkinter}),
            mock.patch.object(file_picker.sys, "platform", "linux"),
        ):
            self.assertIsNone(file_picker._select_with_tkinter())
        options = dialog.askopenfilename.call_args.kwargs
        self.assertIn(("Executables and Scripts", "*"), options["filetypes"])
        self.assertEqual(options["defaultextension"], "")
        root.destroy.assert_called_once_with()

    def test_dialog_is_destroyed_if_window_setup_fails(self) -> None:
        root = mock.Mock()
        root.attributes.side_effect = RuntimeError("no window manager")
        tkinter = SimpleNamespace(Tk=lambda: root, filedialog=mock.Mock())
        with (
            mock.patch.dict("sys.modules", {"tkinter": tkinter}),
            self.assertRaises(RuntimeError),
        ):
            file_picker._select_with_tkinter()
        root.destroy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
