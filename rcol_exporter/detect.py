from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from .binary import RCOL_MAGIC, RSZ_MAGIC, read_wstring, s32, u32, u64


class LayoutDetectionError(ValueError):
    """Raised when an RCOL outer layout cannot be inferred safely."""


CANONICAL_REQUEST_FIELDS = {
    "requestSetID": 0x00,
    "groupIndex": 0x04,
    "userDataObjectIndex": 0x08,
    "nativeShapeColliderObjectIndex": 0x0C,
    "status": 0x10,
    "requestSetIndex": 0x14,
    "nameOffset": 0x18,
    "keyNameOffset": 0x20,
    "keyHash": 0x28,
    "KeyNameMMHash": 0x2C,
}


@dataclass(frozen=True)
class RequestLayout:
    table_offset: int
    count: int
    stride: int
    fields: dict[str, int]
    score: int
    confidence: float
    header_count_offsets: tuple[int, ...] = ()
    pointer_field_offsets: tuple[int, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def end(self) -> int:
        return self.table_offset + self.count * self.stride

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_offset": _hx(self.table_offset),
            "count": self.count,
            "stride": _hx(self.stride),
            "fields": {name: _hx(offset) for name, offset in self.fields.items()},
            "score": self.score,
            "confidence": round(self.confidence, 4),
            "header_count_offsets": [_hx(value) for value in self.header_count_offsets],
            "pointer_field_offsets": [_hx(value) for value in self.pointer_field_offsets],
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class DetectedLayout:
    header_scan_size: int
    groups_offset: int
    group_count: int
    group_stride: int
    rsz_offset: int
    object_count: int
    request: RequestLayout
    string_start: int
    raw_u32: dict[int, int]
    raw_u64: dict[int, int]
    group_count_offsets: tuple[int, ...] = ()
    group_pointer_offsets: tuple[int, ...] = ()
    rsz_pointer_offsets: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()
    candidate_scores: tuple[dict[str, Any], ...] = ()

    @property
    def confidence(self) -> float:
        return self.request.confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "auto_detected",
            "confidence": round(self.confidence, 4),
            "header_scan_size": _hx(self.header_scan_size),
            "groups": {
                "offset": _hx(self.groups_offset),
                "count": self.group_count,
                "stride": _hx(self.group_stride),
                "count_field_offsets": [_hx(value) for value in self.group_count_offsets],
                "pointer_field_offsets": [_hx(value) for value in self.group_pointer_offsets],
            },
            "rsz": {
                "offset": _hx(self.rsz_offset),
                "object_count": self.object_count,
                "pointer_field_offsets": [_hx(value) for value in self.rsz_pointer_offsets],
            },
            "request_sets": self.request.to_dict(),
            "string_start": _hx(self.string_start),
            "warnings": list(self.warnings),
            "candidate_scores": list(self.candidate_scores),
        }


@dataclass(frozen=True)
class LayoutHint:
    request_stride: int
    request_fields: dict[str, int]
    group_stride: int = 0x50


@dataclass
class _GroupCandidate:
    offset: int
    count: int
    stride: int
    score: int
    count_offsets: tuple[int, ...]
    pointer_offsets: tuple[int, ...]


@dataclass
class _RequestCandidate:
    layout: RequestLayout
    group: _GroupCandidate


