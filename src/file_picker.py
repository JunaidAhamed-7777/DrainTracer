"""Native Windows file picker for selecting a target executable or script."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Filter pairs: (description, pattern) for GetOpenFileNameW or tkinter.
_EXECUTABLE_FILTERS: tuple[tuple[str, str], ...] = (
    ("Executables", "*.exe"),
    ("Batch Files", "*.bat;*.cmd"),
    ("Python Scripts", "*.py"),
    ("All Files", "*.*"),
)


def _filters_to_tkinter_string() -> str:
    parts: list[str] = []
    for description, pattern in _EXECUTABLE_FILTERS:
        parts.append(f"{description} ({pattern})")
        parts.append(pattern)
    return "\n".join(parts)


def _select_with_tkinter() -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    try:
        selected = filedialog.askopenfilename(
            title="Select target executable or script",
            filetypes=[
                (description, pattern.replace(";", " "))
                for description, pattern in _EXECUTABLE_FILTERS
            ],
            defaultextension=".exe",
        )
    finally:
        root.destroy()

    if not selected:
        return None
    return str(Path(selected).resolve())


def _select_with_ctypes() -> str | None:
    """Use GetOpenFileNameW via comdlg32 for a native Windows dialog."""
    import ctypes
    from ctypes import wintypes

    comdlg32 = ctypes.windll.comdlg32
    ole32 = ctypes.windll.ole32

    # Build filter string: "Desc\0pattern\0...\0\0"
    filter_parts: list[str] = []
    for description, pattern in _EXECUTABLE_FILTERS:
        filter_parts.append(description)
        filter_parts.append(pattern)
    filter_buffer = "\0".join(filter_parts) + "\0\0"

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    OFN_FILEMUSTEXIST = 0x00001000
    OFN_PATHMUSTEXIST = 0x00000800
    OFN_EXPLORER = 0x00080000
    OFN_NOCHANGEDIR = 0x00000008

    file_buffer = ctypes.create_unicode_buffer(260)
    title_buffer = ctypes.create_unicode_buffer(260)

    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = filter_buffer
    ofn.nFilterIndex = 1
    ofn.lpstrFile = file_buffer
    ofn.nMaxFile = len(file_buffer)
    ofn.lpstrFileTitle = title_buffer
    ofn.nMaxFileTitle = len(title_buffer)
    ofn.lpstrTitle = "Select target executable or script"
    ofn.lpstrDefExt = "exe"
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_EXPLORER | OFN_NOCHANGEDIR

    ole32.CoInitialize(None)
    try:
        if not comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return None
    finally:
        ole32.CoUninitialize()

    selected = file_buffer.value.strip()
    if not selected:
        return None
    return str(Path(selected).resolve())


def select_target_file(prefer_ctypes: bool = True) -> str | None:
    """
    Open a native Windows file picker filtered for executables and scripts.

    Returns the absolute path of the selected file, or None if canceled.
    Raises FileNotFoundError if the platform is not Windows.
    """
    if sys.platform != "win32":
        raise FileNotFoundError("Native Windows file picker is only available on Windows.")

    selectors = (
        (_select_with_ctypes, prefer_ctypes),
        (_select_with_tkinter, not prefer_ctypes),
    )

    for selector, _ in sorted(selectors, key=lambda item: not item[1]):
        try:
            path = selector()
            if path is None:
                return None
            if os.path.isfile(path):
                return path
            raise FileNotFoundError(f"Selected path does not exist: {path}")
        except OSError:
            continue

    return _select_with_tkinter()
