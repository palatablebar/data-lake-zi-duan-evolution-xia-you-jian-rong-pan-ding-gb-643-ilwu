"""M8 影响凭证：批次集成结论、幂等重跑、发布原子性、导出与页面。"""

from __future__ import annotations

import csv
import io

import pytest

from app import store
from app.modules.m6_batches import service as batch_service
from app.modules.m8_evidence import service as evidence_service
from tests.fixtures import expected


# ---------- 样例集成结论 ----------

def test_sample_verdict_has_three_statuses(run):
    verdict = evidence_service.build_verdict(run)
    assert verdict["counts"]["CONFIRMED"] == expected.EVIDENCE_CONFIRMED
    assert verdict["counts"]["PENDING"] == expected.EVIDENCE_PENDING
    assert verdict["counts"]["UNAFFECTED"] == expected.EVIDENCE_UNAFFECTED
    assert verdict["total"] == expected.EVIDENCE_TOTAL
    by_name = {row["name"]: row for row in verdict["rows"]}
    pending = by_name[expected.EVIDENCE_PENDING_NODE]
    assert pending["status"] == "PENDING"
    assert pending["one_sided_edges"], "待补证节点必须指出是哪条单边边"
    assert pending["basis"], "待补证也要带依据字段，不能只给状态"


def test_sample_confirmed_nodes_carry_identity_basis(run):
    verdict = evidence_service.build_verdict(run)
    for row in verdict["rows"]:
        if row["status"] != "CONFIRMED":
            continue
        assert row["basis"], f"已确认节点 {row['name']} 缺依据字段"
        labels = {item["label"] for item in row["basis"]}
        assert labels
    pay_daily = next(row for row in verdict["rows"]
                     if row["name"] == "dw.mv_pay_daily")
    assert "fct_pay.pay_amt" in {item["label"] for item in pay_daily["basis"]}
    assert any(item["field_id"] == "2003" for item in pay_daily["basis"])


def test_sample_unaffected_have_no_path(run):
    verdict = evidence_service.build_verdict(run)
    for row in verdict["rows"]:
        if row["status"] == "UNAFFECTED":
            assert row["path"] == []
            assert row["depth"] is None
            assert row["path_edges"] == []


def test_edge_audit_counts(run):
    verdict = evidence_service.build_verdict(run)
    statuses = [edge["status"] for edge in verdict["edges"]]
    assert statuses.count("mutual") == expected.EVIDENCE_MUTUAL_EDGES
    assert statuses.count("one_sided") == expected.EVIDENCE_ONE_SIDED_EDGES


# ---------- 幂等：登记不变重跑不新增重复凭证 ----------

def test_prepare_is_idempotent(run):
    first = evidence_service.prepare_version(run)
    again = evidence_service.prepare_version(run)
    assert first["version"]["version_id"] == again["version"]["version_id"]
    assert store.count_evidence_versions() == 1
    first_ids = [r["evidence_id"] for r in first["records"]]
    again_ids = [r["evidence_id"] for r in again["records"]]
    assert first_ids == again_ids
    # 同一 version_id 下凭证行不重复
    assert store.count_evidence_records(first["version"]["version_id"]) == \
        expected.EVIDENCE_TOTAL


def test_rerun_same_audit_adds_no_evidence(client, run):
    prepared = evidence_service.prepare_for_latest()
    batch_service.execute()  # 登记不变，命中批次键
    evidence_service.prepare_for_latest()
    assert store.count_evidence_versions() == 1
    assert store.count_evidence_records(prepared["version"]["version_id"]) == \
        expected.EVIDENCE_TOTAL


def test_fingerprint_stable_across_reruns(run):
    first = evidence_service.prepare_version(run)
    again = evidence_service.prepare_version(run)
    assert first["version"]["fingerprint"] == again["version"]["fingerprint"]


def test_first_prepare_is_new_then_reused(run):
    first = evidence_service.prepare_version(run)
    assert first["reused"] is False
    assert first["version"]["status"] == "draft"
    again = evidence_service.prepare_version(run)
    assert again["reused"] is True


def test_reprepare_after_publish_keeps_published_status(run):
    prepared = evidence_service.prepare_version(run)
    evidence_service.publish(prepared["version"]["version_id"])
    # 登记不变再次形成凭证：命中已有版本，发布状态不得被改回草稿
    again = evidence_service.prepare_version(run)
    assert again["reused"] is True
    assert again["version"]["status"] == "published"
    assert store.latest_published_version()["version_id"] == \
        prepared["version"]["version_id"]


# ---------- 发布原子性：失败保留完整旧版，记录不丢 ----------

def test_publish_switches_version_and_keeps_history(run):
    prepared = evidence_service.prepare_version(run)
    result = evidence_service.publish(prepared["version"]["version_id"])
    assert result["version"]["status"] == "published"
    live = store.latest_published_version()
    assert live["version_id"] == prepared["version"]["version_id"]
    assert store.count_evidence_records(live["version_id"]) == \
        live["record_count"] == expected.EVIDENCE_TOTAL


