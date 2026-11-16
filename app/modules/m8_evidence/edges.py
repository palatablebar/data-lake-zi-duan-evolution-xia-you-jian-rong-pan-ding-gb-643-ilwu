"""登记边核验：把上下游登记拆成双方一致边与单边边。

只认双方一致的边会漏掉真实报表：单边边仍保留为待补证路径，
但绝不能当成确认依据。每条边带 declared / missing 与原因码，页面与导出照实展示。
"""

from __future__ import annotations

MUTUAL = "mutual"
ONE_SIDED = "one_sided"


def classify(consumers: list[dict]) -> dict:
    """返回每条有向边（上游 -> 下游）的核验结果。

    mutual：A 的 downstream 含 B，且 B 的 upstream 含 A（反方向同理也算双方一致）。
    one_sided：只有一边登记，declared 记登记方向，missing 记对方缺的那一边。
    """
    names = {item["name"] for item in consumers}
    by_name = {item["name"]: item for item in consumers}
    edges: dict[tuple[str, str], dict] = {}

    def touch(up: str, down: str, declared: str, missing: str) -> None:
        if up not in names or down not in names:
            # 指向未登记节点的边无法核验，按单边待补证保留
            key = (up, down)
            edges.setdefault(key, {
                "upstream": up,
                "downstream": down,
                "status": ONE_SIDED,
                "declared": declared,
                "missing": missing,
                "reason": "REF_ONE_SIDED",
            })
            return
        key = (up, down)
        existing = edges.get(key)
        if existing is not None and existing["status"] == MUTUAL:
            return
        target = by_name[down]
        source = by_name[up]
        agreed = up in target["upstream"] and down in source["downstream"]
        if agreed:
            edges[key] = {
                "upstream": up,
                "downstream": down,
                "status": MUTUAL,
                "declared": "both",
                "missing": "",
                "reason": "",
            }
        elif existing is None:
            edges[key] = {
                "upstream": up,
                "downstream": down,
                "status": ONE_SIDED,
                "declared": declared,
                "missing": missing,
                "reason": "REF_ONE_SIDED",
            }

    for consumer in consumers:
        name = consumer["name"]
        for down in consumer["downstream"]:
            touch(name, down, "downstream", "upstream")
        for up in consumer["upstream"]:
            touch(up, name, "upstream", "downstream")

    return {
        "edges": [edges[key] for key in sorted(edges)],
        "mutual": sorted(key for key, edge in edges.items() if edge["status"] == MUTUAL),
        "one_sided": sorted(key for key, edge in edges.items()
                            if edge["status"] == ONE_SIDED),
    }


def graphs(consumers: list[dict]) -> dict:
    """两张反向传播图：确认图只含双方一致边；联合图含全部已登记边。

    键为下游节点，值为它的直接上游集合（影响沿 上游 -> 下游 传播）。
    """
    classified = classify(consumers)
    confirmed: dict[str, set[str]] = {item["name"]: set() for item in consumers}
    union: dict[str, set[str]] = {item["name"]: set() for item in consumers}
    for edge in classified["edges"]:
        up, down = edge["upstream"], edge["downstream"]
        if down not in union:
            union[down] = set()
            confirmed[down] = set()
        union[down].add(up)
        union.setdefault(up, set())
        confirmed.setdefault(up, set())
        if edge["status"] == MUTUAL:
            confirmed[down].add(up)
    return {"confirmed": confirmed, "union": union, "classified": classified}
