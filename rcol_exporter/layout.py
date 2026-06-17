from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .binary import f32, guid_from_bytes, read_wstring, s32, u32, u64
from .il2cpp import extract_top_level_objects


@dataclass(frozen=True)
class FieldSpec:
    name: str
    offset: int
    kind: str


@dataclass(frozen=True)
class StructLayout:
    name: str
    size: int
    fields: tuple[FieldSpec, ...]

    def read(self, data: bytes, base: int) -> dict[str, Any]:
        return {field.name: read_field(data, base, field) for field in self.fields}


@dataclass(frozen=True)
class ParamField:
    name: str
    offset: int
    kind: str


@dataclass(frozen=True)
class ShapeParamLayout:
    name: str
    source_type: str
    fields: tuple[ParamField, ...]

    def read(self, data: bytes, base: int) -> dict[str, Any]:
        values = {field.name: read_param_field(data, base, field) for field in self.fields}
        if self.name == "Sphere":
            return {
                "x": values.get("pos.x", 0.0),
                "y": values.get("pos.y", 0.0),
                "z": values.get("pos.z", 0.0),
                "radius": values.get("r", 0.0),
            }
        if self.name in {"Capsule", "ContinuousCapsule"}:
            return {
                "start": {
                    "x": values.get("p0.x", 0.0),
                    "y": values.get("p0.y", 0.0),
                    "z": values.get("p0.z", 0.0),
                },
                "end": {
                    "x": values.get("p1.x", 0.0),
                    "y": values.get("p1.y", 0.0),
                    "z": values.get("p1.z", 0.0),
                },
                "radius": values.get("r", 0.0),
            }
        return values


HEADER_COUNTS = (
    FieldSpec("groups", 0x04, "u32"),
    FieldSpec("request_sets", 0x08, "u32"),
    FieldSpec("raw_0c", 0x0C, "u32"),
    FieldSpec("raw_10", 0x10, "u32"),
    FieldSpec("raw_14", 0x14, "u32"),
    FieldSpec("raw_18", 0x18, "u32"),
    FieldSpec("ignore_tags", 0x1C, "u32"),
)

HEADER_UNKNOWNS = (
    FieldSpec("u32_20", 0x20, "u32"),
    FieldSpec("u32_24", 0x24, "u32"),
    FieldSpec("u32_28", 0x28, "u32"),
    FieldSpec("u32_2c", 0x2C, "u32"),
)

HEADER_OFFSETS = (
    FieldSpec("groups", 0x30, "u64"),
    FieldSpec("rsz", 0x38, "u64"),
    FieldSpec("request_sets", 0x40, "u64"),
    FieldSpec("ignore_tags", 0x48, "u64"),
    FieldSpec("auto_generate_joint_descs", 0x50, "u64"),
    FieldSpec("unknown_58", 0x58, "u64"),
    FieldSpec("unknown_60", 0x60, "u64"),
)

GROUP_LAYOUT = StructLayout(
    "RCOL.GroupInfo",
    0x50,
    (
        FieldSpec("groupGuid", 0x00, "guid"),
        FieldSpec("nameOffset", 0x10, "u64"),
        FieldSpec("nameMMHash", 0x18, "u32"),
        FieldSpec("shapeCount", 0x1C, "u32"),
        FieldSpec("unknCount", 0x20, "u32"),
        FieldSpec("maskCount", 0x24, "u32"),
        FieldSpec("shapeOffset", 0x28, "u64"),
        FieldSpec("maskOffset", 0x38, "u64"),
        FieldSpec("layerGuid", 0x40, "guid"),
    ),
)