def detect_rcol_layout(data: bytes, hint: LayoutHint | None = None) -> DetectedLayout:
    """Infer an RCOL outer layout from structural invariants, not a version profile."""
    if len(data) < 48 or data[:4] != RCOL_MAGIC:
        raise LayoutDetectionError("not an RCOL file or header is truncated")

    rsz_offset = _find_rsz_offset(data)
    object_count = s32(data, rsz_offset + 8)
    if object_count < 0:
        raise LayoutDetectionError(f"negative RSZ object count: {object_count}")

    probe_end = min(rsz_offset, 0x100)
    probe_u64 = {
        offset: u64(data, offset)
        for offset in range(0x20, max(0x20, probe_end - 7), 4)
    }
    possible_header_ends = [
        value
        for value in probe_u64.values()
        if 0x40 <= value <= min(rsz_offset, 0x200) and value % 4 == 0
    ]
    header_scan_size = min(possible_header_ends, default=min(rsz_offset, 0x70))
    raw_u32 = {
        offset: u32(data, offset)
        for offset in range(4, max(4, header_scan_size - 3), 4)
    }
    raw_u64 = {
        offset: u64(data, offset)
        for offset in range(0x20, max(0x20, header_scan_size - 7), 4)
    }
    pointer_sources = _pointer_sources(raw_u64, len(data))
    rsz_pointer_offsets = tuple(pointer_sources.get(rsz_offset, ()))

    group_candidates = _detect_group_candidates(
        data,
        rsz_offset,
        raw_u32,
        pointer_sources,
        hint,
    )
    selected_groups = _select_group_candidates(group_candidates, 12)
    request_candidates: list[_RequestCandidate] = []
    # Most files expose enough independent evidence to validate the directory
    # hint (or the common baseline layout) directly.  This is a candidate
    # probe, not a version switch: any failed invariant falls through to the
    # exhaustive offset/stride search below.
    for group in selected_groups:
        request_candidates.extend(
            _detect_direct_request_candidates(
                data,
                rsz_offset,
                object_count,
                group,
                raw_u32,
                pointer_sources,
                hint,
            )
        )

    # RequestSet validation is the stronger discriminator.  Select a bounded
    # but count-diverse set so zero-filled table tails cannot crowd the real
    # Group count out merely by producing more valid-looking records.
    if not request_candidates:
        for group in selected_groups:
            request_candidates.extend(
                _detect_request_candidates(
                    data,
                    rsz_offset,
                    object_count,
                    group,
                    raw_u32,
                    pointer_sources,
                    hint,
                )
            )

    if object_count == 0 and not request_candidates:
        group = group_candidates[0]
        request_candidates.append(
            _empty_request_candidate(data, rsz_offset, group, raw_u32, pointer_sources, hint)
        )
    if not request_candidates:
        raise LayoutDetectionError("no structurally valid RequestSet table candidate")

    request_candidates.sort(key=lambda item: item.layout.score, reverse=True)
    best = request_candidates[0]
    second_score = request_candidates[1].layout.score if len(request_candidates) > 1 else None
    confidence = _candidate_confidence(best.layout, second_score)
    request = RequestLayout(
        table_offset=best.layout.table_offset,
        count=best.layout.count,
        stride=best.layout.stride,
        fields=best.layout.fields,
        score=best.layout.score,
        confidence=confidence,
        header_count_offsets=best.layout.header_count_offsets,
        pointer_field_offsets=best.layout.pointer_field_offsets,
        evidence={
            **best.layout.evidence,
            "score_margin": None if second_score is None else best.layout.score - second_score,
        },
    )
    fatal_evidence = (
        request.count > 0
        and (
            not request.evidence.get("request_index_sequence")
            or not request.evidence.get("object_partition_complete")
            or not request.evidence.get("group_indices_valid")
        )
    )
    if fatal_evidence or confidence < 0.7:
        raise LayoutDetectionError(
            f"ambiguous RequestSet layout (score={request.score}, confidence={confidence:.3f})"
        )

    string_start = _detect_string_start(
        data,
        request.end,
        pointer_sources,
    )
    warnings: list[str] = []
    if not request.header_count_offsets:
        warnings.append("inferred RequestSet count is not mirrored by a scanned header u32")
    if not rsz_pointer_offsets:
        warnings.append("RSZ section was found by magic scan rather than a header pointer")
    if request.count == 0 and object_count != 0:
        warnings.append("RSZ contains objects but no RequestSet records were inferred")

    summaries = tuple(
        {
            "table_offset": _hx(item.layout.table_offset),
            "count": item.layout.count,
            "stride": _hx(item.layout.stride),
            "score": item.layout.score,
            "group_count": item.group.count,
            "group_stride": _hx(item.group.stride),
        }
        for item in request_candidates[:8]
    )
    return DetectedLayout(
        header_scan_size=header_scan_size,
        groups_offset=best.group.offset,
        group_count=best.group.count,
        group_stride=best.group.stride,
        rsz_offset=rsz_offset,
        object_count=object_count,
        request=request,
        string_start=string_start,
        raw_u32=raw_u32,
        raw_u64=raw_u64,
        group_count_offsets=best.group.count_offsets,
        group_pointer_offsets=best.group.pointer_offsets,
        rsz_pointer_offsets=rsz_pointer_offsets,
        warnings=tuple(warnings),
        candidate_scores=summaries,
    )


