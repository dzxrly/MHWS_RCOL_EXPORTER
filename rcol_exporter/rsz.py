from __future__ import annotations

from pathlib import Path
from typing import Any

from pyreuser3.core import BinaryReader, ParseError, align
from pyreuser3.export.fields import ExporterFieldParserMixin
from pyreuser3.schema import TypeDB

from .binary import RSZ_MAGIC, hx, u32
from .il2cpp import Il2cppMetadata


class RszBlockParser(ExporterFieldParserMixin):
    def __init__(
        self,
        typedb: TypeDB,
        il2cpp_path: str | Path | None = None,
        has_request_set_index: bool = True,
    ):
        self.typedb = typedb
        self.metadata = Il2cppMetadata(il2cpp_path)
        self.has_request_set_index = has_request_set_index
        self._instances: dict[int, dict[str, Any]] = {}

    def parse(self, block: bytes) -> dict[str, Any]:
        reader = BinaryReader(block)
        if len(block) < 48:
            raise ParseError("RSZ block is shorter than its header")

        header = {
            "magic": reader.read_u32(),
            "version": reader.read_u32(),
            "object_count": reader.read_s32(),
            "instance_count": reader.read_s32(),
            "userdata_count": reader.read_s32(),
            "reserved": reader.read_s32(),
            "instance_offset": reader.read_s64(),
            "data_offset": reader.read_s64(),
            "userdata_offset": reader.read_s64(),
        }
        if header["magic"] != RSZ_MAGIC:
            raise ParseError(f"RSZ magic mismatch: 0x{header['magic']:08x}")

        reader.seek(48)
        object_table = [reader.read_s32() for _ in range(max(header["object_count"], 0))]
        object_set = set(object_table)

        instance_infos: list[dict[str, Any]] = []
        reader.seek(header["instance_offset"])
        for index in range(max(header["instance_count"], 0)):
            class_hash = reader.read_u32()
            crc = reader.read_u32()
            cls = self.typedb.get_class(class_hash)
            instance_infos.append(
                {
                    "index": index,
                    "hash": f"0x{class_hash:08x}",
                    "crc": f"0x{crc:08x}",
                    "class_name": cls.name if cls else "Unknown Class",
                    "is_object_root": index in object_set,
                }
            )

        class_names = {str(info["class_name"]) for info in instance_infos if info.get("class_name")}
        self.metadata.prepare(class_names)

        userdata_ids, userdata_paths = self._read_userdata(reader, block, header)
        userdata_set = set(userdata_ids)
        parsed_instances = self._read_instances(reader, header, instance_infos, userdata_set, userdata_paths)
        self._instances = {int(item["index"]): item for item in parsed_instances if isinstance(item.get("index"), int)}

        object_trees = {
            str(root_id): self.build_tree(root_id, depth=6)
            for root_id in object_table
            if isinstance(root_id, int) and root_id > 0
        }

        return {
            "header": {
                key: (hx(value) if key.endswith("_offset") else value)
                for key, value in header.items()
            },
            "object_table": object_table,
            "userdata": [
                {"instance_id": item, "path": userdata_paths.get(item, "")}
                for item in userdata_ids
            ],
            "instance_infos": instance_infos,
            "instances": parsed_instances,
            "object_trees": object_trees,
        }

    def _read_userdata(
        self, reader: BinaryReader, block: bytes, header: dict[str, int]
    ) -> tuple[list[int], dict[int, str]]:
        userdata_ids: list[int] = []
        userdata_paths: dict[int, str] = {}
        if header["userdata_count"] <= 0 or header["userdata_offset"] <= 0:
            return userdata_ids, userdata_paths
        reader.seek(header["userdata_offset"])
        for _ in range(header["userdata_count"]):
            instance_id = reader.read_s32()
            reader.read_u32()
            path_offset = reader.read_u64()
            userdata_ids.append(instance_id)
            userdata_paths[instance_id] = ""
            if 0 < path_offset < len(block):
                from .binary import read_wstring

                userdata_paths[instance_id] = read_wstring(block, path_offset)
        return userdata_ids, userdata_paths

    def _read_instances(
        self,
        reader: BinaryReader,
        header: dict[str, int],
        instance_infos: list[dict[str, Any]],
        userdata_set: set[int],
        userdata_paths: dict[int, str],
    ) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        reader.seek(header["data_offset"])
        for info in instance_infos:
            index = int(info["index"])
            class_hash = int(str(info["hash"]), 16)
            if index == 0:
                parsed.append({"index": 0, "class_name": None, "kind": "null"})
                continue
            if index in userdata_set:
                parsed.append(
                    {
                        "index": index,
                        "class_name": info["class_name"],
                        "kind": "userdata_reference",
                        "path": userdata_paths.get(index, ""),
                    }
                )
                continue

            cls = self.typedb.get_class(class_hash)
            if cls is None:
                parsed.append(
                    {
                        "index": index,
                        "class_name": info["class_name"],
                        "unparsed": True,
                        "reason": "class_not_found_in_schema",
                    }
                )
                continue
            if cls.fields:
                first = cls.fields[0]
                reader.seek(align(reader.tell(), 4 if first.is_array else max(first.align, 1)))
            start = reader.tell()
            try:
                instance = self._parse_instance_versioned(reader, class_hash)
                parsed.append(
                    {
                        "index": index,
                        "offset": hx(start),
                        "end": hx(reader.tell()),
                        "class_name": instance.get("_class", info["class_name"]),
                        "fields": instance.get("fields", {}),
                    }
                )
            except Exception as exc:
                parsed.append(
                    {
                        "index": index,
                        "offset": hx(start),
                        "class_name": info["class_name"],
                        "unparsed": True,
                        "reason": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                reader.seek(min(reader.size, start + self._estimate_min_instance_size(cls)))
        return parsed

    def _parse_instance_versioned(self, reader: BinaryReader, class_hash: int) -> dict[str, Any]:
        cls = self.typedb.get_class(class_hash)
        if cls is None:
            raise ParseError(f"class hash 0x{class_hash:08x} not found in schema")
        out: dict[str, Any] = {"_class": cls.name, "fields": {}}
        for idx, field in enumerate(cls.fields):
            if self._skip_native_v2(cls.name, idx, field.name):
                continue
            reader.seek(align(reader.tell(), 4 if field.is_array else max(field.align, 1)))
            out["fields"][field.name or "unnamed"] = self._parse_field_value(reader, field, depth=0)
        return out

    def _skip_native_v2(self, class_name: str, field_index: int, field_name: str) -> bool:
        if self.has_request_set_index:
            return False
        if field_name != "v2" or field_index != 2:
            return False
        cls_hash = self.typedb.name_to_hash.get(class_name)
        cls = self.typedb.get_class(cls_hash) if cls_hash is not None else None
        if cls is None or len(cls.fields) < 3:
            return False
        return [field.name for field in cls.fields[:3]] == ["v0", "v1", "v2"]

    def build_tree(self, index: int, depth: int = 6, visited: set[int] | None = None) -> dict[str, Any]:
        if visited is None:
            visited = set()
        if index in visited:
            return {"Ref": {"ref_instance_id": index, "cycle": True}}
        visited.add(index)
        instance = self._instances.get(index)
        if instance is None:
            return {"Ref": {"ref_instance_id": index, "missing": True}}
        class_name = instance.get("class_name") or ""
        if instance.get("kind") == "null":
            return {class_name: None}
        if instance.get("kind") == "userdata_reference":
            return {class_name: {"ref_instance_id": index, "path": instance.get("path", "")}}
        if instance.get("unparsed"):
            return {class_name: {"ref_instance_id": index, "unparsed": True, "reason": instance.get("reason", "")}}

        fields = self._rename_native_fields(class_name, instance.get("fields", {}))
        resolved = self._resolve_value(fields, depth, set(visited))
        resolved = self._apply_enum_labels(resolved, class_name)
        return {class_name: resolved}

    def _resolve_value(self, value: Any, depth: int, visited: set[int]) -> Any:
        if isinstance(value, dict):
            ref_id = value.get("ref_instance_id")
            if isinstance(ref_id, int):
                if ref_id == 0:
                    return {"": {}}
                if depth <= 0:
                    return {"ref_instance_id": ref_id}
                return self.build_tree(ref_id, depth - 1, set(visited))
            if set(value.keys()) == {"raw", "type"}:
                return value
            return {key: self._resolve_value(child, depth, set(visited)) for key, child in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(item, depth, set(visited)) for item in value]
        if isinstance(value, float):
            return round(value, 4)
        return value

    def _rename_native_fields(self, class_name: str, fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "v0":
                out["Name"] = value
            elif key == "v1":
                out["ParentUserData"] = {"": {}}
            elif key == "v2":
                request_index = self._raw_u32(value)
                if request_index is not None:
                    out["RequestSetIndex"] = request_index
            else:
                out[key] = value
        return out

    @staticmethod
    def _raw_u32(value: Any) -> int | None:
        if not isinstance(value, dict) or value.get("type") != "Data":
            return None
        raw = value.get("raw")
        if not isinstance(raw, str) or len(raw) < 8:
            return None
        return u32(bytes.fromhex(raw[:8]), 0)

    def _apply_enum_labels(self, value: Any, class_name: str | None = None, field_name: str | None = None) -> Any:
        if isinstance(value, dict):
            if class_name and class_name.startswith("ace.Bitset`") and isinstance(value.get("_Value"), list):
                value = dict(value)
                value["_Value"] = self._decode_bitset_words(value["_Value"], value.get("_MaxElement"))
            if len(value) == 1:
                only_key = next(iter(value))
                only_value = value[only_key]
                if isinstance(only_key, str) and "." in only_key and isinstance(only_value, dict):
                    return {
                        only_key: self._apply_enum_labels(
                            only_value, class_name=only_key, field_name=None
                        )
                    }
            out: dict[str, Any] = {}
            for key, child in value.items():
                out[key] = self._apply_enum_labels(child, class_name=class_name, field_name=key)
            return out
        if isinstance(value, list):
            return [self._apply_enum_labels(item, class_name=class_name, field_name=field_name) for item in value]
        if isinstance(value, int):
            enum_type = None
            if field_name == "_Value":
                enum_type = self.metadata.enum_for_serializable(class_name)
            if enum_type is None and field_name:
                enum_type = self.metadata.enum_for_field(class_name, field_name)
            return self.metadata.format_enum(enum_type, value)
        return value

    @staticmethod
    def _decode_bitset_words(words: Any, max_element: Any) -> list[int]:
        if not isinstance(words, list) or not all(isinstance(item, int) for item in words):
            return words
        max_bits = max_element if isinstance(max_element, int) and max_element >= 0 else len(words) * 32
        out: list[int] = []
        for word_index, word in enumerate(words):
            for bit_index in range(32):
                absolute = word_index * 32 + bit_index
                if absolute >= max_bits:
                    break
                if word & (1 << bit_index):
                    out.append(absolute)
        return out
