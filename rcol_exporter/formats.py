from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .binary import display_path


READABLE_FORMAT = "rcol_readable_v3"
REPACK_FORMAT = "rcol_repack_v3"


def strip_private(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_private(child)
            for key, child in value.items()
            if key not in {"_raw", "_object_table_indices"}
        }
    if isinstance(value, list):
        return [strip_private(item) for item in value]
    return value


def build_readable(parsed: dict[str, Any], schema_path: Path, il2cpp_path: Path | None) -> dict[str, Any]:
    if parsed.get("error"):
        return {
            "source": parsed.get("source"),
            "error": parsed.get("error"),
        }
    return {
        "groupInfos": strip_private(parsed.get("groupInfos", [])),
        "requestSets": strip_private(parsed.get("requestSets", [])),
        "ignoreTags": strip_private(parsed.get("ignoreTags", [])),
        "_diagnostics": {
            "rcol_layout": parsed.get("rcol_layout", {}),
            "rsz": (parsed.get("rsz") or {}).get("_diagnostics", {}),
        },
    }


def build_repack(parsed: dict[str, Any], schema_path: Path, il2cpp_path: Path | None) -> dict[str, Any]:
    raw = parsed.get("raw_bytes", b"")
    if not isinstance(raw, (bytes, bytearray)):
        raw = b""
    return {
        "_format": REPACK_FORMAT,
        "_version": 3,
        "_source": {
            "path": parsed.get("source"),
            "file_name": parsed.get("file_name"),
            "file_size": parsed.get("file_size"),
            "sha256": parsed.get("sha256"),
            "schema": display_path(schema_path),
            "il2cpp_dump": display_path(il2cpp_path),
        },
        "_binary": {
            "encoding": "hex",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data": bytes(raw).hex(),
        },
        "header": parsed.get("header", {}),
        "groupInfos": parsed.get("groupInfos", []),
        "requestSets": parsed.get("requestSets", []),
        "ignoreTags": parsed.get("ignoreTags", []),
        "strings": parsed.get("strings", []),
        "rsz": parsed.get("rsz", {}),
        "rcol_layout": parsed.get("rcol_layout", {}),
        "_raw": parsed.get("_raw", {}),
        "error": parsed.get("error"),
    }