def build_layout_hint(layouts: Iterable[DetectedLayout]) -> LayoutHint | None:
    """Build an ephemeral directory consensus; callers do not maintain it."""
    eligible = [layout for layout in layouts if layout.confidence >= 0.85 and layout.request.count > 1]
    if not eligible:
        return None
    signatures = Counter(
        (
            layout.request.stride,
            tuple(sorted(layout.request.fields.items())),
            layout.group_stride,
        )
        for layout in eligible
    )
    (stride, fields, group_stride), _ = signatures.most_common(1)[0]
    return LayoutHint(stride, dict(fields), group_stride)


def _find_rsz_offset(data: bytes) -> int:
    marker = b"RSZ\x00"
    candidates: list[tuple[int, int]] = []
    pos = data.find(marker, 4)
    while pos >= 0:
        if pos % 4 == 0 and pos + 48 <= len(data):
            version = u32(data, pos + 4)
            object_count = s32(data, pos + 8)
            instance_count = s32(data, pos + 12)
            instance_offset = u64(data, pos + 24)
            data_offset = u64(data, pos + 32)
            valid = (
                0 <= version <= 0x10000
                and 0 <= object_count <= 10_000_000
                and 0 <= instance_count <= 10_000_000
                and object_count <= instance_count
                and 0 <= instance_offset < len(data) - pos
                and 0 <= data_offset <= len(data) - pos
            )
            if valid:
                score = 100
                if instance_offset % 4 == 0 and data_offset % 4 == 0:
                    score += 10
                if instance_offset <= data_offset:
                    score += 10
                candidates.append((score, pos))
        pos = data.find(marker, pos + 4)
    if not candidates:
        raise LayoutDetectionError("embedded RSZ block was not found")
    candidates.sort(reverse=True)
    return candidates[0][1]


def _pointer_sources(raw_u64: dict[int, int], file_size: int) -> dict[int, tuple[int, ...]]:
    out: dict[int, list[int]] = {}
    for field_offset, value in raw_u64.items():
        if 0 < value <= file_size and value % 4 == 0:
            out.setdefault(value, []).append(field_offset)
    return {value: tuple(offsets) for value, offsets in out.items()}