SHAPE_LAYOUT = StructLayout(
    "RCOL.Shape",
    0xA0,
    (
        FieldSpec("shapeGuid", 0x00, "guid"),
        FieldSpec("nameOffset", 0x10, "u64"),
        FieldSpec("shapeNameMMHash", 0x18, "u32"),
        FieldSpec("unknIndex", 0x1C, "u32"),
        FieldSpec("layerIndex", 0x20, "u32"),
        FieldSpec("atteribute", 0x24, "s32"),
        FieldSpec("skipIdBits", 0x28, "u32"),
        FieldSpec("shapeTypeId", 0x2C, "u32"),
        FieldSpec("ignoreTagBits", 0x30, "u32"),
        FieldSpec("unkn", 0x34, "u32"),
        FieldSpec("primaryJointOffset", 0x38, "u64"),
        FieldSpec("secondaryJointOffset", 0x40, "u64"),
        FieldSpec("primaryJointNameMMHash", 0x48, "u32"),
        FieldSpec("secondaryJointNameMMHash", 0x4C, "u32"),
    ),
)

REQUEST_SET_LAYOUT = StructLayout(
    "RCOL.RequestSet",
    0x30,
    (
        FieldSpec("requestSetID", 0x00, "u32"),
        FieldSpec("groupIndex", 0x04, "u32"),
        FieldSpec("userDataObjectIndex", 0x08, "u32"),
        FieldSpec("nativeShapeColliderObjectIndex", 0x0C, "u32"),
        FieldSpec("status", 0x10, "u32"),
        FieldSpec("requestSetIndex", 0x14, "u32"),
        FieldSpec("nameOffset", 0x18, "u64"),
        FieldSpec("keyNameOffset", 0x20, "u64"),
        FieldSpec("keyHash", 0x28, "u32"),
        FieldSpec("KeyNameMMHash", 0x2C, "u32"),
    ),
)

SHAPE_PARAM_BASE = 0x50
SHAPE_PARAM_SIZE = 0x30
SHAPE_PARAM_SOURCE_TYPES = {
    "Sphere": "via.Sphere",
    "Capsule": "via.Capsule",
    "ContinuousCapsule": "via.Capsule",
}

FALLBACK_SHAPE_TYPE_LABELS = {
    1: "Sphere",
    3: "Capsule",
    4: "ContinuousCapsule",
}

FALLBACK_PARAM_LAYOUTS = {
    "Sphere": ShapeParamLayout(
        "Sphere",
        "via.Sphere",
        (
            ParamField("pos.x", 0x00, "f32"),
            ParamField("pos.y", 0x04, "f32"),
            ParamField("pos.z", 0x08, "f32"),
            ParamField("r", 0x0C, "f32"),
        ),
    ),
    "Capsule": ShapeParamLayout(
        "Capsule",
        "via.Capsule",
        (
            ParamField("p0.x", 0x00, "f32"),
            ParamField("p0.y", 0x04, "f32"),
            ParamField("p0.z", 0x08, "f32"),
            ParamField("p1.x", 0x10, "f32"),
            ParamField("p1.y", 0x14, "f32"),
            ParamField("p1.z", 0x18, "f32"),
            ParamField("r", 0x20, "f32"),
        ),
    ),
    "ContinuousCapsule": ShapeParamLayout(
        "ContinuousCapsule",
        "via.Capsule",
        (
            ParamField("p0.x", 0x00, "f32"),
            ParamField("p0.y", 0x04, "f32"),
            ParamField("p0.z", 0x08, "f32"),
            ParamField("p1.x", 0x10, "f32"),
            ParamField("p1.y", 0x14, "f32"),
            ParamField("p1.z", 0x18, "f32"),
            ParamField("r", 0x20, "f32"),
        ),
    ),
}


def read_field(data: bytes, base: int, field: FieldSpec) -> Any:
    offset = base + field.offset
    if field.kind == "u32":
        return u32(data, offset)
    if field.kind == "s32":
        return s32(data, offset)
    if field.kind == "u64":
        return u64(data, offset)
    if field.kind == "guid":
        return guid_from_bytes(data[offset : offset + 16])
    if field.kind == "wstring":
        return read_wstring(data, u64(data, offset))
    raise ValueError(f"unsupported field kind: {field.kind}")


