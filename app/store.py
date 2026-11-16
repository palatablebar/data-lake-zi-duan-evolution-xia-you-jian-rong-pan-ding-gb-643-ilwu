"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_versions (
    version_id     TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    batch_key      TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    status         TEXT NOT NULL,
    record_count   INTEGER NOT NULL,
    confirmed      INTEGER NOT NULL,
    pending        INTEGER NOT NULL,
    unaffected     INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    published_at   TEXT,
    superseded_at  TEXT
);
CREATE TABLE IF NOT EXISTS evidence_records (
    evidence_id    TEXT NOT NULL,
    version_id     TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    node           TEXT NOT NULL,
    status         TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    PRIMARY KEY (version_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence_records(run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_version_status ON evidence_versions(status);
"""


def db_path() -> Path:
    raw = os.environ.get("FIELDMAP_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv",
                      "evidence_versions", "evidence_records"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
                int(run["version_seq"]), run["input_sha256"], run["ruleset"],
                run["created_at"], run["effective_from"], run["effective_to"],
                _dumps(run["summary"]), _dumps(run),
            ),
        )


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default


# ---------- 影响凭证 ----------

def save_evidence_version(version: dict, records: list[dict]) -> bool:
    """落一份草稿凭证版本；同 version_id 已存在则原样保留（幂等，不改状态）。

    返回 True 表示新写入，False 表示命中已有版本。
    """
    with _LOCK, _connect() as conn:
        existing = conn.execute(
            "SELECT version_id FROM evidence_versions WHERE version_id=?",
            (version["version_id"],),
        ).fetchone()
        if existing is not None:
            return False
        conn.execute(
            """INSERT INTO evidence_versions (version_id, run_id, batch_key, fingerprint,
                   status, record_count, confirmed, pending, unaffected, created_at,
                   published_at, superseded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version["version_id"], version["run_id"], version["batch_key"],
                version["fingerprint"], version["status"], int(version["record_count"]),
                int(version["confirmed"]), int(version["pending"]),
                int(version["unaffected"]), version["created_at"],
                version.get("published_at"), version.get("superseded_at"),
            ),
        )
        conn.executemany(
            """INSERT OR IGNORE INTO evidence_records (evidence_id, version_id, run_id,
                   node, status, payload_json) VALUES (?,?,?,?,?,?)""",
            [
                (
                    record["evidence_id"], version["version_id"], record["run_id"],
                    record["node"], record["status"], _dumps(record),
                )
                for record in records
            ],
        )
        return True


def get_evidence_version(version_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM evidence_versions WHERE version_id=?", (version_id,)
        ).fetchone()
    return dict(row) if row else None


def get_evidence_version_by_run(run_id: str, status: str | None = None) -> dict | None:
    with _LOCK, _connect() as conn:
        if status is None:
            row = conn.execute(
                "SELECT * FROM evidence_versions WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM evidence_versions WHERE run_id=? AND status=?
                   ORDER BY rowid DESC LIMIT 1""",
                (run_id, status),
            ).fetchone()
    return dict(row) if row else None


def list_evidence_records(version_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM evidence_records WHERE version_id=? ORDER BY node",
            (version_id,),
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def count_evidence_records(version_id: str) -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence_records WHERE version_id=?", (version_id,)
        ).fetchone()
    return int(row["n"])


def latest_published_version() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT * FROM evidence_versions WHERE status='published'
               ORDER BY published_at DESC, rowid DESC LIMIT 1"""
        ).fetchone()
    return dict(row) if row else None


def list_evidence_versions(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT version_id, run_id, batch_key, fingerprint, status, record_count,
                      confirmed, pending, unaffected, created_at, published_at, superseded_at
               FROM evidence_versions ORDER BY rowid DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def publish_evidence_version(version_id: str, published_at: str) -> dict:
    """单事务发布：草稿转 published，旧 published 转 superseded。

    任一步失败整体回滚，旧版凭证保持完整可读；runs 表不在事务内改动，
    批次记录数据不会丢失。返回发布后的版本行。
    """
    with _LOCK, _connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            target = conn.execute(
                "SELECT * FROM evidence_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if target is None:
                conn.execute("ROLLBACK")
                raise KeyError(version_id)
            record_count = conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_records WHERE version_id=?",
                (version_id,),
            ).fetchone()["n"]
            if record_count == 0 or record_count != target["record_count"]:
                conn.execute("ROLLBACK")
                raise ValueError("evidence records incomplete")
            conn.execute(
                """UPDATE evidence_versions SET status='superseded', superseded_at=?
                   WHERE status='published' AND version_id<>?""",
                (published_at, version_id),
            )
            conn.execute(
                """UPDATE evidence_versions SET status='published', published_at=?
                   WHERE version_id=?""",
                (published_at, version_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_evidence_version(version_id)


def count_evidence_versions() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM evidence_versions").fetchone()
    return int(row["n"])


def simulate_publish_failure(version_id: str, published_at: str) -> None:
    """故障演练：执行与真实发布相同的写操作，但在提交前回滚并抛错。

    用于证明发布失败时旧 published 版本与全部凭证记录原样保留。
    """
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE evidence_versions SET status='superseded', superseded_at=?
               WHERE status='published' AND version_id<>?""",
            (published_at, version_id),
        )
        conn.execute(
            """UPDATE evidence_versions SET status='published', published_at=?
               WHERE version_id=?""",
            (published_at, version_id),
        )
        conn.execute("ROLLBACK")
        raise RuntimeError("simulated publish failure")
