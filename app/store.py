"""SQLite-backed storage layer.

Every method that touches a tenant-scoped table takes a `TenantContext` (or a
bare `tenant_id`) and threads it through `app.tenancy.assert_tenant_scoped`,
which statically verifies the SQL text is filtered on `tenant_id`. This is the
local-dev equivalent of the Postgres row-level security policies in
`migrations/postgres/002_row_level_security.sql` -- same guarantee, enforced in
Python instead of by the database, because SQLite has no RLS.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings
from app.tenancy import TenantContext, TenantIsolationError, assert_tenant_scoped

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "migrations" / "sqlite" / "001_schema.sql"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class Store:
    """Thin, explicit wrapper around a single SQLite connection.

    A single connection with `check_same_thread=False` plus a lock is used
    deliberately: this is a portfolio-scale demo, not a production pool. The
    isolation guarantees documented above do not depend on the connection
    strategy.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text()
        self._conn.executescript(sql)
        self._conn.commit()

    # -- low level -----------------------------------------------------

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute non-tenant-scoped SQL (global tables, schema, tenant creation)."""
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def execute_scoped(self, tenant_id: str, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL that must be tenant-scoped. Statically checked, then run."""
        assert_tenant_scoped(sql)
        if tenant_id not in params:
            raise AssertionError("tenant_id must be bound as a query parameter")
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def query_scoped(self, tenant_id: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        assert_tenant_scoped(sql)
        if tenant_id not in params:
            raise AssertionError("tenant_id must be bound as a query parameter")
        return list(self._conn.execute(sql, params).fetchall())

    def reset_for_tests(self) -> None:
        for table in (
            "publish_attempts",
            "generated_posts",
            "campaign_briefs",
            "onboarding_sessions",
            "clients",
            "api_keys",
            "tenants",
        ):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()

    # -- tenants ---------------------------------------------------------

    def create_tenant(self, slug: str, name: str) -> dict[str, Any]:
        tenant_id = new_id()
        self.execute(
            "INSERT INTO tenants (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
            (tenant_id, slug, name, now_iso()),
        )
        return {"id": tenant_id, "slug": slug, "name": name}

    def get_tenant_by_slug(self, slug: str) -> dict[str, Any] | None:
        row = self.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None

    def get_tenant_by_id(self, tenant_id: str) -> dict[str, Any] | None:
        row = self.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        return dict(row) if row else None

    def create_api_key(self, tenant_id: str, raw_key: str, actor_email: str, role: str = "member") -> str:
        key_id = new_id()
        self.execute_scoped(
            tenant_id,
            "INSERT INTO api_keys (id, tenant_id, key_hash, actor_email, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key_id, tenant_id, hash_key(raw_key), actor_email, role, now_iso()),
        )
        return key_id

    def resolve_api_key(self, raw_key: str) -> dict[str, Any] | None:
        """Global lookup: the caller does not yet know their tenant, so this is
        intentionally the one place that queries api_keys without a tenant_id
        predicate. It returns at most one row keyed by the unique hash, and the
        tenant_id it yields becomes the TenantContext for everything after."""
        row = self.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
            (hash_key(raw_key),),
        ).fetchone()
        return dict(row) if row else None

    # -- clients ---------------------------------------------------------

    def create_client(self, ctx: TenantContext, name: str) -> dict[str, Any]:
        client_id = new_id()
        self.execute_scoped(
            ctx.tenant_id,
            "INSERT INTO clients (id, tenant_id, name, created_at) VALUES (?, ?, ?, ?)",
            (client_id, ctx.tenant_id, name, now_iso()),
        )
        return {"id": client_id, "tenant_id": ctx.tenant_id, "name": name}

    def get_client(self, ctx: TenantContext, client_id: str) -> dict[str, Any] | None:
        rows = self.query_scoped(
            ctx.tenant_id,
            "SELECT * FROM clients WHERE tenant_id = ? AND id = ?",
            (ctx.tenant_id, client_id),
        )
        return dict(rows[0]) if rows else None

    def list_clients(self, ctx: TenantContext) -> list[dict[str, Any]]:
        rows = self.query_scoped(
            ctx.tenant_id, "SELECT * FROM clients WHERE tenant_id = ? ORDER BY created_at", (ctx.tenant_id,)
        )
        return [dict(r) for r in rows]

    # -- onboarding sessions ----------------------------------------------

    def create_session(self, ctx: TenantContext, client_id: str, questionnaire_version: str) -> dict[str, Any]:
        session_id = new_id()
        ts = now_iso()
        self.execute_scoped(
            ctx.tenant_id,
            "INSERT INTO onboarding_sessions "
            "(id, tenant_id, client_id, status, current_step, answers, completed_steps, "
            "questionnaire_version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'in_progress', 1, '{}', '[]', ?, ?, ?)",
            (session_id, ctx.tenant_id, client_id, questionnaire_version, ts, ts),
        )
        return self.get_session(ctx, session_id)  # type: ignore[return-value]

    def get_session(self, ctx: TenantContext, session_id: str) -> dict[str, Any] | None:
        rows = self.query_scoped(
            ctx.tenant_id,
            "SELECT * FROM onboarding_sessions WHERE tenant_id = ? AND id = ?",
            (ctx.tenant_id, session_id),
        )
        if not rows:
            return None
        return _deserialize_session(dict(rows[0]))

    def list_sessions(self, ctx: TenantContext) -> list[dict[str, Any]]:
        rows = self.query_scoped(
            ctx.tenant_id,
            "SELECT * FROM onboarding_sessions WHERE tenant_id = ? ORDER BY created_at DESC",
            (ctx.tenant_id,),
        )
        return [_deserialize_session(dict(r)) for r in rows]

    def save_session_progress(
        self,
        ctx: TenantContext,
        session_id: str,
        answers: dict[str, Any],
        completed_steps: list[int],
        current_step: int,
        status: str,
    ) -> dict[str, Any]:
        self.execute_scoped(
            ctx.tenant_id,
            "UPDATE onboarding_sessions SET answers = ?, completed_steps = ?, current_step = ?, "
            "status = ?, updated_at = ? WHERE tenant_id = ? AND id = ?",
            (
                json.dumps(answers),
                json.dumps(completed_steps),
                current_step,
                status,
                now_iso(),
                ctx.tenant_id,
                session_id,
            ),
        )
        session = self.get_session(ctx, session_id)
        if session is None:
            raise TenantIsolationError("session vanished during update")
        return session

    # -- campaign briefs / posts -------------------------------------------

    def create_brief(self, ctx: TenantContext, session_id: str, provider: str, brief: dict[str, Any]) -> dict[str, Any]:
        brief_id = new_id()
        self.execute_scoped(
            ctx.tenant_id,
            "INSERT INTO campaign_briefs (id, tenant_id, session_id, provider, brief, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (brief_id, ctx.tenant_id, session_id, provider, json.dumps(brief), now_iso()),
        )
        return {"id": brief_id, "tenant_id": ctx.tenant_id, "session_id": session_id, "provider": provider, "brief": brief}

    def get_brief(self, ctx: TenantContext, brief_id: str) -> dict[str, Any] | None:
        rows = self.query_scoped(
            ctx.tenant_id, "SELECT * FROM campaign_briefs WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, brief_id)
        )
        if not rows:
            return None
        row = dict(rows[0])
        row["brief"] = json.loads(row["brief"])
        return row

    def add_post(self, ctx: TenantContext, brief_id: str, post: dict[str, Any]) -> dict[str, Any]:
        post_id = new_id()
        self.execute_scoped(
            ctx.tenant_id,
            "INSERT INTO generated_posts "
            "(id, tenant_id, brief_id, channel, format, headline, body, call_to_action, "
            "hashtags, image_prompt, image_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                post_id,
                ctx.tenant_id,
                brief_id,
                post["channel"],
                post["format"],
                post["headline"],
                post["body"],
                post["call_to_action"],
                json.dumps(post.get("hashtags", [])),
                post.get("image_prompt"),
                post.get("image_ref"),
                now_iso(),
            ),
        )
        post_row = dict(post)
        post_row["id"] = post_id
        post_row["tenant_id"] = ctx.tenant_id
        post_row["brief_id"] = brief_id
        return post_row

    def list_posts_for_brief(self, ctx: TenantContext, brief_id: str) -> list[dict[str, Any]]:
        rows = self.query_scoped(
            ctx.tenant_id,
            "SELECT * FROM generated_posts WHERE tenant_id = ? AND brief_id = ? ORDER BY created_at",
            (ctx.tenant_id, brief_id),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["hashtags"] = json.loads(d["hashtags"])
            out.append(d)
        return out

    # -- publish attempts ---------------------------------------------------

    def record_publish_attempt(
        self, ctx: TenantContext, post_id: str, adapter: str, dry_run: bool, status: str, detail: dict[str, Any]
    ) -> dict[str, Any]:
        attempt_id = new_id()
        self.execute_scoped(
            ctx.tenant_id,
            "INSERT INTO publish_attempts (id, tenant_id, post_id, adapter, dry_run, status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, ctx.tenant_id, post_id, adapter, int(dry_run), status, json.dumps(detail), now_iso()),
        )
        return {
            "id": attempt_id,
            "tenant_id": ctx.tenant_id,
            "post_id": post_id,
            "adapter": adapter,
            "dry_run": dry_run,
            "status": status,
            "detail": detail,
        }

    def list_publish_attempts(self, ctx: TenantContext, post_id: str) -> list[dict[str, Any]]:
        rows = self.query_scoped(
            ctx.tenant_id,
            "SELECT * FROM publish_attempts WHERE tenant_id = ? AND post_id = ? ORDER BY created_at",
            (ctx.tenant_id, post_id),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            d["dry_run"] = bool(d["dry_run"])
            out.append(d)
        return out

    def close(self) -> None:
        self._conn.close()


def _deserialize_session(row: dict[str, Any]) -> dict[str, Any]:
    row["answers"] = json.loads(row["answers"])
    row["completed_steps"] = json.loads(row["completed_steps"])
    return row


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.sqlite_path)
    return _store


@contextlib.contextmanager
def temporary_store(path: str) -> Iterator[Store]:
    """Used by tests to get an isolated database per test."""
    global _store
    previous = _store
    _store = Store(path)
    try:
        yield _store
    finally:
        _store.close()
        _store = previous
