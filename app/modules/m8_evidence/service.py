"""影响凭证编排：种子 → 边核验 → 传播分档 → 确定性凭证号 → 草稿/发布。

幂等：凭证号由批次键与凭证内容哈希推出，同一登记重跑同一审计，
命中同一批次键直接复用，库内不新增重复凭证。
发布：单事务替换发布版，失败回滚，旧版凭证与批次记录原样保留。
"""

from __future__ import annotations

from app import config, errors, store
from app.modules.m8_evidence import edges, propagate, records, seeds


def build_verdict(run: dict) -> dict:
    """纯计算：从一个已存批次推出每个下游的三档凭证结论。"""
    consumers = run["refs"]["consumers"]
    resolution = run.get("resolution") or {}
    # resolution 落库时 by_field 被转成排序后的 list，这里恢复成集合口径
    resolution = dict(resolution)
    resolution["by_field"] = {
        key: set(value) for key, value in (resolution.get("by_field") or {}).items()
    }
    seed_payload = seeds.build({"rows": run["diff"]}, resolution)
    graph_payload = edges.graphs(consumers)
    verdict = propagate.build(consumers, seed_payload, graph_payload)
    verdict["seed_payload"] = seed_payload
    return verdict


def _version_id(run: dict) -> str:
    return "evv-" + run["batch_key"][:16]


def prepare_version(run: dict) -> dict:
    """形成凭证草稿并落库；同一批次重复调用返回同一版本，不新增行、不改发布状态。"""
    version_id = _version_id(run)
    verdict = build_verdict(run)
    evidence_records = records.build_records(run, verdict)
    fingerprint = records.audit_fingerprint(evidence_records)
    counts = verdict["counts"]
    version = {
        "version_id": version_id,
        "run_id": run["run_id"],
        "batch_key": run["batch_key"],
        "fingerprint": fingerprint,
        "status": "draft",
        "record_count": len(evidence_records),
        "confirmed": counts[propagate.CONFIRMED],
        "pending": counts[propagate.PENDING],
        "unaffected": counts[propagate.UNAFFECTED],
        "created_at": config.now_utc().isoformat(),
        "published_at": None,
        "superseded_at": None,
    }
    created = store.save_evidence_version(version, evidence_records)
    return {
        "version": store.get_evidence_version(version_id),
        "records": store.list_evidence_records(version_id),
        "verdict": verdict,
        "reused": not created,
    }


def prepare_for_latest() -> dict:
    run = store.latest_run()
    if run is None:
        raise errors.NotFound("还没有批次，先在版本历史页跑一次")
    return prepare_version(run)


def publish(version_id: str | None = None, *, fail_after: bool = False) -> dict:
    """发布凭证版本。

    fail_after 仅供故障演练：在新草稿落库后、提交前制造一次失败，
    验证旧发布版完整保留、记录不丢。
    """
    if version_id is None:
        run = store.latest_run()
        if run is None:
            raise errors.NotFound("还没有批次，先在版本历史页跑一次")
        version_id = _version_id(run)

    before = store.latest_published_version()
    prepared = store.get_evidence_version(version_id)
    if prepared is None:
        raise errors.NotFound("凭证版本不存在，先形成凭证", detail={"version_id": version_id})
    if prepared["status"] == "published" and not fail_after:
        return {"version": prepared, "reused": True, "previous": before}

    if fail_after:
        # 走完整事务路径再主动回滚，模拟发布写库失败
        from app import store as store_mod

        store_mod.simulate_publish_failure(version_id, config.now_utc().isoformat())

    published = store.publish_evidence_version(version_id, config.now_utc().isoformat())
    after = store.latest_published_version()
    if before is not None:
        superseded = store.get_evidence_version(before["version_id"])
    else:
        superseded = None
    return {
        "version": published,
        "reused": False,
        "previous": before,
        "superseded": superseded,
        "records_preserved": after is not None
        and store.count_evidence_records(version_id) == published["record_count"],
    }


def latest_evidence() -> dict:
    """页面与接口的读取口径：优先发布版，没有发布版时给最新草稿。"""
    version = store.latest_published_version()
    stage = "published"
    if version is None:
        versions = store.list_evidence_versions(1)
        version = versions[0] if versions else None
        stage = "draft"
    if version is None:
        return {"version": None, "records": [], "stage": None}
    return {
        "version": version,
        "records": store.list_evidence_records(version["version_id"]),
        "stage": stage,
    }
