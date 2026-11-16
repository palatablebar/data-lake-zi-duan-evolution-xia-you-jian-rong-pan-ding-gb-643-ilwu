"""样例的期望值。改样例必须同时改这里，否则测试会挡住。"""

from __future__ import annotations

OLD_TOTAL = 13
NEW_TOTAL = 16

BUCKETS = {
    "unchanged": 4,
    "renamed": 1,
    "retyped": 5,
    "required": 1,
    "conflict": 1,
    "deleted": 1,
    "added": 4,
}

VERDICTS = {"COMPATIBLE": 10, "COMPATIBLE_BY_MAPPING": 1, "BREAKING": 6}

QUARANTINE_BLOCKED = 2
QUARANTINE_WARNINGS = 4

REFS_TOTAL = 17
REFS_REACHABLE = 14
REFS_BROKEN = 3
REFS_ALIASED = 2

CONSUMERS = 8
IMPACT_TOTAL = 5
IMPACT_DEPTHS = {"1": 3, "2": 2}
ONE_SIDED = 3
CYCLES = 1
SELF_LOOPS = 1

# M8 影响凭证：8 个下游 = 4 已确认 + 1 待补证 + 3 不受影响
EVIDENCE_CONFIRMED = 4
EVIDENCE_PENDING = 1
EVIDENCE_UNAFFECTED = 3
EVIDENCE_TOTAL = 8
EVIDENCE_PENDING_NODE = "ads.rpt_settle_daily"
EVIDENCE_MUTUAL_EDGES = 4
EVIDENCE_ONE_SIDED_EDGES = 3

BIG_ID = "9007199254740993"
BIG_ID_AS_FLOAT = int(float(BIG_ID))

DEFAULT_WINDOW_FROM = "2026-09-01T00:00:00+00:00"


def make_fields(rows: list[dict]) -> list[dict]:
    """构造带归一类型的字段，供纯规则测试使用。"""
    from app.modules.m2_types import service as types

    fields = []
    for index, row in enumerate(rows, start=1):
        fields.append({
            "field_id": str(row.get("field_id") or f"f{index}"),
            "table": row.get("table") or "dim_order",
            "name": row["name"],
            "name_key": row["name"].lower(),
            "type_raw": row.get("type") or "bigint",
            "required": bool(row.get("required", True)),
            "line": index,
            "version": row.get("version") or "v2026_06",
            "source": "fixture",
        })
    return types.normalize_fields(fields)
