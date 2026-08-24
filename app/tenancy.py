"""Tenant context and the guard that makes cross-tenant access a programming error.

The design mirrors Postgres row-level security (see
`migrations/postgres/002_row_level_security.sql`): application code never gets
to choose whether a query is scoped. It states which tenant it is acting for,
and the storage layer refuses to execute any statement against a tenant-scoped
table unless that statement filters on `tenant_id`.

Two independent layers protect the data:

1.  `TenantGuardError` -- a static check on the SQL text, so a developer who
    forgets the predicate gets a loud failure instead of a silent leak.
2.  The bound `tenant_id` parameter itself, which does the actual filtering.

On Postgres a third layer applies: RLS policies enforced by the database, so
even a raw psql session with the application role cannot read another tenant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Tables whose rows belong to exactly one tenant. Any statement touching one of
# these must be tenant-qualified.
TENANT_SCOPED_TABLES = frozenset(
    {
        "clients",
        "onboarding_sessions",
        "campaign_briefs",
        "generated_posts",
        "publish_attempts",
        "api_keys",
    }
)

# Tables that are intentionally global (the tenant registry itself).
GLOBAL_TABLES = frozenset({"tenants", "schema_migrations"})

_TABLE_REF = re.compile(
    r"\b(?:from|join|into|update|table)\s+[\"'`]?([a-z_][a-z0-9_]*)[\"'`]?",
    re.IGNORECASE,
)
_TENANT_PREDICATE = re.compile(r"\btenant_id\s*=\s*[?:]", re.IGNORECASE)
_TENANT_COLUMN_INSERT = re.compile(r"\btenant_id\b", re.IGNORECASE)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_sql_comments(sql: str) -> str:
    """Remove `-- ...` and `/* ... */` comments before any pattern check runs.

    Without this, a leftover comment like `-- TODO: add tenant_id = ? filter`
    satisfies _TENANT_PREDICATE by pure text match, and assert_tenant_scoped
    waves the query through as if it were actually scoped. Since this guard's
    whole purpose is to catch exactly that kind of developer mistake, having
    it be fooled by a comment mentioning the missing filter defeats the point.

    This does not handle a `tenant_id = ?`-shaped string *literal* inside the
    SQL (as opposed to a comment) -- that would need a real SQL tokenizer to
    do correctly, and is a much less likely way to accidentally satisfy the
    pattern than a comment is.
    """
    return _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql))


class TenantGuardError(RuntimeError):
    """Raised when a statement would touch tenant data without scoping it."""


class TenantIsolationError(PermissionError):
    """Raised when a caller references a row belonging to a different tenant."""


@dataclass(frozen=True)
class TenantContext:
    """Identifies the tenant on whose behalf the current request runs."""

    tenant_id: str
    tenant_slug: str
    actor_email: str
    role: str = "member"

    def assert_owns(self, row_tenant_id: str | None, resource: str = "resource") -> None:
        if row_tenant_id is None or row_tenant_id != self.tenant_id:
            raise TenantIsolationError(f"{resource} does not belong to tenant {self.tenant_slug}")


def referenced_tables(sql: str) -> set[str]:
    return {m.lower() for m in _TABLE_REF.findall(sql)}


def assert_tenant_scoped(sql: str) -> None:
    """Reject SQL that touches tenant-scoped tables without a tenant predicate.

    INSERT statements are scoped by supplying a `tenant_id` column rather than a
    WHERE clause, so they are checked for the column instead.
    """
    code = _strip_sql_comments(sql)
    tables = referenced_tables(code) & TENANT_SCOPED_TABLES
    if not tables:
        return

    stripped = code.lstrip()
    is_insert = stripped[:6].lower() == "insert"

    if is_insert:
        if not _TENANT_COLUMN_INSERT.search(code):
            raise TenantGuardError(
                f"INSERT into tenant-scoped table(s) {sorted(tables)} must set tenant_id"
            )
        return

    if not _TENANT_PREDICATE.search(code):
        raise TenantGuardError(
            f"query touching tenant-scoped table(s) {sorted(tables)} "
            "must filter on tenant_id = ?"
        )
