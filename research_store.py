"""Local persistent research projects with immutable evidence snapshots.

This is a single-user/local-first store. It is deliberately not an enterprise
multi-tenant database; service deployments should provide authenticated storage
and tenant isolation outside this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from research_evidence import normalize_text, normalize_url

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path.home() / ".cache" / "web-search" / "research.db"
MAX_SOURCES_PER_RUN = 100
MAX_CONTENT_CHARS = 100_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_project_id(project_id: str) -> str:
    value = re.sub(r"\s+", " ", str(project_id or "")).strip()
    if not value or len(value) > 100:
        raise ValueError("project ID must contain 1-100 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError("project ID contains control characters")
    return value


class ResearchStore:
    """SQLite project store with content-addressed source snapshots and run diffs."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("WEB_SEARCH_RESEARCH_DB") or DEFAULT_DB_PATH).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def _session(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._session() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"research database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version == 0:
                conn.executescript("""
                    CREATE TABLE projects (
                        project_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_run_at TEXT
                    );
                    CREATE TABLE research_runs (
                        run_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                        created_at TEXT NOT NULL,
                        query TEXT NOT NULL,
                        query_hash TEXT NOT NULL,
                        model TEXT,
                        synthesis TEXT,
                        summary_json TEXT NOT NULL
                    );
                    CREATE INDEX research_runs_project_time
                        ON research_runs(project_id, created_at DESC);
                    CREATE TABLE research_sources (
                        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                        source_id TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        url TEXT NOT NULL,
                        title TEXT NOT NULL,
                        host TEXT NOT NULL,
                        publisher_id TEXT,
                        source_type TEXT,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        content TEXT NOT NULL,
                        PRIMARY KEY(project_id, source_id, content_hash)
                    );
                    CREATE INDEX research_sources_project_url
                        ON research_sources(project_id, url);
                    CREATE TABLE run_sources (
                        run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE CASCADE,
                        project_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        source_role TEXT NOT NULL,
                        PRIMARY KEY(run_id, source_id),
                        FOREIGN KEY(project_id, source_id, content_hash)
                            REFERENCES research_sources(project_id, source_id, content_hash)
                    );
                    CREATE TABLE run_claims (
                        run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE CASCADE,
                        claim_key TEXT NOT NULL,
                        claim_id TEXT NOT NULL,
                        claim_text TEXT NOT NULL,
                        status TEXT NOT NULL,
                        PRIMARY KEY(run_id, claim_key)
                    );
                    CREATE INDEX run_claims_key ON run_claims(claim_key);
                    CREATE TABLE claim_evidence (
                        run_id TEXT NOT NULL,
                        claim_key TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_role TEXT NOT NULL,
                        stance TEXT NOT NULL,
                        quote TEXT NOT NULL,
                        PRIMARY KEY(run_id, claim_key, source_id, stance, quote),
                        FOREIGN KEY(run_id, claim_key)
                            REFERENCES run_claims(run_id, claim_key) ON DELETE CASCADE
                    );
                """)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        try:
            if self.path.exists():
                os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _collect_sources(
        source_metadata: Iterable[dict[str, Any]],
        extracted: Iterable[dict[str, Any]],
        source_profiles: Iterable[dict[str, Any]],
        counter_sources: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        metadata_by_url: dict[str, dict[str, Any]] = {}
        for item in [*source_metadata, *counter_sources]:
            url = str(item.get("url") or item.get("href") or "").strip()
            key = normalize_url(url)
            if key:
                metadata_by_url[key] = dict(item)

        content_by_url: dict[str, str] = {}
        for item in extracted:
            url = str(item.get("url") or item.get("href") or "").strip()
            key = normalize_url(url)
            content = str(item.get("content") or "")
            if key and content and key not in content_by_url:
                content_by_url[key] = content[:MAX_CONTENT_CHARS]

        profiles_by_id = {
            str(item.get("source_id")): item
            for item in source_profiles if item.get("source_id")
        }
        ordered: list[str] = []
        for item in [*source_metadata, *counter_sources, *extracted]:
            url = str(item.get("url") or item.get("href") or "").strip()
            key = normalize_url(url)
            if key and key not in ordered:
                ordered.append(key)

        records: list[dict[str, Any]] = []
        for url in ordered[:MAX_SOURCES_PER_RUN]:
            source_id = _hash(url)[:16]
            meta = metadata_by_url.get(url, {})
            profile = profiles_by_id.get(source_id, {})
            content = content_by_url.get(url, "")
            host = str(meta.get("host") or profile.get("host") or urlsplit(url).hostname or "")
            records.append({
                "source_id": source_id,
                "url": url,
                "title": str(meta.get("title") or profile.get("title") or "")[:1000],
                "host": host[:255],
                "publisher_id": str(profile.get("publisher_id") or "")[:255],
                "source_type": str(profile.get("source_type") or "")[:100],
                "content_hash": _hash(content),
                "content": content,
            })
        return records

    def save_project_run(
        self,
        project_id: str,
        payload: dict[str, Any],
        extracted: Iterable[dict[str, Any]],
        *,
        counter_extracted: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        project_id = validate_project_id(project_id)
        created_at = _now()
        run_id = uuid.uuid4().hex
        query = str(payload.get("query") or "")[:4000]
        query_hash = _hash(normalize_text(query))
        source_profiles = payload.get("source_profiles") or []
        counter_meta = payload.get("counter_evidence") or {}
        counter_sources = counter_meta.get("sources") or []
        sources = self._collect_sources(
            payload.get("sources") or [],
            [*list(extracted), *list(counter_extracted)],
            source_profiles,
            counter_sources,
        )
        source_roles: dict[str, str] = {}
        for source in payload.get("sources") or []:
            url = normalize_url(str(source.get("url") or source.get("href") or ""))
            if url:
                source_roles[_hash(url)[:16]] = "cited"
        for source in counter_sources:
            url = normalize_url(str(source.get("url") or source.get("href") or ""))
            if url and source_roles.get(_hash(url)[:16]) != "cited":
                source_roles[_hash(url)[:16]] = "counter_evidence"

        claims = payload.get("claim_audit") or []
        summary = {
            "mode": payload.get("mode"),
            "llm_provider": payload.get("llm_provider"),
            "model": payload.get("model"),
            "confidence": payload.get("confidence"),
            "confidence_reason": payload.get("confidence_reason"),
            "coverage": payload.get("coverage"),
            "research_coverage": payload.get("research_coverage"),
            "source_intelligence": payload.get("source_intelligence"),
            "claim_audit_summary": payload.get("claim_audit_summary"),
            "n_results": payload.get("n_results"),
            "n_extracted": payload.get("n_extracted"),
            "n_sources": payload.get("n_sources"),
        }

        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT run_id, query_hash, query FROM research_runs "
                "WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            previous_run_id = previous["run_id"] if previous else None
            same_query = bool(previous and previous["query_hash"] == query_hash)

            conn.execute(
                "INSERT INTO projects(project_id,created_at,updated_at,last_run_at) "
                "VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                "updated_at=excluded.updated_at,last_run_at=excluded.last_run_at",
                (project_id, created_at, created_at, created_at),
            )
            conn.execute(
                "INSERT INTO research_runs(run_id,project_id,created_at,query,query_hash,model,synthesis,summary_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id, project_id, created_at, query, query_hash,
                    str(payload.get("model") or "")[:200] or None,
                    str(payload.get("synthesis") or "")[:200_000],
                    json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )

            for source in sources:
                conn.execute(
                    "INSERT INTO research_sources(project_id,source_id,content_hash,url,title,host,publisher_id,source_type,first_seen_at,last_seen_at,content) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(project_id,source_id,content_hash) DO UPDATE SET "
                    "last_seen_at=excluded.last_seen_at",
                    (
                        project_id, source["source_id"], source["content_hash"],
                        source["url"], source["title"], source["host"],
                        source["publisher_id"], source["source_type"],
                        created_at, created_at, source["content"],
                    ),
                )
                conn.execute(
                    "INSERT INTO run_sources(run_id,project_id,source_id,content_hash,source_role) "
                    "VALUES(?,?,?,?,?)",
                    (
                        run_id, project_id, source["source_id"], source["content_hash"],
                        source_roles.get(source["source_id"], "discovered"),
                    ),
                )

            claim_rows: list[dict[str, Any]] = []
            for claim in claims:
                text = str(claim.get("claim") or "")
                if not text:
                    continue
                claim_key = _hash(normalize_text(text))
                status = str(claim.get("status") or "unknown")
                claim_rows.append({
                    "claim_key": claim_key, "claim_id": str(claim.get("claim_id") or ""),
                    "claim_text": text[:4000], "status": status,
                })
                conn.execute(
                    "INSERT INTO run_claims(run_id,claim_key,claim_id,claim_text,status) VALUES(?,?,?,?,?)",
                    (run_id, claim_key, str(claim.get("claim_id") or "")[:64], text[:4000], status),
                )
                for evidence in claim.get("evidence") or []:
                    source_id = str(evidence.get("source_id") or "")
                    if not source_id:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO claim_evidence(run_id,claim_key,source_id,source_role,stance,quote) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            run_id, claim_key, source_id,
                            str(evidence.get("source_role") or "cited")[:32],
                            str(evidence.get("stance") or "unknown")[:32],
                            str(evidence.get("quote") or "")[:1000],
                        ),
                    )

            current_source_hashes = {item["source_id"]: item["content_hash"] for item in sources}
            diff: dict[str, Any] = {
                "compared_to_run_id": previous_run_id,
                "same_query": same_query,
                "new_sources": [],
                "changed_sources": [],
                "new_claims": [],
                "claim_status_changes": [],
            }
            if not previous_run_id:
                diff["new_sources"] = [
                    {"source_id": source["source_id"], "url": source["url"]}
                    for source in sources
                ]
            if previous_run_id:
                old_source_rows = conn.execute(
                    "SELECT source_id,content_hash FROM run_sources WHERE run_id=?",
                    (previous_run_id,),
                ).fetchall()
                old_hashes = {row["source_id"]: row["content_hash"] for row in old_source_rows}
                for source in sources:
                    old_hash = old_hashes.get(source["source_id"])
                    if old_hash is None:
                        diff["new_sources"].append({"source_id": source["source_id"], "url": source["url"]})
                    elif old_hash != source["content_hash"]:
                        diff["changed_sources"].append({
                            "source_id": source["source_id"], "url": source["url"],
                            "old_content_hash": old_hash, "new_content_hash": source["content_hash"],
                        })
                if same_query:
                    old_claims = {
                        row["claim_key"]: dict(row)
                        for row in conn.execute(
                            "SELECT claim_key,claim_id,claim_text,status FROM run_claims WHERE run_id=?",
                            (previous_run_id,),
                        ).fetchall()
                    }
                    for claim in claim_rows:
                        previous_claim = old_claims.get(claim["claim_key"])
                        if previous_claim is None:
                            diff["new_claims"].append({
                                "claim_id": claim["claim_id"], "claim": claim["claim_text"],
                                "status": claim["status"],
                            })
                        elif previous_claim["status"] != claim["status"]:
                            diff["claim_status_changes"].append({
                                "claim": claim["claim_text"],
                                "old_status": previous_claim["status"],
                                "new_status": claim["status"],
                            })
            diff["new_source_count"] = len(diff["new_sources"])
            diff["changed_source_count"] = len(diff["changed_sources"])
            diff["new_claim_count"] = len(diff["new_claims"])
            diff["claim_status_change_count"] = len(diff["claim_status_changes"])
            diff["comparison_note"] = (
                "Claim changes compared because the normalized query matches."
                if same_query else
                "Claim-level comparison skipped because this is the first run or the query changed."
            )
            conn.execute(
                "UPDATE research_runs SET summary_json=? WHERE run_id=?",
                (
                    json.dumps({**summary, "workspace_diff": diff}, ensure_ascii=False, sort_keys=True, default=str),
                    run_id,
                ),
            )

        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return {
            "status": "saved",
            "project_id": project_id,
            "run_id": run_id,
            "database": str(self.path),
            "source_snapshot_count": len(sources),
            "claim_count": len(claim_rows),
            "diff": diff,
        }

    def list_projects(self, project_id: str | None = None) -> dict[str, Any]:
        with self._session() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT p.project_id,p.created_at,p.updated_at,p.last_run_at,"
                    "(SELECT COUNT(*) FROM research_runs r WHERE r.project_id=p.project_id) AS run_count "
                    "FROM projects p WHERE p.project_id=?",
                    (validate_project_id(project_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT p.project_id,p.created_at,p.updated_at,p.last_run_at,"
                    "(SELECT COUNT(*) FROM research_runs r WHERE r.project_id=p.project_id) AS run_count "
                    "FROM projects p ORDER BY p.last_run_at DESC,p.project_id"
                ).fetchall()
        return {"database": str(self.path), "projects": [dict(row) for row in rows]}

    def list_runs(self, project_id: str, limit: int = 20) -> dict[str, Any]:
        project_id = validate_project_id(project_id)
        limit = max(1, min(100, int(limit)))
        with self._session() as conn:
            rows = conn.execute(
                "SELECT run_id,created_at,query,model,summary_json "
                "FROM research_runs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        runs = []
        for row in rows:
            item = dict(row)
            try:
                item["summary"] = json.loads(item.pop("summary_json"))
            except (TypeError, json.JSONDecodeError):
                item["summary"] = {}
            runs.append(item)
        return {"project_id": project_id, "runs": runs}

    def delete_project(self, project_id: str) -> bool:
        project_id = validate_project_id(project_id)
        with self._session() as conn:
            cur = conn.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
            return cur.rowcount > 0
