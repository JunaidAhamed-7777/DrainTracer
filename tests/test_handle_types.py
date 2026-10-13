"""Handle-type classification: parsers, categories, and live Windows handles."""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (adds the repository root to sys.path)

from src.handle_types import (
    CATEGORIES,
    HandleClassifier,
    _handle_columns,
    _ObjectTypeInformation,
    _parse_type_names,
    describe_type_growth,
    total_counts,
)

POINTER = ctypes.sizeof(ctypes.c_void_p)


def _table(entries: list[tuple[int, int]]) -> memoryview:
    """Build a SYSTEM_HANDLE_INFORMATION_EX buffer of (pid, type_index) entries."""
    entry_size = 40 if POINTER == 8 else 28
    data = bytearray(2 * POINTER + entry_size * len(entries))
    data[:POINTER] = len(entries).to_bytes(POINTER, "little")
    for index, (pid, type_index) in enumerate(entries):
        base = 2 * POINTER + index * entry_size
        data[base + POINTER : base + 2 * POINTER] = pid.to_bytes(POINTER, "little")
        type_offset = base + 3 * POINTER + 6
        data[type_offset : type_offset + 2] = type_index.to_bytes(2, "little")
    return memoryview(bytes(data))


def _type_list(names: list[tuple[int, str]], corrupt: int | None = None) -> tuple[bytes, int]:
    """Build an OBJECT_TYPES_INFORMATION buffer; returns (raw, base address)."""
    record = ctypes.sizeof(_ObjectTypeInformation)

    def align(value: int) -> int:
        return (value + POINTER - 1) & ~(POINTER - 1)

    size = align(4) + sum(record + align(len(name) * 2 + 2) for _, name in names)
    buffer = ctypes.create_string_buffer(size)
    base = ctypes.addressof(buffer)
    ctypes.memmove(base, len(names).to_bytes(4, "little"), 4)
    offset = align(4)
    for position, (type_index, name) in enumerate(names):
        encoded = name.encode("utf-16-le")
        info = _ObjectTypeInformation()
        info.TypeName.Length = len(encoded)
        info.TypeName.MaximumLength = len(encoded) + 2
        info.TypeName.Buffer = base + offset + record if position != corrupt else 0x10
        info.TypeIndex = type_index
        ctypes.memmove(base + offset, ctypes.addressof(info), record)
        ctypes.memmove(base + offset + record, encoded, len(encoded))
        offset += record + align(len(encoded) + 2)
    return buffer.raw, base


class ParserTests(unittest.TestCase):
    def test_handle_columns_read_pid_and_type(self) -> None:
        pids, types = _handle_columns(_table([(4, 37), (1234, 40), (1234, 37)]))
        self.assertEqual(list(zip(pids, types)), [(4, 37), (1234, 40), (1234, 37)])

    def test_truncated_handle_table_is_rejected(self) -> None:
        table = bytearray(_table([(1, 2), (3, 4)]))
        table[:POINTER] = (50).to_bytes(POINTER, "little")
        with self.assertRaises(ValueError):
            _handle_columns(memoryview(bytes(table)))

    def test_type_names_follow_type_indexes(self) -> None:
        raw, base = _type_list([(37, "File"), (44, "Key"), (16, "Event")])
        self.assertEqual(_parse_type_names(raw, base), {37: "File", 44: "Key", 16: "Event"})

    def test_names_outside_the_buffer_are_skipped(self) -> None:
        raw, base = _type_list([(37, "File"), (44, "Key")], corrupt=0)
        self.assertEqual(_parse_type_names(raw, base), {44: "Key"})


class CategoryTests(unittest.TestCase):
    def test_growth_description_names_the_top_risers(self) -> None:
        self.assertEqual(
            describe_type_growth({"event": 8, "file": 390, "socket": 8, "thread": -2}),
            "by type: file +390, socket +8",
        )
        self.assertEqual(describe_type_growth({"file": 1200}, limit=1), "by type: file +1,200")
        self.assertEqual(describe_type_growth({"thread": -3}), "")

    def test_totals_cover_every_category(self) -> None:
        totals = total_counts({1: {"file": 2, "socket": 1}, 2: {"file": 3, "other": 4}})
        self.assertEqual(list(totals), list(CATEGORIES))
        self.assertEqual((totals["file"], totals["socket"], totals["other"]), (5, 1, 4))

    def test_classifier_gives_up_after_repeated_failures(self) -> None:
        classifier = HandleClassifier()
        classifier._enabled = True
        broken = mock.Mock()
        broken.handle_table.side_effect = OSError("denied")
        classifier._api = broken
        for _ in range(3):
            self.assertIsNone(classifier.classify([1]))
        self.assertFalse(classifier.available)
        self.assertEqual(classifier.last_error, "denied")

    def test_unavailable_classifier_returns_none(self) -> None:
        classifier = HandleClassifier()
        classifier._enabled = False
        self.assertIsNone(classifier.classify([os.getpid()]))


@unittest.skipUnless(sys.platform == "win32", "handle classification is Windows-only")
class LiveHandleTests(unittest.TestCase):
    def test_files_sockets_and_registry_keys_are_classified(self) -> None:
        import winreg

        classifier = HandleClassifier()
        pid = os.getpid()
        before = classifier.classify([pid])
        self.assertIsNotNone(before, classifier.last_error)

        with tempfile.TemporaryDirectory() as temp:
            files = [open(os.path.join(temp, f"held{index}.txt"), "w") for index in range(12)]
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(8)
            clients = [socket.create_connection(server.getsockname()) for _ in range(4)]
            accepted = [server.accept()[0] for _ in range(4)]
            keys = [winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software") for _ in range(5)]
            try:
                after = classifier.classify([pid])
            finally:
                for item in [*files, *clients, *accepted, server]:
                    item.close()
                for key in keys:
                    key.Close()

        growth = {category: after[pid][category] - before[pid][category] for category in CATEGORIES}
        self.assertGreaterEqual(growth["file"], 12)
        self.assertEqual(growth["socket"], 9)  # listener + 4 clients + 4 accepted
        self.assertGreaterEqual(growth["registry"], 5)

    def test_unknown_pids_count_nothing(self) -> None:
        classifier = HandleClassifier()
        result = classifier.classify([0x7FFFFFF0])
        self.assertIsNotNone(result, classifier.last_error)
        self.assertEqual(sum(result[0x7FFFFFF0].values()), 0)


if __name__ == "__main__":
    unittest.main()
