"""凭证种子：节点与变更字段之间的直接证据，全部来自身份解析，不靠登记边。

两类直接证据：
1. field  —— 节点 SQL 里引用的列经身份解析落到一个 BREAKING 字段（引用账可达）。
2. broken —— 节点的引用解析断链（删除 / 身份被占用 / 别名不安全），引用账 REF_BROKEN。
两者都经过 field_id 身份核对，是确认影响，不依赖上下游登记是否一致。
"""

from __future__ import annotations

from app.modules.m5_impact.service import changed_fields


def _field_label(table: str, column: str) -> str:
    return f"{table}.{column}"


def build(diff: dict, resolution: dict) -> dict:
    breaking_ids = {row["field_id"] for row in changed_fields(diff)}
    field_index: dict[str, dict] = {}
    for row in diff["rows"]:
        field_index[row["field_id"]] = row

    # 节点 -> 直接字段证据（经身份解析、且字段本版判破坏）
    field_seeds: dict[str, list[dict]] = {}
    for item in resolution.get("resolved") or []:
        fid = item.get("field_id")
        if fid not in breaking_ids:
            continue
        row = field_index.get(fid) or {}
        field_seeds.setdefault(item["consumer"], []).append({
            "kind": "field",
            "field_id": fid,
            "ref": item["ref"],
            "table": item["table"],
            "column": item["column"],
            "old_name": row.get("old_name") or item["column"],
            "new_name": row.get("new_name") or "",
            "bucket": row.get("bucket") or "",
            "reason": row.get("primary") or "BREAKING",
            "via": item.get("via") or "direct",
            "label": _field_label(item["table"], item["column"]),
        })

    # 节点 -> 断链证据（名字还在但身份换了、字段删除、改名同时换类型等）
    broken_seeds: dict[str, list[dict]] = {}
    for item in resolution.get("broken") or []:
        broken_seeds.setdefault(item["consumer"], []).append({
            "kind": "broken",
            "field_id": None,
            "ref": item["ref"],
            "table": item["table"],
            "column": item["column"],
            "old_name": item["column"],
            "new_name": "",
            "bucket": "broken_ref",
            "reason": "REF_BROKEN",
            "via": item.get("via") or "unknown_name",
            "detail": item.get("detail") or "",
            "label": f"断链 {item['ref']}",
        })

    seeds: dict[str, list[dict]] = {}
    for name in set(field_seeds) | set(broken_seeds):
        seeds[name] = sorted(
            field_seeds.get(name, []) + broken_seeds.get(name, []),
            key=lambda item: (item["label"], item["kind"]),
        )
    return {
        "seeds": seeds,
        "field_seeds": field_seeds,
        "broken_seeds": broken_seeds,
        "breaking_ids": sorted(breaking_ids),
    }
