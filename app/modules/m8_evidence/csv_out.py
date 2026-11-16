"""影响凭证导出：每个下游一行，结论、依据字段、传播路径与边核验情况都可对账。"""

from __future__ import annotations

import csv
import io

from app.modules.m8_evidence.propagate import STATUS_LABELS

EVIDENCE_HEADER = [
    "evidence_id", "node", "kind", "status", "status_label", "depth", "direct",
    "basis_fields", "basis_field_ids", "basis_reasons", "path", "edge_status",
    "one_sided_edges",
]

EDGE_HEADER = ["upstream", "downstream", "status", "declared", "missing", "reason"]


def _buffer() -> tuple[io.StringIO, "csv.writer"]:
    buffer = io.StringIO()
    return buffer, csv.writer(buffer, lineterminator="\n")


def evidence_csv(records: list[dict]) -> str:
    buffer, writer = _buffer()
    writer.writerow(EVIDENCE_HEADER)
    for record in records:
        basis = record.get("basis") or []
        edge_status = "→".join(edge["status"] for edge in record.get("path_edges") or [])
        one_sided = ";".join(
            f"{edge['from']}->{edge['to']}（缺{edge['missing']}登记）"
            for edge in record.get("one_sided_edges") or []
        )
        writer.writerow([
            record["evidence_id"],
            record["node"],
            record.get("kind") or "",
            record["status"],
            record.get("status_label") or STATUS_LABELS.get(record["status"], ""),
            "" if record.get("depth") is None else record["depth"],
            "Y" if record.get("direct") else "N",
            "；".join(item["label"] for item in basis),
            "；".join(str(item["field_id"]) for item in basis if item.get("field_id")),
            "；".join(sorted({str(item["reason"]) for item in basis})),
            record.get("path_text") or "",
            edge_status,
            one_sided,
        ])
    return buffer.getvalue()


def edges_csv(edge_list: list[dict]) -> str:
    buffer, writer = _buffer()
    writer.writerow(EDGE_HEADER)
    for edge in edge_list:
        writer.writerow([
            edge["upstream"], edge["downstream"], edge["status"],
            edge.get("declared") or "", edge.get("missing") or "",
            edge.get("reason") or "",
        ])
    return buffer.getvalue()


def evidence_markdown(version: dict, records: list[dict], stage: str) -> str:
    lines = [
        f"# 影响凭证 {version['version_id']}",
        "",
        f"- 批次：{version['run_id']}",
        f"- 版本状态：{stage}（{version['status']}）",
        f"- 凭证指纹：{version['fingerprint']}",
        f"- 已确认 {version['confirmed']}　待补证 {version['pending']}　不受影响 {version['unaffected']}",
        "",
        "| 节点 | 结论 | 深度 | 直接证据 | 依据字段 | 传播路径 | 路径边核验 |",
        "|---|---|---|---|---|---|---|",
    ]
    for record in records:
        basis = "；".join(item["label"] for item in record.get("basis") or []) or "—"
        edges_on_path = record.get("path_edges") or []
        edge_text = "，".join(
            f"{edge['from']}→{edge['to']}({'一致' if edge['status'] == 'mutual' else '单边'})"
            for edge in edges_on_path
        ) or "—"
        lines.append(
            "| {node} | {label} | {depth} | {direct} | {basis} | {path} | {edge} |".format(
                node=record["node"],
                label=record.get("status_label") or STATUS_LABELS[record["status"]],
                depth=("—" if record.get("depth") is None else record["depth"]),
                direct=("是" if record.get("direct") else "否"),
                basis=basis,
                path=record.get("path_text") or "—",
                edge=edge_text,
            )
        )
    lines.append("")
    lines.append("> 单边登记边只形成待补证路径，不作为确认停任务的依据。")
    lines.append("")
    return "\n".join(lines)
