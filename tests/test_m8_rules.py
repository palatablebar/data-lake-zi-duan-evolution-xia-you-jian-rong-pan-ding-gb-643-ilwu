"""M8 影响凭证纯规则：边核验、种子、三档传播与确定性凭证号。

构造小登记，显式覆盖：双方一致边确认、单边边只给待补证、环不爆栈、
无路径判不受影响、重排输入结论不变。
"""

from __future__ import annotations

from app.modules.m8_evidence import edges, propagate, records, seeds


def _consumer(name, refs=None, upstream=None, downstream=None, kind="view"):
    return {
        "name": name,
        "kind": kind,
        "sql": "",
        "refs": refs or [],
        "upstream": upstream or [],
        "downstream": downstream or [],
    }


def _diff(rows):
    return {"rows": rows}


def _breaking(fid, table, old, new=None, bucket="retyped", primary="TYPE_NARROW"):
    return {
        "field_id": fid, "table": table, "old_name": old, "new_name": new or old,
        "bucket": bucket, "primary": primary, "verdict": "BREAKING",
    }


def _resolved(consumer, fid, ref, table, column, via="direct"):
    return {
        "consumer": consumer, "kind": "view", "ref": ref, "table": table,
        "column": column, "via": via, "field_id": fid, "new_name": column,
        "detail": "",
    }


def _broken(consumer, ref, table, column, via="deleted", detail="字段已删除"):
    return {
        "consumer": consumer, "kind": "view", "ref": ref, "table": table,
        "column": column, "via": via, "field_id": None, "new_name": None,
        "detail": detail,
    }


def test_edges_split_mutual_and_one_sided():
    consumers = [
        _consumer("a", downstream=["b"]),       # a 认 b，b 的登记里没有 a
        _consumer("b", downstream=["c"]),
        _consumer("c", upstream=["b"]),         # b-c 双方一致
    ]
    result = edges.classify(consumers)
    status = {(e["upstream"], e["downstream"]): e["status"] for e in result["edges"]}
    assert status[("a", "b")] == edges.ONE_SIDED
    assert status[("b", "c")] == edges.MUTUAL
    one_sided = next(e for e in result["edges"]
                     if (e["upstream"], e["downstream"]) == ("a", "b"))
    assert one_sided["reason"] == "REF_ONE_SIDED"
    assert one_sided["missing"] == "upstream"
    graphs = edges.graphs(consumers)
    assert graphs["confirmed"]["c"] == {"b"}
    assert graphs["union"]["b"] == {"a"}
    assert graphs["confirmed"]["b"] == set()  # 单边边不进确认图


def test_one_sided_edge_yields_pending_not_confirmed():
    """核心规则：单边依赖不能当成确认影响，也不能直接漏掉。"""
    consumers = [
        _consumer("seed", downstream=["mid"]),
        _consumer("mid", upstream=["seed"], downstream=["report"]),  # seed-mid 一致
        _consumer("report", upstream=[]),                            # mid->report 只登记一边
    ]
    diff = _diff([_breaking("f1", "t", "col")])
    resolution = {
        "resolved": [_resolved("seed", "f1", "t.col", "t", "col")],
        "broken": [],
        "by_field": {"f1": {"seed"}},
    }
    seed_payload = seeds.build(diff, resolution)
    verdict = propagate.build(consumers, seed_payload, edges.graphs(consumers))
    by_name = {row["name"]: row for row in verdict["rows"]}
    assert by_name["seed"]["status"] == propagate.CONFIRMED
    assert by_name["mid"]["status"] == propagate.CONFIRMED
    assert by_name["report"]["status"] == propagate.PENDING
    pending = by_name["report"]
    assert pending["path"] == ["seed", "mid", "report"]
    assert pending["one_sided_edges"][0]["from"] == "mid"
    assert pending["one_sided_edges"][0]["missing"] == "upstream"
    # 待补证也要带依据字段，证明它为什么被挂起
    assert pending["basis"][0]["field_id"] == "f1"


def test_unreachable_node_is_unaffected_even_with_one_sided_registrations():
    consumers = [
        _consumer("seed", downstream=["lonely"]),  # 单边，指向的下游无其他路径
        _consumer("lonely", upstream=[]),
        _consumer("isolated", upstream=[], downstream=[]),
    ]
    diff = _diff([_breaking("f1", "t", "col")])
    resolution = {
        "resolved": [_resolved("seed", "f1", "t.col", "t", "col")],
        "broken": [],
        "by_field": {"f1": {"seed"}},
    }
    verdict = propagate.build(consumers, seeds.build(diff, resolution),
                              edges.graphs(consumers))
    by_name = {row["name"]: row for row in verdict["rows"]}
    # lonely 经单边边可达 -> 待补证，而不是不受影响
    assert by_name["lonely"]["status"] == propagate.PENDING
    assert by_name["isolated"]["status"] == propagate.UNAFFECTED
    assert by_name["isolated"]["path"] == []
    assert by_name["isolated"]["basis"] == []
    assert verdict["counts"] == {
        propagate.CONFIRMED: 1, propagate.PENDING: 1, propagate.UNAFFECTED: 1,
    }


