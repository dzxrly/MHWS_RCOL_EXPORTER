from __future__ import annotations

import struct
import unittest

from rcol_exporter.detect import LayoutDetectionError, detect_rcol_layout
from rcol_exporter.rcol import attach_request_userdata, parse_request_sets


def _u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def _u64(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<Q", data, offset, value)


def build_shifted_rcol(
    object_count: int = 5,
    trailing_empty_group: bool = False,
) -> bytes:
    data = bytearray(0x600)
    data[:4] = b"RCOL"

    group_offset = 0x90
    rsz_offset = 0x300
    request_offset = 0x400
    string_offset = 0x500

    # Count fields deliberately do not use the legacy locations.
    group_count = 3 if trailing_empty_group else 2
    _u32(data, 0x0C, group_count)  # groups
    _u32(data, 0x14, object_count - 2)  # collider objects
    _u32(data, 0x18, 2)  # request sets

    # Section pointers are also shifted relative to the legacy header.
    _u64(data, 0x38, group_offset)
    _u64(data, 0x48, rsz_offset)
    _u64(data, 0x58, request_offset)
    _u64(data, 0x68, string_offset)

    first_shape_offset = group_offset + group_count * 0x50
    for index, shape_count in enumerate((2, 1)):
        base = group_offset + index * 0x50
        _u32(data, base + 0x1C, shape_count)
        _u64(data, base + 0x28, first_shape_offset + index * 0xA0)
    if trailing_empty_group:
        base = group_offset + 2 * 0x50
        _u64(data, base + 0x28, rsz_offset)

    _u32(data, rsz_offset, 0x005A5352)
    _u32(data, rsz_offset + 4, 16)
    _u32(data, rsz_offset + 8, object_count)
    _u32(data, rsz_offset + 12, 6)
    _u64(data, rsz_offset + 24, 0x40)
    _u64(data, rsz_offset + 32, 0x80)

    stride = 0x34
    rows = (
        # request ID, group, user object, native start, status, request index
        (100, 0, 0, 1, 0, 0),
        (101, 1, 3, 4, 0, 1),
    )
    for index, row in enumerate(rows):
        base = request_offset + index * stride
        for field_offset, value in zip((0x04, 0x08, 0x0C, 0x10, 0x14, 0x18), row):
            _u32(data, base + field_offset, value)
    return bytes(data)


class LayoutDetectionTests(unittest.TestCase):
    def test_detects_shifted_header_and_request_record(self) -> None:
        layout = detect_rcol_layout(build_shifted_rcol())

        self.assertEqual(layout.groups_offset, 0x90)
        self.assertEqual(layout.group_count, 2)
        self.assertEqual(layout.rsz_offset, 0x300)
        self.assertEqual(layout.request.table_offset, 0x400)
        self.assertEqual(layout.request.count, 2)
        self.assertEqual(layout.request.stride, 0x34)
        self.assertEqual(layout.request.fields["groupIndex"], 0x08)
        self.assertEqual(layout.request.fields["userDataObjectIndex"], 0x0C)
        self.assertEqual(layout.request.fields["nativeShapeColliderObjectIndex"], 0x10)
        self.assertEqual(layout.request.fields["requestSetIndex"], 0x18)
        self.assertGreaterEqual(layout.confidence, 0.9)

    def test_restores_every_native_collider_in_each_object_span(self) -> None:
        data = build_shifted_rcol()
        layout = detect_rcol_layout(data)
        request_sets = parse_request_sets(data, layout)
        rsz = {
            "object_table": [10, 11, 12, 13, 14],
            "object_trees": {str(index): {f"Class{index}": {}} for index in range(10, 15)},
        }

        attached = attach_request_userdata(request_sets, rsz)

        self.assertEqual(len(attached), 2)
        self.assertEqual(len(attached[0]["nativeShapeColliders"]), 2)
        self.assertEqual(len(attached[1]["nativeShapeColliders"]), 1)
        self.assertEqual(
            attached[0]["_raw"]["instance_roots"]["nativeShapeColliders"],
            [11, 12],
        )

    def test_allows_an_empty_native_collider_span(self) -> None:
        data = build_shifted_rcol(object_count=4)
        layout = detect_rcol_layout(data)
        request_sets = parse_request_sets(data, layout)
        rsz = {
            "object_table": [10, 11, 12, 13],
            "object_trees": {str(index): {f"Class{index}": {}} for index in range(10, 14)},
        }

        attached = attach_request_userdata(request_sets, rsz)

        self.assertEqual(len(attached[0]["nativeShapeColliders"]), 2)
        self.assertEqual(attached[1]["nativeShapeColliders"], [])
        self.assertEqual(
            attached[1]["_raw"]["object_table_indices"]["nativeShapeCollider"],
            layout.object_count,
        )

    def test_keeps_a_trailing_group_not_referenced_by_requests(self) -> None:
        layout = detect_rcol_layout(build_shifted_rcol(trailing_empty_group=True))

        self.assertEqual(layout.group_count, 3)
        self.assertEqual(layout.request.count, 2)

    def test_rejects_data_without_a_valid_rsz_anchor(self) -> None:
        data = bytearray(0x200)
        data[:4] = b"RCOL"
        with self.assertRaises(LayoutDetectionError):
            detect_rcol_layout(bytes(data))


if __name__ == "__main__":
    unittest.main()
