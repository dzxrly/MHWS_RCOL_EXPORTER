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
    u64,
)
from .detect import DetectedLayout, LayoutHint, detect_rcol_layout
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
from .rsz import RszBlockParser, native_field_count_candidates


def parse_rcol(
    path: Path,
    typedb: TypeDB,
    il2cpp_path: Path | None = None,
    layout_hint: LayoutHint | None = None,
) -> dict[str, Any]:
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

    detected = detect_rcol_layout(data, hint=layout_hint)
    result["rcol_layout"] = detected.to_dict()
    header = parse_header(data, detected)
    result["header"] = {key: value for key, value in header.items() if key != "_offset_values"}
    offsets = header["_offset_values"]
    string_start = detected.string_start
    result["strings"] = scan_string_table(data, string_start)

    shape_start = detected.groups_offset + detected.group_count * detected.group_stride
    groups = parse_groups(data, detected)
    shapes = parse_shapes(data, shape_start, offsets["rsz"], il2cpp_path)
    result["groupInfos"] = attach_shapes(data, groups, shapes)

    request_sets = parse_request_sets(data, detected)
    rsz_start = offsets["rsz"]
    request_start = offsets["request_sets"]
    rsz_end = request_start if request_start > rsz_start else string_start
    if 0 < rsz_start < len(data) and rsz_end > rsz_start:
        result["rsz"] = parse_rsz_auto(
            data[rsz_start:rsz_end],
            typedb,
            il2cpp_path=il2cpp_path,
            request_sets=request_sets,
            version_hint=rcol_version(path),
        )
    else:
        result["rsz"] = {"error": "missing_or_invalid_rsz"}
    result["rcol_layout"]["schema_validation"] = layout_schema_validation(
        request_sets,
        result["rsz"],
    )
    result["requestSets"] = attach_request_userdata(request_sets, result["rsz"])
    result["ignoreTags"] = parse_ignore_tags(data, header, string_start)
    result["_raw"] = build_raw_sections(data, header, detected, shape_start, string_start)
    return result


def rcol_version(path: Path) -> int:
    try:
        return int(path.name.rsplit(".", 1)[-1])
    except ValueError:
        return 0


