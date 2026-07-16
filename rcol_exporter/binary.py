from __future__ import annotations

import hashlib
import math
import struct
import uuid
from pathlib import Path
from typing import Any


RCOL_MAGIC = b"RCOL"
RSZ_MAGIC = 0x005A5352
HEADER_SIZE = 0x70


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def s32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def f32(data: bytes, offset: int) -> float:
    return struct.unpack_from("<f", data, offset)[0]


def f32_word(value: int) -> float | None:
    number = struct.unpack("<f", struct.pack("<I", value & 0xFFFFFFFF))[0]
    if not math.isfinite(number) or abs(number) > 1.0e20:
        return None
    return round(number, 6)


def hx(value: int | None) -> str | None:
    if value is None:
        return None
    return f"0x{value:x}"


def guid_from_bytes(raw: bytes) -> str:
    if len(raw) != 16:
        return raw.hex()
    try:
        return str(uuid.UUID(bytes_le=raw))
    except ValueError:
        return raw.hex()


def printable_utf16_codepoint(value: int) -> bool:
    return value in (0x09, 0x0A, 0x0D) or 0x20 <= value <= 0xD7FF or 0xE000 <= value <= 0xFFFD


def read_wstring(data: bytes, offset: int) -> str:
    if offset <= 0 or offset + 1 >= len(data) or offset % 2:
        return ""
    chars: list[int] = []
    pos = offset
    while pos + 1 < len(data):
        value = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if value == 0:
            break
        if not printable_utf16_codepoint(value):
            return ""
        chars.append(value)
        if len(chars) > 4096:
            return ""
    return "".join(chr(ch) for ch in chars)


def scan_string_table(data: bytes, start: int) -> list[dict[str, Any]]:
    if start <= 0 or start >= len(data):
        return []
    strings: list[dict[str, Any]] = []
    pos = start + (start % 2)
    while pos + 1 < len(data):
        value = struct.unpack_from("<H", data, pos)[0]
        if value == 0:
            pos += 2
            continue
        text_start = pos
        chars: list[int] = []
        valid = True
        while pos + 1 < len(data):
            value = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if value == 0:
                break
            if not printable_utf16_codepoint(value):
                valid = False
                break
            chars.append(value)
        if valid and chars:
            strings.append({"offset": hx(text_start), "text": "".join(chr(ch) for ch in chars)})
        else:
            pos = text_start + 2
    return strings


def collect_string_offsets(data: bytes, start: int, end: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for pos in range(start, max(start, min(end, len(data)) - 7), 4):
        value = u64(data, pos)
        if value <= 0 or value >= len(data) or value % 2:
            continue
        text = read_wstring(data, value)
        if not text:
            continue
        key = (pos - start, value)
        if key in seen:
            continue
        seen.add(key)
        out.append({"field_offset": hx(pos - start), "target_offset": hx(value), "text": text})
    return out


def section_summary(data: bytes, start: int, end: int, preview: int = 128) -> dict[str, Any]:
    start = max(0, min(start, len(data)))
    end = max(start, min(end, len(data)))
    chunk = data[start:end]
    return {
        "start": hx(start),
        "end": hx(end),
        "size": len(chunk),
        "sha256": hashlib.sha256(chunk).hexdigest(),
        "hex_preview": chunk[:preview].hex(),
        "hex_truncated": len(chunk) > preview,
        "possible_string_offsets": collect_string_offsets(data, start, end),
    }


def display_path(path: str | Path | None) -> str | None:
    if path is None:
        return None
    value = Path(path)
    try:
        if value.is_absolute():
            return value.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(value)
    return value.as_posix()
