from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pyreuser3.schema import TypeDB

from .binary import (
    HEADER_SIZE,
    RCOL_MAGIC,
    collect_string_offsets,
    display_path,
    f32_word,
    guid_from_bytes,
    hx,
    scan_string_table,
    section_summary,
    u32,
)
from .layout import (
    GROUP_LAYOUT,
    HEADER_COUNTS,
    HEADER_OFFSETS,
    HEADER_UNKNOWNS,
    REQUEST_SET_LAYOUT,
    SHAPE_LAYOUT,
    SHAPE_PARAM_BASE,
    SHAPE_PARAM_SIZE,
    read_named_fields,
    read_string_field,
    shape_param_layout,
    shape_type_labels,
)
from .rsz import RszBlockParser


def parse_rcol(path: Path, typedb: TypeDB, il2cpp_path: Path | None = None) -> dict[str, Any]:
    data = path.read_bytes()
    result: dict[str, Any] = {
        "source": display_path(path),
        "file_name": path.name,
        "file_size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "raw_bytes": data,
    }
    if len(data) < HEADER_SIZE:
        result["error"] = f"file_too_small:{len(data)}"
        return result
    if data[:4] != RCOL_MAGIC:
        result["error"] = f"bad_magic:{data[:4]!r}"
        return result

    header = parse_header(data)
    result["header"] = {key: value for key, value in header.items() if key != "_offset_values"}
    offsets = header["_offset_values"]
    string_start = likely_string_start(header, len(data))
    result["strings"] = scan_string_table(data, string_start)

    shape_start = HEADER_SIZE + header["counts"]["groups"] * GROUP_LAYOUT.size
    groups = parse_groups(data, header)
    shapes = parse_shapes(data, shape_start, offsets["rsz"], il2cpp_path)
    result["groupInfos"] = attach_shapes(data, groups, shapes)

    request_sets = parse_request_sets(data, header)
    rsz_start = offsets["rsz"]
    request_start = offsets["request_sets"]
    rsz_end = request_start if request_start > rsz_start else string_start
    has_request_set_index = rcol_version(path) >= 38
    if 0 < rsz_start < len(data) and rsz_end > rsz_start:
        result["rsz"] = RszBlockParser(
            typedb,
            il2cpp_path=il2cpp_path,
            has_request_set_index=has_request_set_index,
        ).parse(data[rsz_start:rsz_end])
    else:
        result["rsz"] = {"error": "missing_or_invalid_rsz"}
    result["requestSets"] = attach_request_userdata(request_sets, result["rsz"])
    result["ignoreTags"] = parse_ignore_tags(data, header, string_start)
    result["_raw"] = build_raw_sections(data, header, shape_start, string_start)
    return result


def rcol_version(path: Path) -> int:
    try:
        return int(path.name.rsplit(".", 1)[-1])
    except ValueError:
        return 0


def parse_header(data: bytes) -> dict[str, Any]:
    offsets = read_named_fields(data, HEADER_OFFSETS)
    return {
        "magic": "RCOL",
        "counts": read_named_fields(data, HEADER_COUNTS),
        "unknowns": read_named_fields(data, HEADER_UNKNOWNS),
        "offsets": {key: hx(value) for key, value in offsets.items()},
        "_offset_values": offsets,
    }


def likely_string_start(header: dict[str, Any], file_size: int) -> int:
    offsets = header["_offset_values"]
    candidates = [
        offsets["auto_generate_joint_descs"],
        offsets["unknown_58"],
        offsets["unknown_60"],
    ]
    valid = [value for value in candidates if 0 < value < file_size]
    if valid:
        return min(valid)
    fallback = offsets["ignore_tags"]
    return fallback if 0 < fallback < file_size else file_size