def parse_rsz_auto(
    block: bytes,
    typedb: TypeDB,
    il2cpp_path: Path | None,
    request_sets: list[dict[str, Any]],
    version_hint: int,
) -> dict[str, Any]:
    preferred = 3 if version_hint >= 38 else 2
    candidates = native_field_count_candidates(block, typedb, preferred=preferred)
    attempts: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_score: tuple[int, int, int] | None = None
    first_error: Exception | None = None

    for native_count in candidates:
        try:
            parsed = RszBlockParser(
                typedb,
                il2cpp_path=il2cpp_path,
                native_field_count=native_count,
            ).parse(block)
        except Exception as exc:
            if first_error is None:
                first_error = exc
            attempts.append(
                {
                    "native_field_count": native_count,
                    "score": -1_000_000,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            continue

        stats = score_rsz_parse(parsed, request_sets)
        diagnostics = parsed.setdefault("_diagnostics", {})
        diagnostics.update(stats)
        score_tuple = (
            int(stats["score"]),
            1 if native_count == preferred else 0,
            int(stats["request_index_matches"]),
        )
        attempts.append(
            {
                "native_field_count": native_count,
                "score": stats["score"],
                "unparsed_instances": stats["unparsed_instances"],
                "missing_refs": stats["missing_refs"],
                "invalid_object_indices": stats["invalid_object_indices"],
                "request_index_matches": stats["request_index_matches"],
                "request_index_mismatches": stats["request_index_mismatches"],
                "request_index_present": stats["request_index_present"],
            }
        )
        if best_score is None or score_tuple > best_score:
            best = parsed
            best_score = score_tuple

    if best is None:
        if first_error is not None:
            raise first_error
        raise ValueError("no RSZ native field candidates were available")

    best.setdefault("_diagnostics", {})["candidate_scores"] = attempts
    return best


def score_rsz_parse(parsed: dict[str, Any], request_sets: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics = parsed.get("_diagnostics") or {}
    object_table = parsed.get("object_table") or []
    trees = parsed.get("object_trees") or {}
    unparsed_instances = int(diagnostics.get("unparsed_instances") or 0)

    invalid_object_indices = 0
    request_index_present = 0
    request_index_matches = 0
    request_index_mismatches = 0
    missing_refs = count_problem_refs(trees)

    for request_set in request_sets:
        indices = request_set.get("_object_table_indices") or {}
        expected_index = request_set.get("requestSetIndex")
        native_start = indices.get("nativeShapeCollider")
        native_end = indices.get("nativeShapeColliderEnd")
        table_indices = [indices.get("userData")]
        if isinstance(native_start, int) and isinstance(native_end, int) and native_end >= native_start:
            table_indices.extend(range(native_start, native_end))
        else:
            table_indices.append(native_start)
        for table_index in table_indices:
            if not isinstance(table_index, int) or table_index < 0 or table_index >= len(object_table):
                invalid_object_indices += 1
                continue
            root_id = object_table[table_index]
            payload = single_class_payload(trees.get(str(root_id), {}))
            if not isinstance(payload, dict) or "RequestSetIndex" not in payload:
                continue
            request_index_present += 1
            value = payload.get("RequestSetIndex")
            if isinstance(value, int) and value == expected_index:
                request_index_matches += 1
            else:
                request_index_mismatches += 1

    score = (
        request_index_matches * 100
        - request_index_mismatches * 500
        - unparsed_instances * 300
        - missing_refs * 80
        - invalid_object_indices * 500
    )
    return {
        "score": score,
        "unparsed_instances": unparsed_instances,
        "missing_refs": missing_refs,
        "invalid_object_indices": invalid_object_indices,
        "request_index_present": request_index_present,
        "request_index_matches": request_index_matches,
        "request_index_mismatches": request_index_mismatches,
    }


def single_class_payload(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        return next(iter(value.values()))
    return value


def count_problem_refs(value: Any) -> int:
    if isinstance(value, dict):
        total = 0
        ref_id = value.get("ref_instance_id")
        if isinstance(ref_id, int) and (ref_id < 0 or value.get("missing") or value.get("unparsed")):
            total += 1
        for child in value.values():
            total += count_problem_refs(child)
        return total
    if isinstance(value, list):
        return sum(count_problem_refs(item) for item in value)
    return 0


def parse_header(data: bytes, detected: DetectedLayout | None = None) -> dict[str, Any]:
    legacy_offsets = read_named_fields(data, HEADER_OFFSETS)
    legacy_counts = read_named_fields(data, HEADER_COUNTS)
    if detected is None:
        offsets = legacy_offsets
        counts = legacy_counts
        raw_u32 = {
            f"0x{field.offset:02x}": legacy_counts[field.name]
            for field in HEADER_COUNTS
        }
        raw_u64 = {
            f"0x{field.offset:02x}": legacy_offsets[field.name]
            for field in HEADER_OFFSETS
        }
    else:
        offsets = dict(legacy_offsets)
        offsets.update(
            {
                "groups": detected.groups_offset,
                "rsz": detected.rsz_offset,
                "request_sets": detected.request.table_offset,
            }
        )
        counts = dict(legacy_counts)
        counts.update(
            {
                "groups": detected.group_count,
                "collider_user_data": max(0, detected.object_count - detected.request.count),
                "request_sets": detected.request.count,
            }
        )
        raw_u32 = {f"0x{offset:02x}": value for offset, value in detected.raw_u32.items()}
        raw_u64 = {f"0x{offset:02x}": value for offset, value in detected.raw_u64.items()}
    return {
        "magic": "RCOL",
        "counts": counts,
        "unknowns": read_named_fields(data, HEADER_UNKNOWNS),
        "offsets": {key: hx(value) for key, value in offsets.items()},
        "raw_u32": raw_u32,
        "raw_u64": {key: hx(value) for key, value in raw_u64.items()},
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


def parse_groups(data: bytes, layout: DetectedLayout) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    start = layout.groups_offset
    for index in range(layout.group_count):
        offset = start + index * layout.group_stride
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
                    "shape_count": fields["shapeCount"],
                    "layout": f"{GROUP_LAYOUT.name}/auto_stride_{layout.group_stride:#x}",
                    "raw_u32": [u32(data, offset + pos) for pos in range(0, layout.group_stride, 4)],
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


def parse_request_sets(data: bytes, layout: DetectedLayout) -> list[dict[str, Any]]:
    request_layout = layout.request
    start = request_layout.table_offset
    count = request_layout.count
    stride = request_layout.stride
    field_offsets = request_layout.fields
    out: list[dict[str, Any]] = []
    for index in range(count):
        offset = start + index * stride
        if offset + stride > len(data):
            break
        fields = {
            name: (
                u64(data, offset + field_offset)
                if name in {"nameOffset", "keyNameOffset"}
                else u32(data, offset + field_offset)
            )
            for name, field_offset in field_offsets.items()
            if offset + field_offset + (8 if name in {"nameOffset", "keyNameOffset"} else 4) <= len(data)
        }
        out.append(
            {
                "requestSetID": fields.get("requestSetID", 0),
                "groupIndex": fields["groupIndex"],
                "status": fields.get("status", 0),
                "requestSetIndex": fields["requestSetIndex"],
                "keyHash": fields.get("keyHash", 0),
                "KeyNameMMHash": fields.get("KeyNameMMHash", 0),
                "name": read_string_field(data, fields, "nameOffset") if "nameOffset" in fields else "",
                "keyName": read_string_field(data, fields, "keyNameOffset") if "keyNameOffset" in fields else "",
                "_object_table_indices": {
                    "userData": fields["userDataObjectIndex"],
                    "nativeShapeCollider": fields["nativeShapeColliderObjectIndex"],
                },
                "_raw": {
                    "offset": hx(offset),
                    "name_offset": hx(fields.get("nameOffset")),
                    "key_name_offset": hx(fields.get("keyNameOffset")),
                    "layout": f"{REQUEST_SET_LAYOUT.name}/auto_stride_{stride:#x}",
                    "field_offsets": {name: hx(value) for name, value in field_offsets.items()},
                    "raw_u32": [u32(data, offset + word) for word in range(0, stride, 4)],
                },
            }
        )
    object_count = layout.object_count
    for index, item in enumerate(out):
        indices = item["_object_table_indices"]
        end = (
            out[index + 1]["_object_table_indices"]["userData"]
            if index + 1 < len(out)
            else object_count
        )
        indices["nativeShapeColliderEnd"] = end
    return out


def attach_request_userdata(request_sets: list[dict[str, Any]], rsz: dict[str, Any]) -> list[dict[str, Any]]:
    object_table = rsz.get("object_table") or []
    trees = rsz.get("object_trees") or {}
    out: list[dict[str, Any]] = []
    for item in request_sets:
        indices = item.pop("_object_table_indices")
        user_root = object_table_value(object_table, indices["userData"])
        native_start = indices["nativeShapeCollider"]
        native_end = indices.get("nativeShapeColliderEnd", native_start + 1)
        native_roots = [
            object_table_value(object_table, table_index)
            for table_index in range(native_start, native_end)
        ]
        item["nativeShapeColliders"] = [
            trees.get(str(native_root), {"Ref": {"ref_instance_id": native_root}})
            for native_root in native_roots
        ]
        item["userData"] = trees.get(str(user_root), {"Ref": {"ref_instance_id": user_root}})
        item["_raw"]["object_table_indices"] = indices
        item["_raw"]["instance_roots"] = {
            "userData": user_root,
            "nativeShapeColliders": native_roots,
        }
        out.append(item)
    return out


def layout_schema_validation(request_sets: list[dict[str, Any]], rsz: dict[str, Any]) -> dict[str, Any]:
    stats = score_rsz_parse(rsz, request_sets)
    present = int(stats.get("request_index_present") or 0)
    matches = int(stats.get("request_index_matches") or 0)
    invalid = int(stats.get("invalid_object_indices") or 0)
    mismatches = int(stats.get("request_index_mismatches") or 0)
    return {
        "valid": invalid == 0 and mismatches == 0,
        "request_index_present": present,
        "request_index_matches": matches,
        "request_index_mismatches": mismatches,
        "invalid_object_indices": invalid,
        "missing_refs": stats.get("missing_refs", 0),
        "unparsed_instances": stats.get("unparsed_instances", 0),
    }


def object_table_value(object_table: list[Any], index: Any) -> Any:
    if isinstance(index, int) and 0 <= index < len(object_table):
        return object_table[index]
    return None


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


def build_raw_sections(
    data: bytes,
    header: dict[str, Any],
    layout: DetectedLayout,
    shape_start: int,
    string_start: int,
) -> dict[str, Any]:
    offsets = header["_offset_values"]
    request_records_end = layout.request.end
    sections = {
        "header": section_summary(data, 0, layout.groups_offset),
        "groups": section_summary(data, offsets["groups"], shape_start),
        "shapes": section_summary(data, shape_start, offsets["rsz"]),
        "rsz": section_summary(data, offsets["rsz"], offsets["request_sets"]),
        "request_set_records": section_summary(data, offsets["request_sets"], request_records_end),
        "post_request_data": section_summary(data, request_records_end, string_start),
        "strings": section_summary(data, string_start, len(data)),
    }
    return sections
