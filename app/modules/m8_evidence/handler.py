"""M8 入口：影响凭证页、凭证形成/发布接口与导出。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import errors, store, web
from app.modules.m8_evidence import csv_out, service

router = APIRouter()


def _csv(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/evidence", response_class=HTMLResponse)
def evidence_page(request: Request):
    payload = service.latest_evidence()
    version = payload.get("version")
    records = payload.get("records") or []
    edge_list: list[dict] = []
    if records:
        # 边清单从最新草稿/发布版本的批次里取，保证与凭证同口径
        run = store.run_by_id(version["run_id"])
        if run is not None:
            verdict = service.build_verdict(run)
            edge_list = verdict["edges"]
    return web.page_or_404(
        request,
        "evidence.html",
        "evidence",
        store.latest_run(),
        version=version,
        records=records,
        stage=payload.get("stage"),
        edge_list=edge_list,
        versions=store.list_evidence_versions(),
    )


@router.post("/api/evidence/prepare")
def prepare_evidence():
    """按当前批次形成凭证草稿；登记不变时重复调用不新增凭证。"""
    result = service.prepare_for_latest()
    version = result["version"]
    return {
        "version_id": version["version_id"],
        "run_id": version["run_id"],
        "status": version["status"],
        "fingerprint": version["fingerprint"],
        "record_count": version["record_count"],
        "confirmed": version["confirmed"],
        "pending": version["pending"],
        "unaffected": version["unaffected"],
        "reused": result["reused"],
    }


@router.post("/api/evidence/publish")
def publish_evidence(payload: dict | None = None):
    body = payload or {}
    fail_after = bool(body.get("fail_after"))
    try:
        result = service.publish(body.get("version_id"), fail_after=fail_after)
    except RuntimeError as exc:
        # 演练的失败也要结构化返回，便于页面展示旧版仍在
        raise errors.Conflict(
            "凭证发布失败，已回滚，旧版凭证完整保留",
            detail={"reason": "EVIDENCE_PUBLISH_FAILED", "message": str(exc)},
        )
    version = result["version"]
    return {
        "version_id": version["version_id"],
        "status": version["status"],
        "published_at": version["published_at"],
        "reused": result.get("reused", False),
        "previous_version": (result.get("previous") or {}).get("version_id"),
        "records_preserved": result.get("records_preserved"),
    }


@router.get("/api/evidence")
def evidence_api():
    payload = service.latest_evidence()
    version = payload.get("version")
    if version is None:
        return {"version": None, "records": [], "stage": None}
    return {
        "version": {key: version[key] for key in (
            "version_id", "run_id", "batch_key", "fingerprint", "status",
            "record_count", "confirmed", "pending", "unaffected",
            "created_at", "published_at", "superseded_at")},
        "stage": payload["stage"],
        "records": payload["records"],
    }


@router.get("/api/evidence/versions")
def evidence_versions_api():
    return {"versions": store.list_evidence_versions()}


@router.get("/api/export/evidence.csv")
def export_evidence_csv():
    payload = service.latest_evidence()
    if not payload.get("version"):
        raise errors.NotFound("还没有凭证，先在影响凭证页形成凭证")
    version = payload["version"]
    return _csv(csv_out.evidence_csv(payload["records"]),
                f"{version['version_id']}-evidence.csv")


@router.get("/api/export/evidence-edges.csv")
def export_edges_csv():
    payload = service.latest_evidence()
    version = payload.get("version")
    if not version:
        raise errors.NotFound("还没有凭证，先在影响凭证页形成凭证")
    run = store.run_by_id(version["run_id"])
    edge_list = service.build_verdict(run)["edges"]
    return _csv(csv_out.edges_csv(edge_list),
                f"{version['version_id']}-edges.csv")


@router.get("/api/export/evidence.md")
def export_evidence_md():
    payload = service.latest_evidence()
    version = payload.get("version")
    if not version:
        raise errors.NotFound("还没有凭证，先在影响凭证页形成凭证")
    body = csv_out.evidence_markdown(version, payload["records"], payload["stage"])
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{version["version_id"]}.md"'},
    )
