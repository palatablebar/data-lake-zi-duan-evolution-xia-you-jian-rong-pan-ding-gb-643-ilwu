"""不变量：守恒、幂等、单调、可复算。"""

from __future__ import annotations

from app import store
from app.modules.m1_intake import service as intake
from app.modules.m6_batches import service as batch_service
from app.modules.m7_migration import service as migration
from tests.fixtures import expected


def test_field_account_is_conserved(run):
    ledger = run["ledger"]
    fields = ledger["fields"]
    buckets = fields["buckets"]
    old_side = sum(buckets[key] for key in
                   ("unchanged", "renamed", "retyped", "required", "conflict", "deleted"))
    new_side = sum(buckets[key] for key in
                   ("unchanged", "renamed", "retyped", "required", "conflict", "added"))
    assert old_side == fields["old_total"] == expected.OLD_TOTAL
    assert new_side == fields["new_total"] == expected.NEW_TOTAL
    assert fields["old_gap"] == fields["new_gap"] == 0


def test_ref_account_is_conserved(run):
    refs = run["ledger"]["refs"]
    assert refs["reachable"] + refs["broken"] == refs["refs_total"]
    assert refs["refs_total"] == sum(len(item["refs"]) for item in run["refs"]["consumers"])
    assert refs["gap"] == 0
    assert refs["direct"] + refs["aliased"] >= refs["reachable"]


def test_intake_account_is_conserved(client):
    for name in ("schema_v2026_03.sql", "schema_v2026_06.sql",
                 "schema_v2026_09.sql", "metadata_extra.json"):
        stats = intake.ingest(name)["stats"]
        assert stats["accepted"] + stats["blocked"] == stats["column_lines"]
        assert stats["parsed"] + stats["parse_failures"] == stats["column_lines"]


def test_rerun_keeps_rows_and_totals(client):
    first = store.latest_run()
    before = migration.fields_csv_text(first)
    again = batch_service.execute()
    after = migration.fields_csv_text(again)
    assert again["run_id"] == first["run_id"]
    assert before == after
    assert again["ledger"]["counts"] == first["ledger"]["counts"]
    assert again["buckets"] == first["buckets"]
    assert store.count_runs() == 1


def test_export_text_is_deterministic(client):
    run = store.latest_run()
    assert migration.spec_markdown(run) == migration.spec_markdown(run)
    assert migration.manifest_json_text(run) == migration.manifest_json_text(run)
    assert migration.refs_csv_text(run) == migration.refs_csv_text(run)


def test_evidence_partition_covers_all_consumers(client):
    """凭证守恒：已确认 + 待补证 + 不受影响 = 全部下游，一个不重不漏。"""
    from app.modules.m8_evidence import service as evidence

    prepared = evidence.prepare_for_latest()
    version = prepared["version"]
    consumers = len(store.latest_run()["refs"]["consumers"])
    assert version["confirmed"] + version["pending"] + version["unaffected"] == consumers
    assert version["record_count"] == consumers
    nodes = [record["node"] for record in prepared["records"]]
    assert len(nodes) == len(set(nodes)) == consumers


def test_evidence_rerun_creates_no_duplicate_versions_or_records(client):
    from app.modules.m8_evidence import service as evidence

    first = evidence.prepare_for_latest()
    batch_service.execute()
    evidence.prepare_for_latest()
    evidence.prepare_for_latest()
    assert store.count_evidence_versions() == 1
    assert store.count_evidence_records(first["version"]["version_id"]) == \
        first["version"]["record_count"]


def test_failed_publish_never_deletes_run_or_records(client):
    from app.modules.m8_evidence import service as evidence

    prepared = evidence.prepare_for_latest()
    evidence.publish(prepared["version"]["version_id"])
    runs_before = store.count_runs()
    try:
        evidence.publish(prepared["version"]["version_id"], fail_after=True)
    except RuntimeError:
        pass
    assert store.count_runs() == runs_before
    assert store.latest_published_version() is not None
    assert store.count_evidence_records(prepared["version"]["version_id"]) == \
        prepared["version"]["record_count"]
