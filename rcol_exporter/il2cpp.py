from __future__ import annotations

import json
import mmap
import re
from pathlib import Path
from typing import Any


FIXED_ENUM_RE = re.compile(r"[A-Za-z0-9_.]+_Fixed")
_TOP_LEVEL_CACHE: dict[str, dict[str, dict[str, Any]]] = {}


def _skip_ws(mm: mmap.mmap, pos: int) -> int:
    size = len(mm)
    while pos < size and mm[pos] in b" \t\r\n":
        pos += 1
    return pos


def _scan_string_end(mm: mmap.mmap, pos: int) -> int:
    pos += 1
    size = len(mm)
    escaped = False
    while pos < size:
        ch = mm[pos]
        if escaped:
            escaped = False
        elif ch == 0x5C:
            escaped = True
        elif ch == 0x22:
            return pos + 1
        pos += 1
    raise ValueError("unterminated JSON string")


def _skip_value(mm: mmap.mmap, pos: int) -> int:
    pos = _skip_ws(mm, pos)
    size = len(mm)
    first = mm[pos]
    if first == 0x22:
        return _scan_string_end(mm, pos)
    if first not in (0x7B, 0x5B):
        while pos < size and mm[pos] not in b",}]":
            pos += 1
        return pos

    stack = [first]
    pos += 1
    in_string = False
    escaped = False
    while pos < size and stack:
        ch = mm[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == 0x5C:
                escaped = True
            elif ch == 0x22:
                in_string = False
        else:
            if ch == 0x22:
                in_string = True
            elif ch in (0x7B, 0x5B):
                stack.append(ch)
            elif ch == 0x7D:
                if stack[-1] != 0x7B:
                    raise ValueError("mismatched JSON object")
                stack.pop()
            elif ch == 0x5D:
                if stack[-1] != 0x5B:
                    raise ValueError("mismatched JSON array")
                stack.pop()
        pos += 1
    return pos


def extract_top_level_objects(path: Path, wanted: set[str]) -> dict[str, dict[str, Any]]:
    if not wanted or not path.is_file():
        return {}
    cache_key = str(path.resolve())
    cache = _TOP_LEVEL_CACHE.setdefault(cache_key, {})
    missing = wanted - set(cache)
    if not missing:
        return {key: cache[key] for key in wanted if key in cache}

    found: dict[str, dict[str, Any]] = {}
    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for key in missing:
                encoded = json.dumps(key, ensure_ascii=False).encode("utf-8")
                positions = []
                for pattern in (b"\n    " + encoded + b": {", b"{\r\n    " + encoded + b": {", b"{\n    " + encoded + b": {"):
                    pos = mm.find(pattern)
                    if pos >= 0:
                        positions.append(pos + len(pattern) - 1)
                        break
                if not positions:
                    continue
                value_start = positions[0]
                value_end = _skip_value(mm, value_start)
                found[key] = json.loads(mm[value_start:value_end].decode("utf-8"))
    cache.update(found)
    return {key: cache[key] for key in wanted if key in cache}


def _fixed_types_from_text(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return list(dict.fromkeys(FIXED_ENUM_RE.findall(text)))


def _to_u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _to_s32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def _id_formatter(name: str, value: int) -> str:
    if value & 0x80000000:
        value = _to_s32(value)
    return f"[{value}]{name}"


class Il2cppMetadata:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.enum_lookup: dict[str, dict[int, tuple[str, int]]] = {}
        self.class_field_fixed_types: dict[str, dict[str, str]] = {}
        self.serializable_to_fixed: dict[str, str] = {}
        self.generic_container_rules: dict[str, tuple[str, str]] = {}
        self.param_type_default_enum: dict[str, str] = {}

    def prepare(self, class_names: set[str]) -> None:
        if self.path is None or not self.path.is_file():
            return
        wanted_classes = {name for name in class_names if name}
        fixed_guess = {
            name.replace("_Serializable", "_Fixed")
            for name in wanted_classes
            if name.endswith("_Serializable")
        }

        class_objects = extract_top_level_objects(self.path, wanted_classes)
        enum_names = set(fixed_guess)
        self._collect_context(class_objects, enum_names)
        enum_objects = extract_top_level_objects(self.path, enum_names)
        self._build_enum_lookup(enum_objects)

    def _collect_context(self, class_objects: dict[str, dict[str, Any]], enum_names: set[str]) -> None:
        for class_name, obj in class_objects.items():
            field_map: dict[str, str] = {}
            for field_name, field_info in (obj.get("fields") or {}).items():
                enum_type = self._enum_type_from_text((field_info or {}).get("type"))
                if enum_type:
                    field_map[field_name] = enum_type
                    enum_names.add(enum_type)
            for rsz_field in obj.get("RSZ") or []:
                if not isinstance(rsz_field, dict):
                    continue
                fixed = self._enum_type_from_text(rsz_field.get("type"))
                potential = rsz_field.get("potential_name")
                if fixed and isinstance(potential, str):
                    field_map.setdefault(potential, fixed)
                    enum_names.add(fixed)
            for prop_name, prop_info in (obj.get("reflection_properties") or {}).items():
                fixed = self._enum_type_from_text((prop_info or {}).get("type"))
                if fixed:
                    field_map.setdefault(prop_name, fixed)
                    enum_names.add(fixed)
            if field_map:
                self.class_field_fixed_types[class_name] = field_map

            if class_name.endswith("_Serializable"):
                fixed_types = set()
                default_guess = class_name.replace("_Serializable", "_Fixed")
                fixed_types.add(default_guess)
                for method in (obj.get("methods") or {}).values():
                    if not isinstance(method, dict):
                        continue
                    returns = method.get("returns")
                    if isinstance(returns, dict):
                        fixed_types.update(_fixed_types_from_text(returns.get("type")))
                    for param in method.get("params") or []:
                        if isinstance(param, dict):
                            fixed_types.update(_fixed_types_from_text(param.get("type")))
                for fixed in fixed_types:
                    enum_names.add(fixed)
                if len(fixed_types) == 1:
                    self.serializable_to_fixed[class_name] = next(iter(fixed_types))
                elif default_guess in fixed_types:
                    self.serializable_to_fixed[class_name] = default_guess

            generic_args = obj.get("generic_arg_types")
            if isinstance(generic_args, list) and len(generic_args) >= 2:
                enum_arg, param_arg = generic_args[0], generic_args[1]
                enum_type = self._one_fixed_type(enum_arg.get("type") if isinstance(enum_arg, dict) else None)
                param_type = param_arg.get("type") if isinstance(param_arg, dict) else None
                if enum_type and isinstance(param_type, str):
                    self.generic_container_rules[class_name] = (param_type, enum_type)
                    enum_names.add(enum_type)
                    self.param_type_default_enum.setdefault(param_type, enum_type)

    def _build_enum_lookup(self, enum_objects: dict[str, dict[str, Any]]) -> None:
        for enum_type, obj in enum_objects.items():
            if obj.get("parent") != "System.Enum":
                continue
            value_map: dict[int, tuple[str, int]] = {}
            for member_name, member_info in (obj.get("fields") or {}).items():
                if member_name == "value__" or not isinstance(member_info, dict):
                    continue
                raw_value = member_info.get("default")
                if not isinstance(raw_value, int):
                    continue
                entry = (member_name, raw_value)
                value_map[_to_s32(raw_value)] = entry
                value_map[_to_u32(raw_value)] = entry
            if value_map:
                self.enum_lookup[enum_type] = value_map

    @staticmethod
    def _one_fixed_type(text: Any) -> str | None:
        matches = _fixed_types_from_text(text)
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _enum_type_from_text(cls, text: Any) -> str | None:
        fixed = cls._one_fixed_type(text)
        if fixed:
            return fixed
        if not isinstance(text, str) or not text.startswith("app."):
            return None
        if "<" in text or ">" in text:
            return None
        leaf = text.rsplit(".", 1)[-1]
        if leaf.upper() == leaf and any(ch.isalpha() for ch in leaf):
            return text
        if "_TYPE" in leaf or "FLAG" in leaf:
            return text
        return None

    def format_enum(self, enum_type: str | None, value: Any) -> Any:
        if not enum_type or not isinstance(value, int):
            return value
        value_map = self.enum_lookup.get(enum_type)
        if not value_map:
            return value
        match = value_map.get(value) or value_map.get(_to_s32(value)) or value_map.get(_to_u32(value))
        if not match:
            return value
        name, fixed_value = match
        return _id_formatter(name, fixed_value)

    def enum_for_serializable(self, class_name: str | None) -> str | None:
        if not class_name:
            return None
        direct = self.serializable_to_fixed.get(class_name)
        if direct:
            return direct
        if class_name.endswith("_Serializable"):
            return class_name.replace("_Serializable", "_Fixed")
        return None

    def enum_for_field(self, class_name: str | None, field_name: str) -> str | None:
        if not class_name:
            return None
        for candidate in (class_name, class_name.replace(".cData", ".cParam"), class_name.replace(".cParam", ".cData")):
            field_map = self.class_field_fixed_types.get(candidate)
            if field_map and field_name in field_map:
                return field_map[field_name]
        return None
