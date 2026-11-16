"""凭证传播：种子沿登记边反向闭包，区分确认路径与待补证路径。

三档结论：
- CONFIRMED   有直接字段/断链证据，或只经双方一致边从确认节点传播到达。
- PENDING     无确认链路，只能经单边登记边到达；边缺对方登记，须补证后才能停任务。
- UNAFFECTED  联合图（含单边边）也走不到，本批次没有任何受影响路径。

环与自引用靠 visited 截止，深度取最短跳数；同层按节点名字典序，结论可复算。
"""

from __future__ import annotations

from app import config

CONFIRMED = "CONFIRMED"
PENDING = "PENDING"
UNAFFECTED = "UNAFFECTED"

STATUS_LABELS = {
    CONFIRMED: "已确认",
    PENDING: "待补证",
    UNAFFECTED: "不受影响",
}


def _shortest_paths(seeds: dict[str, list[dict]], graph: dict[str, set[str]],
                    limit: int) -> dict[str, dict]:
    """反向 BFS：graph[node] 是 node 的直接上游，影响从上游传到 node。

    记录每个节点的一条最短路径（含种子到该节点的节点序列）与沿途边。
    """
    # 反向邻接：上游 -> 登记了它的下游集合，避免每层全表扫描
    forward: dict[str, list[str]] = {}
    for node, ups in graph.items():
        for up in ups:
            forward.setdefault(up, []).append(node)

    depth = {name: 0 for name in seeds}
    parent: dict[str, str] = {}
    queue = sorted(seeds)
    while queue:
        node = queue.pop(0)
        if depth[node] >= limit:
            continue
        for nxt in sorted(forward.get(node, ())):
            if nxt in depth:
                continue
            depth[nxt] = depth[node] + 1
            parent[nxt] = node
            queue.append(nxt)

    paths: dict[str, dict] = {}
    for name in depth:
        chain = [name]
        cur = name
        while cur in parent:
            cur = parent[cur]
            chain.append(cur)
        chain.reverse()  # 种子 -> ... -> 当前节点
        paths[name] = {"depth": depth[name], "chain": chain, "parent": parent.get(name)}
    return paths


def _edge_lookup(edge_list: list[dict]) -> dict[tuple[str, str], dict]:
    return {(edge["upstream"], edge["downstream"]): edge for edge in edge_list}


def build(consumers: list[dict], seed_payload: dict, graph_payload: dict,
          max_depth: int | None = None) -> dict:
    limit = max_depth or config.MAX_IMPACT_DEPTH
    kinds = {item["name"]: item.get("kind") or "task" for item in consumers}
    names = sorted(item["name"] for item in consumers)
    seeds = seed_payload["seeds"]
    edge_list = graph_payload["classified"]["edges"]
    edge_by_pair = _edge_lookup(edge_list)
    confirmed_graph = graph_payload["confirmed"]
    union_graph = graph_payload["union"]

    confirmed_reach = _shortest_paths(seeds, confirmed_graph, limit)
    union_reach = _shortest_paths(seeds, union_graph, limit)

    # 每个节点收集依据字段：自己的种子 + 最短路径上各节点种子，按标签去重
    def basis_for(chain: list[str]) -> list[dict]:
        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        for node_name in chain:
            for seed in seeds.get(node_name, []):
                key = (seed["kind"], seed["label"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(seed)
        return sorted(out, key=lambda item: (item["label"], item["kind"]))

    def path_edges(chain: list[str]) -> list[dict]:
        out = []
        for up, down in zip(chain, chain[1:]):
            edge = edge_by_pair.get((up, down))
            if edge is None:
                edge = {"upstream": up, "downstream": down, "status": "one_sided",
                        "declared": "unknown", "missing": "", "reason": "REF_ONE_SIDED"}
            out.append({
                "from": up,
                "to": down,
                "status": edge["status"],
                "declared": edge["declared"],
                "missing": edge.get("missing") or "",
                "reason": edge.get("reason") or "",
            })
        return out

    rows: list[dict] = []
    counts = {CONFIRMED: 0, PENDING: 0, UNAFFECTED: 0}
    for name in names:
        if name in confirmed_reach:
            status = CONFIRMED
            reach = confirmed_reach[name]
        elif name in union_reach:
            status = PENDING
            reach = union_reach[name]
        else:
            status = UNAFFECTED
            reach = None
        counts[status] += 1

        if reach is None:
            rows.append({
                "name": name,
                "kind": kinds.get(name, "task"),
                "status": status,
                "status_label": STATUS_LABELS[UNAFFECTED],
                "depth": None,
                "path": [],
                "path_edges": [],
                "basis": [],
                "direct": False,
                "one_sided_edges": [],
            })
            continue

        chain = reach["chain"]
        edges_on_path = path_edges(chain)
        rows.append({
            "name": name,
            "kind": kinds.get(name, "task"),
            "status": status,
            "status_label": STATUS_LABELS[status],
            "depth": reach["depth"],
            "path": chain,
            "path_edges": edges_on_path,
            "basis": basis_for(chain),
            "direct": name in seeds,
            "one_sided_edges": [edge for edge in edges_on_path
                                if edge["status"] != "mutual"],
        })

    order = {CONFIRMED: 0, PENDING: 1, UNAFFECTED: 2}
    rows.sort(key=lambda row: (order[row["status"]],
                               row["depth"] if row["depth"] is not None else 999,
                               row["name"]))
    return {
        "rows": rows,
        "counts": counts,
        "total": len(rows),
        "seed_nodes": sorted(seeds),
        "edges": edge_list,
        "mutual_edge_count": len(graph_payload["classified"]["mutual"]),
        "one_sided_edge_count": len(graph_payload["classified"]["one_sided"]),
    }
