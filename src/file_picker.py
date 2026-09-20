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

    # The dialog may return long Windows paths; keep the buffers larger than
    # MAX_PATH and pass their addresses explicitly for Python 3.14+ ctypes.
    file_buffer = ctypes.create_unicode_buffer(32768)
    title_buffer = ctypes.create_unicode_buffer(512)

    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = filter_buffer
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(file_buffer, wintypes.LPWSTR)
    ofn.nMaxFile = len(file_buffer)
    ofn.lpstrFileTitle = ctypes.cast(title_buffer, wintypes.LPWSTR)
    ofn.nMaxFileTitle = len(title_buffer)
    ofn.lpstrTitle = "Select target executable or script"
    ofn.lpstrDefExt = "exe"
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_EXPLORER | OFN_NOCHANGEDIR

    get_open_file_name = comdlg32.GetOpenFileNameW
    get_open_file_name.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    get_open_file_name.restype = wintypes.BOOL
    get_extended_error = comdlg32.CommDlgExtendedError
    get_extended_error.argtypes = []
    get_extended_error.restype = wintypes.DWORD

    co_initialize_ex = ole32.CoInitializeEx
    co_initialize_ex.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    co_initialize_ex.restype = wintypes.HRESULT
    co_uninitialize = ole32.CoUninitialize
    co_uninitialize.argtypes = []
    co_uninitialize.restype = None

    # COINIT_APARTMENTTHREADED; CoInitializeEx may report that the current
    # thread is already in another apartment. In that case, do not unbalance
    # COM with a matching CoUninitialize call.
    COINIT_APARTMENTTHREADED = 0x2
    S_OK = 0
    S_FALSE = 1
    com_result = co_initialize_ex(None, COINIT_APARTMENTTHREADED)
    com_initialized = com_result in (S_OK, S_FALSE)
    try:
        if not get_open_file_name(ctypes.byref(ofn)):
            error_code = get_extended_error()
            if error_code == 0:
                return None
            raise OSError(
                int(error_code),
                f"GetOpenFileNameW failed with error 0x{error_code:08X}",
            )
    finally:
        if com_initialized:
            co_uninitialize()

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

    errors: list[Exception] = []
    for selector, _ in sorted(selectors, key=lambda item: not item[1]):
        try:
            path = selector()
            if path is None:
                return None
            if os.path.isfile(path):
                return path
            raise FileNotFoundError(f"Selected path does not exist: {path}")
        except Exception as exc:
            # ctypes and tkinter expose platform-specific exception types.
            # A failed native dialog should fall through to the other picker.
            errors.append(exc)
            continue

    if errors:
        raise OSError("All Windows file picker implementations failed") from errors[-1]
    return None