def read_named_fields(data: bytes, specs: tuple[FieldSpec, ...]) -> dict[str, Any]:
    return {field.name: read_field(data, 0, field) for field in specs}


def read_param_field(data: bytes, base: int, field: ParamField) -> Any:
    if field.kind == "f32":
        return round(f32(data, base + field.offset), 4)
    raise ValueError(f"unsupported param field kind: {field.kind}")


def read_string_field(data: bytes, fields: dict[str, Any], offset_name: str) -> str:
    offset = fields.get(offset_name)
    return read_wstring(data, offset) if isinstance(offset, int) else ""


@lru_cache(maxsize=8)
def shape_type_labels(il2cpp_path_text: str | None) -> dict[int, str]:
    labels = dict(FALLBACK_SHAPE_TYPE_LABELS)
    if not il2cpp_path_text:
        return labels
    path = Path(il2cpp_path_text)
    if not path.is_file():
        return labels
    objects = extract_top_level_objects(path, {"via.physics.ShapeType"})
    shape_type = objects.get("via.physics.ShapeType") or {}
    if shape_type.get("parent") != "System.Enum":
        return labels
    for name, info in (shape_type.get("fields") or {}).items():
        if name == "value__" or not isinstance(info, dict):
            continue
        value = info.get("default")
        if isinstance(value, int):
            labels[value] = name
    return labels


@lru_cache(maxsize=32)
def shape_param_layout(shape_name: str, il2cpp_path_text: str | None) -> ShapeParamLayout | None:
    source_type = SHAPE_PARAM_SOURCE_TYPES.get(shape_name)
    if source_type and il2cpp_path_text:
        path = Path(il2cpp_path_text)
        if path.is_file():
            objects = extract_top_level_objects(path, {source_type})
            layout = _layout_from_il2cpp(shape_name, source_type, objects.get(source_type) or {})
            if layout:
                return layout
    return FALLBACK_PARAM_LAYOUTS.get(shape_name)


def _layout_from_il2cpp(shape_name: str, source_type: str, obj: dict[str, Any]) -> ShapeParamLayout | None:
    fields = obj.get("fields") or {}
    if shape_name == "Sphere":
        pos_offset = _field_offset(fields, "pos")
        r_offset = _field_offset(fields, "r")
        if pos_offset is None or r_offset is None:
            return None
        return ShapeParamLayout(
            shape_name,
            source_type,
            (
                ParamField("pos.x", pos_offset + 0x00, "f32"),
                ParamField("pos.y", pos_offset + 0x04, "f32"),
                ParamField("pos.z", pos_offset + 0x08, "f32"),
                ParamField("r", r_offset, "f32"),
            ),
        )
    if shape_name in {"Capsule", "ContinuousCapsule"}:
        p0_offset = _field_offset(fields, "p0")
        p1_offset = _field_offset(fields, "p1")
        r_offset = _field_offset(fields, "r")
        if p0_offset is None or p1_offset is None or r_offset is None:
            return None
        return ShapeParamLayout(
            shape_name,
            source_type,
            (
                ParamField("p0.x", p0_offset + 0x00, "f32"),
                ParamField("p0.y", p0_offset + 0x04, "f32"),
                ParamField("p0.z", p0_offset + 0x08, "f32"),
                ParamField("p1.x", p1_offset + 0x00, "f32"),
                ParamField("p1.y", p1_offset + 0x04, "f32"),
                ParamField("p1.z", p1_offset + 0x08, "f32"),
                ParamField("r", r_offset, "f32"),
            ),
        )
    return None


def _field_offset(fields: dict[str, Any], name: str) -> int | None:
    info = fields.get(name)
    if not isinstance(info, dict):
        return None
    value = info.get("offset_from_fieldptr")
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return value if isinstance(value, int) else None
