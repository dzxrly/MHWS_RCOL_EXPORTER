from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, Sequence

from pyreuser3.schema import TypeDB

from .binary import display_path
from .detect import build_layout_hint, detect_rcol_layout
from .formats import build_readable, build_readable_debug, build_repack
from .rcol import parse_rcol


JsonFormat = Literal["readable", "readable_debug", "repack"]
RCOL_RE = re.compile(r"\.rcol\.\d+$", re.IGNORECASE)


def discover_rcol_files(path: str | Path) -> list[Path]:
    source = Path(path)
    if source.is_file():
        return [source] if RCOL_RE.search(source.name) else []
    if not source.is_dir():
        raise FileNotFoundError(source)
    return sorted(p for p in source.rglob("*") if p.is_file() and RCOL_RE.search(p.name))


def candidates_near(input_path: str | Path, name: str) -> list[Path]:
    source = Path(input_path)
    search_from = source if source.is_dir() else source.parent
    candidates = [parent / name for parent in [search_from, *search_from.parents]]
    candidates.extend([Path.cwd() / name, Path.cwd() / "debug" / name])
    return candidates


def find_schema(input_path: str | Path, explicit: str | Path | None = None) -> Path:
    if explicit:
        schema = Path(explicit)
        if not schema.is_file():
            raise FileNotFoundError(f"schema file not found: {schema}")
        return schema
    for candidate in candidates_near(input_path, "rszmhws.json"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("rszmhws.json not found. Pass it with --schema-path.")


def find_il2cpp_dump(input_path: str | Path, explicit: str | Path | None = None) -> Path | None:
    if explicit:
        dump = Path(explicit)
        if not dump.is_file():
            raise FileNotFoundError(f"il2cpp dump not found: {dump}")
        return dump
    for candidate in candidates_near(input_path, "il2cpp_dump.json"):
        if candidate.is_file():
            return candidate
    return None


def normalize_json_format(json_format: str) -> JsonFormat:
    normalized = json_format.strip().lower().replace("-", "_")
    if normalized not in {"readable", "readable_debug", "repack"}:
        raise ValueError("json_format must be 'readable', 'readable-debug', or 'repack'")
    return normalized  # type: ignore[return-value]


def normalize_json_formats(json_format: str | Sequence[str]) -> list[JsonFormat]:
    if isinstance(json_format, str):
        if json_format.strip().lower() == "both":
            return ["readable", "repack"]
        return [normalize_json_format(json_format)]
    return [normalize_json_format(item) for item in json_format]


def default_output_path(
    source: Path,
    input_root: Path,
    output_root: Path | None,
    json_format: str,
    split_formats: bool,
) -> Path:
    format_label = json_format.replace("_", "-")
    output_name = f"{source.name}.{format_label}.json" if split_formats else f"{source.name}.json"
    if output_root is None:
        return source.with_name(output_name)
    relative_parent = source.relative_to(input_root).parent if input_root.is_dir() else Path()
    return output_root / relative_parent / output_name


class RCOLConverter:
    def __init__(
        self,
        schema_path: str | Path,
        il2cpp_dump_path: str | Path | None = None,
    ) -> None:
        self.schema_path = Path(schema_path)
        self.il2cpp_dump_path = Path(il2cpp_dump_path) if il2cpp_dump_path else None
        self.typedb = TypeDB.load(self.schema_path)

    def rcol_to_json(self, rcol_path: str | Path, json_format: JsonFormat | str = "readable") -> dict[str, Any]:
        source = Path(rcol_path)
        normalized_format = normalize_json_format(str(json_format))
        parsed = parse_rcol(source, self.typedb, il2cpp_path=self.il2cpp_dump_path)
        if normalized_format == "readable":
            return build_readable(parsed, self.schema_path, self.il2cpp_dump_path)
        if normalized_format == "readable_debug":
            return build_readable_debug(parsed, self.schema_path, self.il2cpp_dump_path)
        return build_repack(parsed, self.schema_path, self.il2cpp_dump_path)

    def export_file(
        self,
        rcol_path: str | Path,
        json_path: str | Path | None = None,
        json_format: JsonFormat | str = "readable",
    ) -> Path:
        source = Path(rcol_path)
        normalized_format = normalize_json_format(str(json_format))
        target = Path(json_path) if json_path else source.with_name(f"{source.name}.json")
        document = self.rcol_to_json(source, normalized_format)
        write_json(target, document)
        return target

    def export_path(
        self,
        input_path: str | Path,
        output_root: str | Path | None = None,
        json_format: str | Sequence[str] = "readable",
        limit: int = 0,
    ) -> dict[str, Any]:
        root = Path(input_path)
        output_dir = Path(output_root) if output_root else None
        files = discover_rcol_files(root)
        if limit > 0:
            files = files[:limit]
        formats = normalize_json_formats(json_format)
        split_formats = len(formats) > 1
        layout_hint = None
        if len(files) > 1:
            detected_layouts = []
            # Directory consensus is only a performance hint.  A small,
            # size-diverse sample avoids making the pre-pass proportional to
            # the largest files and is never persisted as a version profile.
            ordered = sorted(files, key=lambda item: item.stat().st_size)
            sample_count = min(16, len(ordered))
            anchor_indices = (
                {
                    round(index * (len(ordered) - 1) / (sample_count - 1))
                    for index in range(sample_count)
                }
                if sample_count > 1
                else {0}
            )
            anchor_files = [ordered[index] for index in sorted(anchor_indices)]
            for anchor in anchor_files:
                try:
                    detected_layouts.append(detect_rcol_layout(anchor.read_bytes()))
                except Exception:
                    continue
            layout_hint = build_layout_hint(detected_layouts)
        summary: dict[str, Any] = {
            "total": len(files),
            "exported": 0,
            "failed": 0,
            "written": [],
            "errors": [],
            "layout_consensus": (
                {
                    "request_stride": f"0x{layout_hint.request_stride:x}",
                    "request_fields": {
                        key: f"0x{value:x}" for key, value in layout_hint.request_fields.items()
                    },
                    "group_stride": f"0x{layout_hint.group_stride:x}",
                }
                if layout_hint is not None
                else None
            ),
        }
        for source in files:
            try:
                parsed = parse_rcol(
                    source,
                    self.typedb,
                    il2cpp_path=self.il2cpp_dump_path,
                    layout_hint=layout_hint,
                )
                for one_format in formats:
                    if one_format == "readable":
                        document = build_readable(parsed, self.schema_path, self.il2cpp_dump_path)
                    elif one_format == "readable_debug":
                        document = build_readable_debug(
                            parsed,
                            self.schema_path,
                            self.il2cpp_dump_path,
                        )
                    else:
                        document = build_repack(parsed, self.schema_path, self.il2cpp_dump_path)
                    target = default_output_path(source, root, output_dir, one_format, split_formats)
                    write_json(target, document)
                    summary["written"].append(display_path(target))
                summary["exported"] += 1
            except Exception as exc:
                summary["failed"] += 1
                summary["errors"].append(
                    {
                        "source": display_path(source),
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
        return summary

    def export_directory(
        self,
        input_root: str | Path,
        output_root: str | Path | None = None,
        json_format: str | Sequence[str] = "readable",
        limit: int = 0,
    ) -> dict[str, Any]:
        return self.export_path(input_root, output_root, json_format=json_format, limit=limit)


def write_json(path: str | Path, document: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return target
