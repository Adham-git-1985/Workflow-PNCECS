"""Build an OLE ``Package`` object for embedding arbitrary files in Word.

The compound-file writer below is adapted from msgforge's pure-Python CFB
writer (https://pypi.org/project/msgforge/) so meeting minutes can be built on
an intranet server without Microsoft Word or COM automation.

MIT License

Copyright (c) 2026 j

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field


_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SECTOR_SIZE = 512
_MINI_SECTOR_SIZE = 64
_MINI_STREAM_CUTOFF = 0x1000
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE
_FATSECT = 0xFFFFFFFD
_DIFSECT = 0xFFFFFFFC
_NOSTREAM = 0xFFFFFFFF
_DIR_ENTRY_SIZE = 128
_ENTRIES_PER_FAT_SECTOR = _SECTOR_SIZE // 4
_DIFAT_HEADER_ENTRIES = 109
_ENTRIES_PER_DIFAT_SECTOR = _ENTRIES_PER_FAT_SECTOR - 1

_STGTY_STORAGE = 1
_STGTY_STREAM = 2
_STGTY_ROOT = 5
_RED = 0
_BLACK = 1

_PACKAGE_CLSID = uuid.UUID("0003000c-0000-0000-c000-000000000046").bytes_le
_COMPOBJ_STREAM = bytes.fromhex(
    "0100feff030a0000ffffffff0c00030000000000c000000000000046"
    "0c0000004f4c45205061636b6167650000000000080000005061636b"
    "61676500f439b271000000000000000000000000"
)
_OBJINFO_STREAM = b"\x00\x00\x03\x00\x0d\x00"


@dataclass
class _DirEntry:
    name: str
    entry_type: int
    data: bytes = b""
    children: list["_DirEntry"] = field(default_factory=list)
    clsid: bytes = b"\x00" * 16
    dir_id: int = 0
    left_id: int = _NOSTREAM
    right_id: int = _NOSTREAM
    child_id: int = _NOSTREAM
    color: int = _RED
    start_sector: int = _ENDOFCHAIN
    data_size: int = 0


class _OleWriter:
    """Write an MS-CFB v3 compound file using 512-byte sectors."""

    def __init__(self) -> None:
        self.root = _DirEntry("Root Entry", _STGTY_ROOT, clsid=_PACKAGE_CLSID)

    def add_stream(self, name: str, data: bytes) -> None:
        entry = _DirEntry(name, _STGTY_STREAM, bytes(data), data_size=len(data))
        self.root.children.append(entry)

    def build(self) -> bytes:
        entries = self._flatten_sorted()
        large = [
            entry for entry in entries
            if entry.entry_type == _STGTY_STREAM and entry.data
            and len(entry.data) >= _MINI_STREAM_CUTOFF
        ]
        small = [
            entry for entry in entries
            if entry.entry_type == _STGTY_STREAM and entry.data
            and len(entry.data) < _MINI_STREAM_CUTOFF
        ]

        mini_size = sum((len(entry.data) + 63) >> 6 for entry in small)
        fat_size = sum((len(entry.data) + 511) >> 9 for entry in large)
        directory_sector_count = (len(entries) + 3) >> 2
        mini_fat_sector_count = (mini_size + 127) >> 7
        mini_container_sector_count = (mini_size + 7) >> 3

        fat_base = (
            fat_size
            + directory_sector_count
            + mini_fat_sector_count
            + mini_container_sector_count
        )
        fat_sector_count = 0
        difat_sector_count = 0
        while True:
            total = fat_base + fat_sector_count + difat_sector_count
            needed_fat = (total + _ENTRIES_PER_FAT_SECTOR - 1) >> 7
            needed_difat = (
                0
                if needed_fat <= _DIFAT_HEADER_ENTRIES
                else (
                    needed_fat
                    - _DIFAT_HEADER_ENTRIES
                    + _ENTRIES_PER_DIFAT_SECTOR
                    - 1
                )
                // _ENTRIES_PER_DIFAT_SECTOR
            )
            if needed_fat == fat_sector_count and needed_difat == difat_sector_count:
                break
            fat_sector_count, difat_sector_count = needed_fat, needed_difat

        total_sectors = (
            difat_sector_count
            + fat_sector_count
            + mini_fat_sector_count
            + directory_sector_count
            + fat_size
            + mini_container_sector_count
        )

        fat = [_DIFSECT] * difat_sector_count + [_FATSECT] * fat_sector_count
        sector_index = difat_sector_count + fat_sector_count

        def add_chain(count: int) -> None:
            nonlocal sector_index
            for _ in range(count - 1):
                fat.append(sector_index + 1)
                sector_index += 1
            if count:
                fat.append(_ENDOFCHAIN)
                sector_index += 1

        add_chain(mini_fat_sector_count)
        directory_start = sector_index
        add_chain(directory_sector_count)

        for entry in large:
            entry.start_sector = sector_index
            add_chain((len(entry.data) + 511) >> 9)

        mini_container_start = (
            sector_index if mini_container_sector_count else _ENDOFCHAIN
        )
        add_chain(mini_container_sector_count)
        self.root.start_sector = mini_container_start if mini_size else _ENDOFCHAIN
        self.root.data_size = mini_size * _MINI_SECTOR_SIZE if mini_size else 0

        mini_fat: list[int] = []
        mini_index = 0
        for entry in small:
            entry.start_sector = mini_index
            mini_count = (len(entry.data) + 63) >> 6
            for _ in range(mini_count - 1):
                mini_fat.append(mini_index + 1)
                mini_index += 1
            mini_fat.append(_ENDOFCHAIN)
            mini_index += 1

        while len(fat) < fat_sector_count * _ENTRIES_PER_FAT_SECTOR:
            fat.append(_FREESECT)

        output = bytearray(_SECTOR_SIZE * (1 + total_sectors))
        self._write_header(
            output,
            fat_sector_count,
            difat_sector_count,
            directory_start,
            mini_fat_sector_count,
        )

        for index in range(difat_sector_count):
            offset = _SECTOR_SIZE * (1 + index)
            for item_index in range(_ENTRIES_PER_DIFAT_SECTOR):
                fat_index = (
                    _DIFAT_HEADER_ENTRIES
                    + index * _ENTRIES_PER_DIFAT_SECTOR
                    + item_index
                )
                value = (
                    difat_sector_count + fat_index
                    if fat_index < fat_sector_count
                    else _FREESECT
                )
                struct.pack_into("<I", output, offset + item_index * 4, value)
            next_difat = index + 1 if index + 1 < difat_sector_count else _ENDOFCHAIN
            struct.pack_into("<I", output, offset + _SECTOR_SIZE - 4, next_difat)

        for index in range(fat_sector_count):
            offset = _SECTOR_SIZE * (1 + difat_sector_count + index)
            for item_index in range(_ENTRIES_PER_FAT_SECTOR):
                value = fat[index * _ENTRIES_PER_FAT_SECTOR + item_index]
                struct.pack_into("<I", output, offset + item_index * 4, value & 0xFFFFFFFF)

        mini_fat_offset = _SECTOR_SIZE * (
            1 + difat_sector_count + fat_sector_count
        )
        for index, value in enumerate(mini_fat):
            struct.pack_into("<I", output, mini_fat_offset + index * 4, value)
        for index in range(
            len(mini_fat), mini_fat_sector_count * _ENTRIES_PER_FAT_SECTOR
        ):
            struct.pack_into("<I", output, mini_fat_offset + index * 4, _FREESECT)

        directory_offset = _SECTOR_SIZE * (
            1 + difat_sector_count + fat_sector_count + mini_fat_sector_count
        )
        for index, entry in enumerate(entries):
            self._write_directory_entry(
                output,
                directory_offset + index * _DIR_ENTRY_SIZE,
                entry,
            )
        for index in range(len(entries), directory_sector_count * 4):
            offset = directory_offset + index * _DIR_ENTRY_SIZE
            struct.pack_into("<I", output, offset + 68, _NOSTREAM)
            struct.pack_into("<I", output, offset + 72, _NOSTREAM)
            struct.pack_into("<I", output, offset + 76, _NOSTREAM)

        data_base = (
            difat_sector_count
            + fat_sector_count
            + mini_fat_sector_count
            + directory_sector_count
        )
        large_offset = _SECTOR_SIZE * (1 + data_base)
        for entry in large:
            offset = large_offset + (entry.start_sector - data_base) * _SECTOR_SIZE
            output[offset:offset + len(entry.data)] = entry.data

        mini_offset = _SECTOR_SIZE * (1 + data_base + fat_size)
        for entry in small:
            offset = mini_offset + entry.start_sector * _MINI_SECTOR_SIZE
            output[offset:offset + len(entry.data)] = entry.data

        return bytes(output)

    def _flatten_sorted(self) -> list[_DirEntry]:
        children = sorted(
            self.root.children,
            key=lambda entry: (len(entry.name), entry.name.upper()),
        )
        entries = [self.root, *children]
        for index, entry in enumerate(entries):
            entry.dir_id = index
            entry.left_id = _NOSTREAM
            entry.right_id = _NOSTREAM
            entry.child_id = _NOSTREAM
            entry.color = _BLACK

        def build_tree(indices: list[int], depth: int = 0, black_depth: int = -1) -> int:
            if not indices:
                return _NOSTREAM
            if black_depth < 0:
                black_depth = len(indices).bit_length() - 1
            middle = len(indices) // 2
            root_index = indices[middle]
            entries[root_index].color = _RED if depth >= black_depth else _BLACK
            entries[root_index].left_id = build_tree(
                indices[:middle], depth + 1, black_depth
            )
            entries[root_index].right_id = build_tree(
                indices[middle + 1:], depth + 1, black_depth
            )
            return root_index

        if children:
            self.root.child_id = build_tree(list(range(1, len(entries))))
        return entries

    @staticmethod
    def _write_directory_entry(
        output: bytearray,
        offset: int,
        entry: _DirEntry,
    ) -> None:
        name_bytes = entry.name.encode("utf-16-le")[:62]
        output[offset:offset + len(name_bytes)] = name_bytes
        output[offset + len(name_bytes):offset + len(name_bytes) + 2] = b"\x00\x00"
        struct.pack_into("<H", output, offset + 64, len(name_bytes) + 2)
        output[offset + 66] = entry.entry_type
        output[offset + 67] = _BLACK if entry.entry_type == _STGTY_ROOT else entry.color
        struct.pack_into("<I", output, offset + 68, entry.left_id)
        struct.pack_into("<I", output, offset + 72, entry.right_id)
        struct.pack_into("<I", output, offset + 76, entry.child_id)
        output[offset + 80:offset + 96] = entry.clsid
        struct.pack_into("<I", output, offset + 116, entry.start_sector)
        struct.pack_into("<I", output, offset + 120, entry.data_size)

    @staticmethod
    def _write_header(
        output: bytearray,
        fat_sector_count: int,
        difat_sector_count: int,
        directory_start: int,
        mini_fat_sector_count: int,
    ) -> None:
        output[0:8] = _MAGIC
        struct.pack_into("<H", output, 24, 0x003E)
        struct.pack_into("<H", output, 26, 0x0003)
        struct.pack_into("<H", output, 28, 0xFFFE)
        struct.pack_into("<H", output, 30, 9)
        struct.pack_into("<H", output, 32, 6)
        struct.pack_into("<I", output, 44, fat_sector_count)
        struct.pack_into("<I", output, 48, directory_start)
        struct.pack_into("<I", output, 56, _MINI_STREAM_CUTOFF)
        struct.pack_into(
            "<I",
            output,
            60,
            difat_sector_count + fat_sector_count
            if mini_fat_sector_count
            else _ENDOFCHAIN,
        )
        struct.pack_into("<I", output, 64, mini_fat_sector_count)
        struct.pack_into(
            "<I", output, 68, 0 if difat_sector_count else _ENDOFCHAIN
        )
        struct.pack_into("<I", output, 72, difat_sector_count)
        for index in range(_DIFAT_HEADER_ENTRIES):
            value = (
                difat_sector_count + index
                if index < fat_sector_count
                else _FREESECT
            )
            struct.pack_into("<I", output, 76 + index * 4, value)


def _ansi(value: str) -> bytes:
    """Encode legacy OLE labels without exposing server filesystem paths."""

    return value.encode("cp1252", errors="replace")


def _unicode_value(value: str) -> bytes:
    encoded = value.encode("utf-16-le")
    return struct.pack("<I", len(value)) + encoded


def _ole_native_stream(filename: str, payload: bytes) -> bytes:
    safe_name = str(filename or "attachment.bin").replace("\x00", "")
    source_path = f"C:\\Embedded\\{safe_name}"
    temporary_path = f"C:\\Temp\\{safe_name}"

    body = bytearray()
    body.extend(struct.pack("<H", 2))
    body.extend(_ansi(safe_name) + b"\x00")
    body.extend(_ansi(source_path) + b"\x00")
    body.extend(struct.pack("<II", 0x00030000, len(temporary_path) + 1))
    body.extend(_ansi(temporary_path) + b"\x00")
    body.extend(struct.pack("<I", len(payload)))
    body.extend(payload)
    body.extend(_unicode_value(temporary_path))
    body.extend(_unicode_value(safe_name))
    body.extend(_unicode_value(source_path))
    return struct.pack("<I", len(body)) + bytes(body)


def build_ole_package(filename: str, payload: bytes) -> bytes:
    """Return a Word-compatible OLE ``Package`` containing *payload*."""

    writer = _OleWriter()
    writer.add_stream("\x01CompObj", _COMPOBJ_STREAM)
    writer.add_stream("\x01Ole10Native", _ole_native_stream(filename, bytes(payload)))
    writer.add_stream("\x03ObjInfo", _OBJINFO_STREAM)
    return writer.build()