def parse_groups(data: bytes, header: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    start = header["_offset_values"]["groups"]
    for index in range(header["counts"]["groups"]):
        offset = start + index * GROUP_LAYOUT.size
        fields = GROUP_LAYOUT.read(data, offset)
        groups.append(
            {
                "groupGuid": fields["groupGuid"],
                "nameMMHash": fields["nameMMHash"],
                "shapeCount": fields["shapeCount"],
                "unknCount": fields["unknCount"],
                "maskCount": fields["maskCount"],
                "shapeOffset": fields["shapeOffset"],
                "maskOffset": fields["maskOffset"],
                "layerGuid": fields["layerGuid"],
                "groupName": read_string_field(data, fields, "nameOffset"),
                "_raw": {
                    "offset": hx(offset),
                    "name_offset": hx(fields["nameOffset"]),
                    "shape_offset": hx(fields["shapeOffset"]),
                    "mask_offset": hx(fields["maskOffset"]),
                    "layout": GROUP_LAYOUT.name,
                    "raw_u32": [u32(data, offset + pos) for pos in range(0, GROUP_LAYOUT.size, 4)],
                },
            }
        )
    return groups


def parse_shapes(data: bytes, start: int, end: int, il2cpp_path: Path | None = None) -> list[dict[str, Any]]:
    shapes: list[dict[str, Any]] = []
    labels = shape_type_labels(str(il2cpp_path) if il2cpp_path else None)
    pos = start
    while pos + SHAPE_LAYOUT.size <= end:
        fields = SHAPE_LAYOUT.read(data, pos)
        shape_type_id = fields["shapeTypeId"]
        shape_type = labels.get(shape_type_id, f"Unknown({shape_type_id})")
        shape = {
            "shapeGuid": fields["shapeGuid"],
            "shapeNameMMHash": fields["shapeNameMMHash"],
            "unknIndex": fields["unknIndex"],
            "layerIndex": fields["layerIndex"],
            "atteribute": fields["atteribute"],
            "skipIdBits": fields["skipIdBits"],
            "shapeType": shape_type,
            "shapeParam": parse_shape_param(data, pos, shape_type, il2cpp_path),
            "ignoreTagBits": fields["ignoreTagBits"],
            "unkn": fields["unkn"],
            "primaryJointNameMMHash": fields["primaryJointNameMMHash"],
            "secondaryJointNameMMHash": fields["secondaryJointNameMMHash"],
            "shapeName": read_string_field(data, fields, "nameOffset"),
            "primaryJointName": read_string_field(data, fields, "primaryJointOffset"),
            "secondaryJointName": read_string_field(data, fields, "secondaryJointOffset"),
            "_raw": {
                "offset": hx(pos),
                "name_offset": hx(fields["nameOffset"]),
                "primary_joint_offset": hx(fields["primaryJointOffset"]),
                "secondary_joint_offset": hx(fields["secondaryJointOffset"]),
                "shape_type_id": shape_type_id,
                "layout": SHAPE_LAYOUT.name,
                "raw_u32": [u32(data, pos + word) for word in range(0, SHAPE_LAYOUT.size, 4)],
                "float32_view": [f32_word(u32(data, pos + word)) for word in range(0, SHAPE_LAYOUT.size, 4)],
                "possible_string_offsets": collect_string_offsets(data, pos, pos + SHAPE_LAYOUT.size),
            },
        }
        shapes.append(shape)
        pos += SHAPE_LAYOUT.size
    return shapes


def parse_shape_param(data: bytes, offset: int, shape_type: str, il2cpp_path: Path | None = None) -> dict[str, Any]:
    base = offset + SHAPE_PARAM_BASE
    layout = shape_param_layout(shape_type, str(il2cpp_path) if il2cpp_path else None)
    if layout:
        return layout.read(data, base)
    return {
        "floats": [f32_word(u32(data, base + pos)) for pos in range(0, SHAPE_PARAM_SIZE, 4)]
    }


def attach_shapes(data: bytes, groups: list[dict[str, Any]], shapes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_offset = {int(shape["_raw"]["offset"], 16): shape for shape in shapes}
    out: list[dict[str, Any]] = []
    for group in groups:
        shape_offset = group.pop("shapeOffset")
        mask_offset = group.pop("maskOffset")
        shape_count = group.pop("shapeCount")
        mask_count = group.pop("maskCount")
        group_shapes = []
        for idx in range(shape_count):
            shape = by_offset.get(shape_offset + idx * SHAPE_LAYOUT.size)
            if shape is not None:
                group_shapes.append(shape)
        group_info = {
            "groupGuid": group["groupGuid"],
            "nameMMHash": group["nameMMHash"],
            "unknCount": group["unknCount"],
            "layerIndex": 0,
            "maskBits": 0,
            "layerGuid": group["layerGuid"],
            "groupShapes": group_shapes,
            "maskGuids": read_guid_array(data, mask_offset, mask_count),
            "groupName": group["groupName"],
            "_raw": group["_raw"],
        }
        out.append(group_info)
    return out


def read_guid_array(data: bytes, offset: int, count: int) -> list[str]:
    if offset <= 0 or count <= 0:
        return []
    return [
        guid_from_bytes(data[offset + i * 16 : offset + (i + 1) * 16])
        for i in range(count)
        if offset + (i + 1) * 16 <= len(data)
    ]


def parse_request_sets(data: bytes, header: dict[str, Any]) -> list[dict[str, Any]]:
    start = header["_offset_values"]["request_sets"]
    count = header["counts"]["request_sets"]
    out: list[dict[str, Any]] = []
    for index in range(count):
        offset = start + index * REQUEST_SET_LAYOUT.size
        if offset + REQUEST_SET_LAYOUT.size > len(data):
            break
        fields = REQUEST_SET_LAYOUT.read(data, offset)
        out.append(
            {
                "requestSetID": fields["requestSetID"],
                "groupIndex": fields["groupIndex"],
                "status": fields["status"],
                "requestSetIndex": fields["requestSetIndex"],
                "keyHash": fields["keyHash"],
                "KeyNameMMHash": fields["KeyNameMMHash"],
                "name": read_string_field(data, fields, "nameOffset"),
                "keyName": read_string_field(data, fields, "keyNameOffset"),
                "_object_table_indices": {
                    "userData": fields["userDataObjectIndex"],
                    "nativeShapeCollider": fields["nativeShapeColliderObjectIndex"],
                },
                "_raw": {
                    "offset": hx(offset),
                    "name_offset": hx(fields["nameOffset"]),
                    "key_name_offset": hx(fields["keyNameOffset"]),
                    "layout": REQUEST_SET_LAYOUT.name,
                    "raw_u32": [u32(data, offset + word) for word in range(0, REQUEST_SET_LAYOUT.size, 4)],
                },
            }
        )
    return out


def attach_request_userdata(request_sets: list[dict[str, Any]], rsz: dict[str, Any]) -> list[dict[str, Any]]:
    object_table = rsz.get("object_table") or []
    trees = rsz.get("object_trees") or {}
    out: list[dict[str, Any]] = []
    for item in request_sets:
        indices = item.pop("_object_table_indices")
        user_root = object_table[indices["userData"]] if indices["userData"] < len(object_table) else None
        native_root = object_table[indices["nativeShapeCollider"]] if indices["nativeShapeCollider"] < len(object_table) else None
        item["nativeShapeColliders"] = [trees.get(str(native_root), {"Ref": {"ref_instance_id": native_root}})]
        item["userData"] = trees.get(str(user_root), {"Ref": {"ref_instance_id": user_root}})
        item["_raw"]["object_table_indices"] = indices
        item["_raw"]["instance_roots"] = {
            "userData": user_root,
            "nativeShapeCollider": native_root,
        }
        out.append(item)
    return out


def parse_ignore_tags(data: bytes, header: dict[str, Any], string_start: int) -> list[dict[str, Any]]:
    start = header["_offset_values"]["ignore_tags"]
    count = header["counts"]["ignore_tags"]
    if count <= 0 or start <= 0 or start >= string_start:
        return []
    tags = []
    for index in range(count):
        offset = start + index * 16
        if offset + 16 > string_start:
            break
        tags.append({"index": index, "guid": guid_from_bytes(data[offset : offset + 16])})
    return tags


def build_raw_sections(data: bytes, header: dict[str, Any], shape_start: int, string_start: int) -> dict[str, Any]:
    offsets = header["_offset_values"]
    request_records_end = offsets["request_sets"] + header["counts"]["request_sets"] * REQUEST_SET_LAYOUT.size
    sections = {
        "header": section_summary(data, 0, HEADER_SIZE),
        "groups": section_summary(data, offsets["groups"], shape_start),
        "shapes": section_summary(data, shape_start, offsets["rsz"]),
        "rsz": section_summary(data, offsets["rsz"], offsets["request_sets"]),
        "request_set_records": section_summary(data, offsets["request_sets"], request_records_end),
        "post_request_data": section_summary(data, request_records_end, string_start),
        "strings": section_summary(data, string_start, len(data)),
    }
    return sections
