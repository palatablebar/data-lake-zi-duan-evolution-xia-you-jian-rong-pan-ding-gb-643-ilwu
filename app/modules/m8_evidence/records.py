"""凭证记录与确定性凭证号。

凭证号由批次键 + 节点名 + 结论 + 依据内容哈希推出：
同一登记重跑同一审计，输入不变则凭证号不变，库内按 (run_id, evidence_id) 去重，
不会产生重复凭证；登记真改了，哈希跟着变，自然形成新凭证。
"""

from __future__ import annotations

import hashlib
import json

from app import manifest as manifest_mod

CONFIRMED = "CONFIRMED"
PENDING = "PENDING"
UNAFFECTED = "UNAFFECTED"


def evidence_id(batch_key: str, row: dict) -> str:
    payload = {
        "batch_key": batch_key,
        "node": row["name"],
        "status": row["status"],
        "path": row["path"],
        "basis": [
            {key: item.get(key) for key in ("kind", "field_id", "ref", "reason", "via")}
            for item in row["basis"]
        ],
        "edges": [
            {"from": edge["from"], "to": edge["to"], "status": edge["status"]}
            for edge in row["path_edges"]
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "ev-" + digest[:16]


def build_records(run: dict, verdict: dict) -> list[dict]:
    batch_key = run["batch_key"]
    records = []
    for row in verdict["rows"]:
        record = {
            "evidence_id": evidence_id(batch_key, row),
            "run_id": run["run_id"],
            "node": row["name"],
            "kind": row["kind"],
            "status": row["status"],
            "status_label": row["status_label"],
            "depth": row["depth"],
            "direct": bool(row["direct"]),
            "path": list(row["path"]),
            "path_text": " → ".join(row["path"]),
            "path_edges": list(row["path_edges"]),
            "basis": list(row["basis"]),
            "one_sided_edges": list(row["one_sided_edges"]),
        }
        records.append(record)
    return records


def audit_fingerprint(records: list[dict]) -> str:
    """整份审计的指纹：凭证号集合哈希，用于重跑前后比对。"""
    ids = sorted(record["evidence_id"] for record in records)
    return manifest_mod.sha256_bytes("\n".join(ids).encode("utf-8"))