def test_broken_ref_is_confirmed_seed_with_identity_basis():
    consumers = [_consumer("v1"), _consumer("v2")]
    diff = _diff([_breaking("f1", "dim_order", "status", bucket="deleted",
                            primary="FIELD_DELETED_REFERENCED")])
    resolution = {
        "resolved": [],
        "broken": [_broken("v1", "dim_order.status", "dim_order", "status",
                           via="rebound", detail="名字已被别的身份占用")],
        "by_field": {},
    }
    verdict = propagate.build(consumers, seeds.build(diff, resolution),
                              edges.graphs(consumers))
    by_name = {row["name"]: row for row in verdict["rows"]}
    row = by_name["v1"]
    assert row["status"] == propagate.CONFIRMED
    assert row["direct"] is True
    assert row["basis"][0]["reason"] == "REF_BROKEN"
    assert row["basis"][0]["label"] == "断链 dim_order.status"
    assert by_name["v2"]["status"] == propagate.UNAFFECTED


def test_cycle_does_not_blow_and_shortest_path_wins():
    consumers = [
        _consumer("seed", downstream=["ring_a"]),
        _consumer("ring_a", upstream=["seed"], downstream=["ring_b"]),
        _consumer("ring_b", upstream=["ring_a"], downstream=["ring_a"]),
    ]
    diff = _diff([_breaking("f1", "t", "col")])
    resolution = {
        "resolved": [_resolved("seed", "f1", "t.col", "t", "col")],
        "broken": [],
        "by_field": {"f1": {"seed"}},
    }
    verdict = propagate.build(consumers, seeds.build(diff, resolution),
                              edges.graphs(consumers))
    by_name = {row["name"]: row for row in verdict["rows"]}
    assert by_name["ring_a"]["depth"] == 1
    assert by_name["ring_b"]["depth"] == 2
    assert by_name["ring_b"]["path"] == ["seed", "ring_a", "ring_b"]
    assert all(row["status"] == propagate.CONFIRMED for row in verdict["rows"])


def test_mutual_path_confirms_but_mixed_path_stays_pending():
    """同节点若只能经含单边边的路径到达，即使图上另有环也不得升级为确认。"""
    consumers = [
        _consumer("seed", downstream=["x"]),
        _consumer("x", upstream=[]),                       # seed->x 单边
        _consumer("y", upstream=["x"], downstream=["x"]),  # x-y 成环且双方一致
    ]
    diff = _diff([_breaking("f1", "t", "col")])
    resolution = {
        "resolved": [_resolved("seed", "f1", "t.col", "t", "col")],
        "broken": [],
        "by_field": {"f1": {"seed"}},
    }
    verdict = propagate.build(consumers, seeds.build(diff, resolution),
                              edges.graphs(consumers))
    by_name = {row["name"]: row for row in verdict["rows"]}
    assert by_name["x"]["status"] == propagate.PENDING
    assert by_name["y"]["status"] == propagate.PENDING


def test_evidence_id_stable_and_changes_with_content():
    run = {"run_id": "fm-x", "batch_key": "batch-1"}
    row = {
        "name": "rpt", "status": propagate.CONFIRMED, "path": ["seed", "rpt"],
        "basis": [{"kind": "field", "field_id": "f1", "ref": "t.col",
                   "reason": "TYPE_NARROW", "via": "direct"}],
        "path_edges": [{"from": "seed", "to": "rpt", "status": "mutual"}],
    }
    first = records.evidence_id(run["batch_key"], row)
    second = records.evidence_id(run["batch_key"], dict(row))
    assert first == second and first.startswith("ev-")
    changed = dict(row, path=["seed", "other", "rpt"],
                   path_edges=[{"from": "seed", "to": "other", "status": "mutual"},
                               {"from": "other", "to": "rpt", "status": "mutual"}])
    assert records.evidence_id(run["batch_key"], changed) != first


def test_verdict_is_order_independent():
    consumers_a = [
        _consumer("seed", downstream=["mid"]),
        _consumer("mid", upstream=["seed"], downstream=["rep"]),
        _consumer("rep", upstream=[]),
    ]
    consumers_b = list(reversed(consumers_a))
    diff = _diff([_breaking("f1", "t", "col")])
    resolution = {
        "resolved": [_resolved("seed", "f1", "t.col", "t", "col")],
        "broken": [],
        "by_field": {"f1": {"seed"}},
    }
    va = propagate.build(consumers_a, seeds.build(diff, resolution),
                         edges.graphs(consumers_a))
    vb = propagate.build(consumers_b, seeds.build(diff, resolution),
                         edges.graphs(consumers_b))
    assert [(r["name"], r["status"], tuple(r["path"])) for r in va["rows"]] == \
           [(r["name"], r["status"], tuple(r["path"])) for r in vb["rows"]]