def test_publish_idempotent_when_already_published(run):
    prepared = evidence_service.prepare_version(run)
    evidence_service.publish(prepared["version"]["version_id"])
    again = evidence_service.publish(prepared["version"]["version_id"])
    assert again["reused"] is True
    assert store.count_evidence_versions() == 1


def test_failed_publish_keeps_previous_version_intact(client, run):
    first = evidence_service.prepare_for_latest()
    evidence_service.publish(first["version"]["version_id"])
    old_published = store.latest_published_version()
    old_records = store.list_evidence_records(old_published["version_id"])

    # 再形成一份草稿（同批次同 version_id），演练发布写库失败
    with pytest.raises(RuntimeError):
        evidence_service.publish(first["version"]["version_id"], fail_after=True)

    still = store.latest_published_version()
    assert still["version_id"] == old_published["version_id"]
    assert still["status"] == "published"
    # 旧版凭证一条不丢
    assert store.list_evidence_records(still["version_id"]) == old_records
    # 批次运行记录不受影响
    assert store.run_by_id(run["run_id"]) is not None
    assert store.count_runs() == 1


def test_publish_failure_api_returns_409_and_keeps_old(client):
    client.post("/api/evidence/prepare")
    client.post("/api/evidence/publish")
    before = client.get("/api/evidence").json()
    response = client.post("/api/evidence/publish", json={"fail_after": True})
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["reason"] == "EVIDENCE_PUBLISH_FAILED"
    after = client.get("/api/evidence").json()
    assert after["version"]["version_id"] == before["version"]["version_id"]
    assert after["version"]["status"] == "published"
    assert len(after["records"]) == expected.EVIDENCE_TOTAL


def test_superseded_version_remains_readable(client):
    first_prepared = client.post("/api/evidence/prepare").json()
    client.post("/api/evidence/publish")
    # 跑一个参数不同的新批次，产生新凭证版本
    client.post("/api/runs", json={"params": {"wave": "evidence-next"}})
    second = client.post("/api/evidence/prepare").json()
    client.post("/api/evidence/publish")
    versions = client.get("/api/evidence/versions").json()["versions"]
    statuses = {v["version_id"]: v["status"] for v in versions}
    assert statuses[first_prepared["version_id"]] == "superseded"
    assert statuses[second["version_id"]] == "published"
    # 被取代版本的记录仍可完整读出
    old_records = store.list_evidence_records(first_prepared["version_id"])
    assert len(old_records) == expected.EVIDENCE_TOTAL


# ---------- 接口、页面与导出 ----------

def test_prepare_api_shape(client):
    response = client.post("/api/evidence/prepare")
    assert response.status_code == 200
    body = response.json()
    assert body["confirmed"] == expected.EVIDENCE_CONFIRMED
    assert body["pending"] == expected.EVIDENCE_PENDING
    assert body["unaffected"] == expected.EVIDENCE_UNAFFECTED
    assert body["record_count"] == expected.EVIDENCE_TOTAL


def test_evidence_api_empty_state(empty_client):
    response = empty_client.get("/api/evidence")
    assert response.status_code == 200
    assert response.json()["version"] is None


def test_evidence_page_renders_three_statuses(client):
    client.post("/api/evidence/prepare")
    page = client.get("/evidence")
    assert page.status_code == 200
    assert "影响凭证" in page.text
    assert "已确认" in page.text and "待补证" in page.text and "不受影响" in page.text
    assert expected.EVIDENCE_PENDING_NODE in page.text


def test_evidence_csv_export_matches_records(client):
    client.post("/api/evidence/prepare")
    response = client.get("/api/export/evidence.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == expected.EVIDENCE_TOTAL
    by_node = {row["node"]: row for row in rows}
    assert by_node[expected.EVIDENCE_PENDING_NODE]["status"] == "PENDING"
    assert by_node[expected.EVIDENCE_PENDING_NODE]["edge_status"] == "one_sided"
    unaffected = [row for row in rows if row["status"] == "UNAFFECTED"]
    assert all(row["path"] == "" for row in unaffected)


def test_edges_csv_lists_one_sided_missing_side(client):
    client.post("/api/evidence/prepare")
    response = client.get("/api/export/evidence-edges.csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    one_sided = [row for row in rows if row["status"] == "one_sided"]
    assert len(one_sided) == expected.EVIDENCE_ONE_SIDED_EDGES
    assert all(row["missing"] for row in one_sided)
    assert all(row["reason"] == "REF_ONE_SIDED" for row in one_sided)


def test_markdown_export_contains_all_nodes(client):
    client.post("/api/evidence/prepare")
    response = client.get("/api/export/evidence.md")
    assert response.status_code == 200
    text = response.text
    for node in ("dw.mv_pay_daily", expected.EVIDENCE_PENDING_NODE, "dw.task_self"):
        assert node in text
    assert "单边登记边只形成待补证路径" in text


def test_export_404_without_evidence(empty_client):
    assert empty_client.get("/api/export/evidence.csv").status_code == 404
    assert empty_client.get("/api/export/evidence.md").status_code == 404