def _detect_group_candidates(
    data: bytes,
    rsz_offset: int,
    raw_u32: dict[int, int],
    pointer_sources: dict[int, tuple[int, ...]],
    hint: LayoutHint | None,
) -> list[_GroupCandidate]:
    starts = [value for value in pointer_sources if 0x20 <= value < rsz_offset]
    if not starts:
        starts = [min(0x70, rsz_offset)]
    counts = sorted(set(raw_u32.values()))
    counts = [value for value in counts if 0 <= value <= 100_000]
    strides = _ordered_values(
        [hint.group_stride] if hint else [],
        [0x50],
        range(0x40, 0x81, 8),
    )
    candidates: list[_GroupCandidate] = []
    for start in starts:
        for count in counts:
            count_offsets = tuple(offset for offset, value in raw_u32.items() if value == count)
            for stride in strides:
                if stride < 0x50 or start + count * stride > rsz_offset:
                    continue
                score = 0
                if pointer_sources.get(start):
                    score += 20
                    if any(offset >= 0x30 for offset in pointer_sources[start]):
                        score += 10
                if count_offsets:
                    score += 10
                if stride == 0x50:
                    score += 4
                if hint and stride == hint.group_stride:
                    score += 6
                first_shape_offset = u64(data, start + 0x28) if count else 0
                table_end = start + count * stride
                if first_shape_offset:
                    if first_shape_offset < table_end or first_shape_offset > rsz_offset:
                        continue
                    table_gap = first_shape_offset - table_end
                    if table_gap == 0:
                        score += 120
                    else:
                        score -= min(80, table_gap // max(stride, 1))
                # Reject bad candidates from a representative sample.  A full
                # scan for every header integer interpreted as a Group count
                # is quadratic enough to hurt on large files, while the later
                # RequestSet/group-index checks still validate the winner.
                sampled_indices = _sample_indices(count, 16)
                valid_records = 0
                for index in sampled_indices:
                    base = start + index * stride
                    if base + 0x50 > len(data):
                        break
                    name_offset = u64(data, base + 0x10)
                    shape_count = u32(data, base + 0x1C)
                    shape_offset = u64(data, base + 0x28)
                    name_valid = _valid_wstring_pointer(data, name_offset)
                    shape_valid = (
                        shape_count <= 1_000_000
                        and (
                            (shape_count == 0 and (shape_offset == 0 or shape_offset <= rsz_offset))
                            or (0 < shape_offset < rsz_offset)
                        )
                    )
                    if name_valid and shape_valid:
                        valid_records += 1
                sampled_count = len(sampled_indices)
                if count == 0:
                    score += 5
                else:
                    score += 20
                    score += valid_records * 8
                    score -= (sampled_count - valid_records) * 30
                if sampled_count and valid_records != sampled_count:
                    continue
                candidates.append(
                    _GroupCandidate(
                        offset=start,
                        count=count,
                        stride=stride,
                        score=score,
                        count_offsets=count_offsets,
                        pointer_offsets=pointer_sources.get(start, ()),
                    )
                )
    if not candidates:
        raise LayoutDetectionError("no structurally valid Group table candidate")
    candidates.sort(key=lambda item: item.score, reverse=True)
    return _dedupe_groups(candidates)


def _sample_indices(count: int, limit: int) -> list[int]:
    if count <= 0:
        return []
    if count <= limit:
        return list(range(count))
    # Include both ends and evenly spaced interior records.  set() also
    # handles integer-rounding duplicates for small ranges.
    return sorted({round(index * (count - 1) / (limit - 1)) for index in range(limit)})


def _select_group_candidates(
    candidates: list[_GroupCandidate],
    limit: int,
) -> list[_GroupCandidate]:
    buckets: dict[tuple[int, int], list[_GroupCandidate]] = {}
    order: list[tuple[int, int]] = []
    for candidate in candidates:
        key = (candidate.offset, candidate.count)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(candidate)

    selected: list[_GroupCandidate] = []
    depth = 0
    while len(selected) < limit:
        added = False
        for key in order:
            bucket = buckets[key]
            if depth < len(bucket):
                selected.append(bucket[depth])
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        depth += 1
    return selected


def _detect_request_candidates(
    data: bytes,
    rsz_offset: int,
    object_count: int,
    group: _GroupCandidate,
    raw_u32: dict[int, int],
    pointer_sources: dict[int, tuple[int, ...]],
    hint: LayoutHint | None,
) -> list[_RequestCandidate]:
    starts = sorted(value for value in pointer_sources if rsz_offset < value < len(data))
    if object_count == 0:
        return []
    strides = _ordered_values(
        [hint.request_stride] if hint else [],
        [0x30],
        range(0x20, 0x61, 4),
    )
    candidates: list[_RequestCandidate] = []
    for start in starts:
        boundary = min((value for value in starts if value > start), default=len(data))
        for stride in strides:
            max_records = min(object_count, max(0, (boundary - start) // stride))
            if max_records <= 0:
                continue
            offsets = list(range(0, stride - 3, 4))
            req_offsets = _ordered_values(
                [hint.request_fields.get("requestSetIndex")] if hint else [],
                [CANONICAL_REQUEST_FIELDS["requestSetIndex"]],
                offsets,
            )
            for request_index_offset in req_offsets:
                if request_index_offset + 4 > stride:
                    continue
                count = _sequential_prefix(data, start, stride, request_index_offset, max_records)
                if count <= 0 or count > object_count:
                    continue
                rows = [
                    [u32(data, start + row * stride + offset) for offset in offsets]
                    for row in range(count)
                ]
                values = {
                    offset: [row[offset // 4] for row in rows]
                    for offset in offsets
                }
                object_offsets = [
                    offset
                    for offset in offsets
                    if offset != request_index_offset
                    and all(0 <= value <= object_count for value in values[offset])
                ]
                pair = _best_object_pair(values, object_offsets, object_count, request_index_offset)
                if pair is None:
                    continue
                user_offset, native_offset, pair_score, partition_complete = pair
                group_offsets = [
                    offset
                    for offset in offsets
                    if offset not in {request_index_offset, user_offset, native_offset}
                    and group.count > 0
                    and all(value < group.count for value in values[offset])
                ]
                if not group_offsets:
                    continue
                delta = request_index_offset - CANONICAL_REQUEST_FIELDS["requestSetIndex"]
                group_offset = min(
                    group_offsets,
                    key=lambda offset: (
                        0 if hint and offset == hint.request_fields.get("groupIndex") else 1,
                        abs(offset - (CANONICAL_REQUEST_FIELDS["groupIndex"] + delta)),
                    ),
                )
                fields = _infer_request_fields(
                    stride,
                    delta,
                    group_offset,
                    user_offset,
                    native_offset,
                    request_index_offset,
                    values,
                    hint,
                )
                count_offsets = tuple(offset for offset, value in raw_u32.items() if value == count)
                score = group.score + count * 40 + pair_score
                score += 50 if partition_complete else -200
                score += 25 if count_offsets else 0
                score += 20 if pointer_sources.get(start) else 0
                score += 15 if all(value < group.count for value in values[group_offset]) else -100
                score += max(0, 12 - abs(stride - 0x30))
                score += max(0, 12 - abs(delta))
                if hint and _matches_hint(stride, fields, hint):
                    score += 25
                evidence = {
                    "request_index_sequence": True,
                    "object_indices_valid": True,
                    "object_partition_complete": partition_complete,
                    "group_indices_valid": True,
                    "header_count_match": bool(count_offsets),
                    "header_pointer_match": bool(pointer_sources.get(start)),
                    "object_count": object_count,
                    "collider_object_count": object_count - count,
                }
                confidence = _evidence_confidence(evidence, count)
                candidates.append(
                    _RequestCandidate(
                        layout=RequestLayout(
                            table_offset=start,
                            count=count,
                            stride=stride,
                            fields=fields,
                            score=score,
                            confidence=confidence,
                            header_count_offsets=count_offsets,
                            pointer_field_offsets=pointer_sources.get(start, ()),
                            evidence=evidence,
                        ),
                        group=group,
                    )
                )
                if confidence >= 0.98 and count > 2 and stride == (hint.request_stride if hint else 0x30):
                    break
    return _dedupe_requests(candidates)


def _detect_direct_request_candidates(
    data: bytes,
    rsz_offset: int,
    object_count: int,
    group: _GroupCandidate,
    raw_u32: dict[int, int],
    pointer_sources: dict[int, tuple[int, ...]],
    hint: LayoutHint | None,
) -> list[_RequestCandidate]:
    if object_count <= 0 or group.count <= 0:
        return []
    starts = sorted(value for value in pointer_sources if rsz_offset < value < len(data))
    specs: list[tuple[int, dict[str, int], str]] = []
    if hint is not None:
        specs.append((hint.request_stride, dict(hint.request_fields), "directory_consensus"))
    specs.append((0x30, dict(CANONICAL_REQUEST_FIELDS), "baseline_candidate"))

    out: list[_RequestCandidate] = []
    seen_specs: set[tuple[int, tuple[tuple[str, int], ...]]] = set()
    for stride, fields, source in specs:
        signature = (stride, tuple(sorted(fields.items())))
        if signature in seen_specs:
            continue
        seen_specs.add(signature)
        core_names = (
            "groupIndex",
            "userDataObjectIndex",
            "nativeShapeColliderObjectIndex",
            "requestSetIndex",
        )
        if stride <= 0 or any(
            not isinstance(fields.get(name), int)
            or fields[name] < 0
            or fields[name] + 4 > stride
            for name in core_names
        ):
            continue

        for start in starts:
            boundary = min((value for value in starts if value > start), default=len(data))
            max_records = min(object_count, max(0, (boundary - start) // stride))
            if max_records <= 0:
                continue
            count = _sequential_prefix(
                data,
                start,
                stride,
                fields["requestSetIndex"],
                max_records,
            )
            if count <= 0 or count > object_count:
                continue
            count_offsets = tuple(offset for offset, value in raw_u32.items() if value == count)
            if not count_offsets or not pointer_sources.get(start):
                continue

            groups = [
                u32(data, start + row * stride + fields["groupIndex"])
                for row in range(count)
            ]
            users = [
                u32(data, start + row * stride + fields["userDataObjectIndex"])
                for row in range(count)
            ]
            natives = [
                u32(data, start + row * stride + fields["nativeShapeColliderObjectIndex"])
                for row in range(count)
            ]
            group_indices_valid = all(value < group.count for value in groups)
            object_indices_valid = all(
                0 <= user < object_count and user < native <= object_count
                for user, native in zip(users, natives)
            ) and all(users[index + 1] >= natives[index] for index in range(count - 1))
            collider_count = sum(
                (users[index + 1] if index + 1 < count else object_count) - native
                for index, native in enumerate(natives)
            ) if object_indices_valid else -1
            partition_complete = (
                object_indices_valid
                and users[0] == 0
                and collider_count == object_count - count
            )
            if not group_indices_valid or not partition_complete:
                continue

            evidence = {
                "request_index_sequence": True,
                "object_indices_valid": True,
                "object_partition_complete": True,
                "group_indices_valid": True,
                "header_count_match": True,
                "header_pointer_match": True,
                "object_count": object_count,
                "collider_object_count": object_count - count,
                "direct_candidate_source": source,
            }
            score = group.score + count * 40 + 350
            if source == "directory_consensus":
                score += 25
            out.append(
                _RequestCandidate(
                    layout=RequestLayout(
                        table_offset=start,
                        count=count,
                        stride=stride,
                        fields=fields,
                        score=score,
                        confidence=_evidence_confidence(evidence, count),
                        header_count_offsets=count_offsets,
                        pointer_field_offsets=pointer_sources.get(start, ()),
                        evidence=evidence,
                    ),
                    group=group,
                )
            )
    return _dedupe_requests(out)


def _empty_request_candidate(
    data: bytes,
    rsz_offset: int,
    group: _GroupCandidate,
    raw_u32: dict[int, int],
    pointer_sources: dict[int, tuple[int, ...]],
    hint: LayoutHint | None,
) -> _RequestCandidate:
    starts = sorted(value for value in pointer_sources if rsz_offset < value <= len(data))
    start = starts[0] if starts else len(data)
    stride = hint.request_stride if hint else 0x30
    fields = dict(hint.request_fields) if hint else dict(CANONICAL_REQUEST_FIELDS)
    count_offsets = tuple(offset for offset, value in raw_u32.items() if value == 0)
    evidence = {
        "request_index_sequence": True,
        "object_indices_valid": True,
        "object_partition_complete": True,
        "group_indices_valid": True,
        "header_count_match": bool(count_offsets),
        "header_pointer_match": bool(pointer_sources.get(start)),
        "object_count": 0,
        "collider_object_count": 0,
        "empty_table": True,
    }
    layout = RequestLayout(
        table_offset=start,
        count=0,
        stride=stride,
        fields=fields,
        score=group.score + 80,
        confidence=0.9,
        header_count_offsets=count_offsets,
        pointer_field_offsets=pointer_sources.get(start, ()),
        evidence=evidence,
    )
    return _RequestCandidate(layout=layout, group=group)


def _best_object_pair(
    values: dict[int, list[int]],
    offsets: list[int],
    object_count: int,
    request_index_offset: int,
) -> tuple[int, int, int, bool] | None:
    best: tuple[int, int, int, bool] | None = None
    for user_offset in offsets:
        users = values[user_offset]
        for native_offset in offsets:
            if native_offset == user_offset or native_offset == request_index_offset:
                continue
            natives = values[native_offset]
            if not all(0 <= user < object_count and user < native <= object_count for user, native in zip(users, natives)):
                continue
            if not all(users[index + 1] >= natives[index] for index in range(len(users) - 1)):
                continue
            score = 80
            score += sum(20 for user, native in zip(users, natives) if native == user + 1)
            score += 20 if users and users[0] == 0 else 0
            coverage = bool(users) and users[0] == 0 and all(
                users[index + 1] >= natives[index]
                for index in range(len(users) - 1)
            ) and object_count >= natives[-1]
            collider_count = sum(
                (users[index + 1] if index + 1 < len(users) else object_count) - native
                for index, native in enumerate(natives)
            )
            partition_complete = coverage and collider_count == object_count - len(users)
            if partition_complete:
                score += 120
            score += max(0, 12 - abs((native_offset - user_offset) - 4))
            candidate = (user_offset, native_offset, score, partition_complete)
            if best is None or candidate[2] > best[2]:
                best = candidate
    return best


def _infer_request_fields(
    stride: int,
    delta: int,
    group_offset: int,
    user_offset: int,
    native_offset: int,
    request_index_offset: int,
    values: dict[int, list[int]],
    hint: LayoutHint | None,
) -> dict[str, int]:
    fields: dict[str, int] = {
        "groupIndex": group_offset,
        "userDataObjectIndex": user_offset,
        "nativeShapeColliderObjectIndex": native_offset,
        "requestSetIndex": request_index_offset,
    }
    occupied = set(fields.values())
    for name, canonical in CANONICAL_REQUEST_FIELDS.items():
        if name in fields:
            continue
        hinted = hint.request_fields.get(name) if hint else None
        candidates = [hinted, canonical + delta, canonical]
        selected = next(
            (
                offset
                for offset in candidates
                if isinstance(offset, int)
                and 0 <= offset
                and offset + (8 if name in {"nameOffset", "keyNameOffset"} else 4) <= stride
                and offset not in occupied
            ),
            None,
        )
        if selected is not None:
            fields[name] = selected
            occupied.add(selected)

    if "status" not in fields:
        small_columns = [
            offset
            for offset, column in values.items()
            if offset not in occupied and all(value <= 0xFFFF for value in column)
        ]
        if small_columns:
            fields["status"] = min(small_columns, key=lambda value: abs(value - (0x10 + delta)))
    if "requestSetID" not in fields:
        leftovers = [offset for offset in values if offset not in occupied]
        if leftovers:
            fields["requestSetID"] = min(leftovers)
    return fields


def _sequential_prefix(data: bytes, start: int, stride: int, field_offset: int, maximum: int) -> int:
    count = 0
    for index in range(maximum):
        offset = start + index * stride + field_offset
        if offset + 4 > len(data) or u32(data, offset) != index:
            break
        count += 1
    return count


def _detect_string_start(
    data: bytes,
    request_end: int,
    pointer_sources: dict[int, tuple[int, ...]],
) -> int:
    candidates = sorted(value for value in pointer_sources if request_end <= value < len(data))
    for candidate in candidates:
        if read_wstring(data, candidate):
            return candidate
        for offset in range(candidate, min(len(data), candidate + 64), 2):
            if read_wstring(data, offset):
                return candidate
    return candidates[-1] if candidates else len(data)


def _valid_wstring_pointer(data: bytes, offset: int) -> bool:
    if offset == 0:
        return True
    if offset < 0 or offset + 1 >= len(data) or offset % 2:
        return False
    if data[offset : offset + 2] == b"\x00\x00":
        return True
    return bool(read_wstring(data, offset))


def _candidate_confidence(layout: RequestLayout, second_score: int | None) -> float:
    confidence = layout.confidence
    if second_score is None:
        return confidence
    margin = layout.score - second_score
    if margin >= 100:
        return min(1.0, confidence + 0.02)
    if margin <= 0:
        return max(0.7, confidence - 0.08)
    return confidence


def _evidence_confidence(evidence: dict[str, Any], count: int) -> float:
    weights = {
        "request_index_sequence": 0.22,
        "object_indices_valid": 0.18,
        "object_partition_complete": 0.25,
        "group_indices_valid": 0.15,
        "header_count_match": 0.10,
        "header_pointer_match": 0.10,
    }
    value = sum(weight for key, weight in weights.items() if evidence.get(key))
    if count == 1:
        value -= 0.05
    return max(0.0, min(1.0, value))


def _matches_hint(stride: int, fields: dict[str, int], hint: LayoutHint) -> bool:
    if stride != hint.request_stride:
        return False
    core = {
        "groupIndex",
        "userDataObjectIndex",
        "nativeShapeColliderObjectIndex",
        "requestSetIndex",
    }
    return all(fields.get(name) == hint.request_fields.get(name) for name in core)


def _ordered_values(*groups: Iterable[int | None]) -> list[int]:
    out: list[int] = []
    for group in groups:
        for value in group:
            if isinstance(value, int) and value not in out:
                out.append(value)
    return out


def _dedupe_groups(candidates: list[_GroupCandidate]) -> list[_GroupCandidate]:
    out: list[_GroupCandidate] = []
    seen: set[tuple[int, int, int]] = set()
    for item in candidates:
        key = (item.offset, item.count, item.stride)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _dedupe_requests(candidates: list[_RequestCandidate]) -> list[_RequestCandidate]:
    candidates.sort(key=lambda item: item.layout.score, reverse=True)
    out: list[_RequestCandidate] = []
    seen: set[tuple[Any, ...]] = set()
    for item in candidates:
        fields = item.layout.fields
        key = (
            item.layout.table_offset,
            item.layout.count,
            item.layout.stride,
            fields.get("groupIndex"),
            fields.get("userDataObjectIndex"),
            fields.get("nativeShapeColliderObjectIndex"),
            fields.get("requestSetIndex"),
            item.group.offset,
            item.group.count,
        )
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _hx(value: int) -> str:
    return f"0x{value:x}"
