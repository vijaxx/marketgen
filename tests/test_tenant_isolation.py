"""Proves that tenant A cannot read, list, or modify tenant B's data through
any API surface, and that the storage-layer guard rejects unscoped SQL
outright. This is the most important test file in the repository."""

from __future__ import annotations

import pytest

from app.tenancy import TenantContext, TenantGuardError, TenantIsolationError, assert_tenant_scoped
from tests.conftest import FULL_STEP_ANSWERS, complete_all_steps, make_client_and_session, make_tenant


def test_tenant_a_cannot_list_tenant_b_clients(client):
    tenant_a = make_tenant(client, name="Tenant A", email="a@a.test")
    tenant_b = make_tenant(client, name="Tenant B", email="b@b.test")

    client.post("/api/clients", json={"name": "A-Client"}, headers={"X-API-Key": tenant_a["api_key"]})
    client.post("/api/clients", json={"name": "B-Client"}, headers={"X-API-Key": tenant_b["api_key"]})

    a_clients = client.get("/api/clients", headers={"X-API-Key": tenant_a["api_key"]}).json()
    b_clients = client.get("/api/clients", headers={"X-API-Key": tenant_b["api_key"]}).json()

    assert {c["name"] for c in a_clients} == {"A-Client"}
    assert {c["name"] for c in b_clients} == {"B-Client"}


def test_tenant_a_cannot_fetch_tenant_b_session_by_id(client):
    tenant_a = make_tenant(client, name="Tenant A", email="a@a.test")
    tenant_b = make_tenant(client, name="Tenant B", email="b@b.test")

    _, session_b = make_client_and_session(client, tenant_b["api_key"], client_name="B-Client")

    # Tenant A tries to fetch tenant B's session id directly.
    resp = client.get(
        f"/api/onboarding/sessions/{session_b['id']}", headers={"X-API-Key": tenant_a["api_key"]}
    )
    assert resp.status_code == 404  # not 403 -- the row is invisible, not "forbidden"


def test_tenant_a_cannot_submit_steps_into_tenant_b_session(client):
    tenant_a = make_tenant(client, name="Tenant A", email="a@a.test")
    tenant_b = make_tenant(client, name="Tenant B", email="b@b.test")
    _, session_b = make_client_and_session(client, tenant_b["api_key"], client_name="B-Client")

    resp = client.post(
        f"/api/onboarding/sessions/{session_b['id']}/steps/1",
        json={"answers": FULL_STEP_ANSWERS[1], "advance": True},
        headers={"X-API-Key": tenant_a["api_key"]},
    )
    assert resp.status_code == 404


def test_tenant_a_cannot_read_tenant_b_generated_posts(client):
    tenant_a = make_tenant(client, name="Tenant A", email="a@a.test")
    tenant_b = make_tenant(client, name="Tenant B", email="b@b.test")

    _, session_b = make_client_and_session(client, tenant_b["api_key"], client_name="B-Client")
    complete_all_steps(client, tenant_b["api_key"], session_b["id"])
    gen = client.post(
        f"/api/onboarding/sessions/{session_b['id']}/generate", headers={"X-API-Key": tenant_b["api_key"]}
    ).json()
    brief_id = gen["brief"]["id"]

    resp = client.get(f"/api/briefs/{brief_id}", headers={"X-API-Key": tenant_a["api_key"]})
    assert resp.status_code == 404


def test_client_uniqueness_is_scoped_per_tenant_not_global(client):
    """Two different tenants may each have a client named identically -- the
    uniqueness constraint in the schema is (tenant_id, name), not (name)."""
    tenant_a = make_tenant(client, name="Tenant A", email="a@a.test")
    tenant_b = make_tenant(client, name="Tenant B", email="b@b.test")

    r1 = client.post("/api/clients", json={"name": "Shared Name"}, headers={"X-API-Key": tenant_a["api_key"]})
    r2 = client.post("/api/clients", json={"name": "Shared Name"}, headers={"X-API-Key": tenant_b["api_key"]})
    assert r1.status_code == 201
    assert r2.status_code == 201


def test_store_layer_rejects_unscoped_select(store):
    with pytest.raises(TenantGuardError):
        store.query_scoped("some-tenant-id", "SELECT * FROM clients")


def test_store_layer_rejects_select_missing_tenant_param(store):
    with pytest.raises(AssertionError):
        store.query_scoped("some-tenant-id", "SELECT * FROM clients WHERE tenant_id = ?", ("other-id",))


def test_assert_tenant_scoped_accepts_properly_scoped_query():
    # Should not raise.
    assert_tenant_scoped("SELECT * FROM clients WHERE tenant_id = ? AND id = ?")


def test_assert_tenant_scoped_rejects_unscoped_query():
    with pytest.raises(TenantGuardError):
        assert_tenant_scoped("SELECT * FROM clients WHERE id = ?")


def test_assert_tenant_scoped_ignores_global_tables():
    # tenants table itself is looked up by slug/id without a tenant predicate.
    assert_tenant_scoped("SELECT * FROM tenants WHERE slug = ?")


def test_assert_tenant_scoped_is_not_fooled_by_a_comment_mentioning_tenant_id():
    """A leftover TODO comment must not satisfy the guard.

    The whole point of this check is to catch a developer who forgot the
    tenant_id predicate. Before this was fixed, a comment like the one below
    contained the literal text "tenant_id = ?" and the regex-based check
    matched it directly against the raw SQL, waving through a query with no
    actual WHERE-clause filter at all.
    """
    sql = """
    -- TODO: add tenant_id = ? filter here eventually
    SELECT * FROM clients
    """
    with pytest.raises(TenantGuardError):
        assert_tenant_scoped(sql)


def test_assert_tenant_scoped_is_not_fooled_by_a_block_comment():
    sql = "/* tenant_id = ? handled upstream, trust me */ SELECT * FROM clients"
    with pytest.raises(TenantGuardError):
        assert_tenant_scoped(sql)


def test_assert_tenant_scoped_still_accepts_a_real_predicate_next_to_a_comment():
    sql = """
    -- fetch one client
    SELECT * FROM clients WHERE tenant_id = ? AND id = ?
    """
    assert_tenant_scoped(sql)  # should not raise


def test_tenant_context_assert_owns_raises_on_mismatch():
    ctx = TenantContext(tenant_id="t1", tenant_slug="t1-slug", actor_email="x@x.com")
    with pytest.raises(TenantIsolationError):
        ctx.assert_owns("t2")


def test_tenant_context_assert_owns_passes_on_match():
    ctx = TenantContext(tenant_id="t1", tenant_slug="t1-slug", actor_email="x@x.com")
    ctx.assert_owns("t1")  # must not raise


def test_publish_attempt_isolated_per_tenant(client):
    tenant_a = make_tenant(client, name="Tenant A", email="a@a.test")
    tenant_b = make_tenant(client, name="Tenant B", email="b@b.test")

    _, session_b = make_client_and_session(client, tenant_b["api_key"], client_name="B-Client")
    complete_all_steps(client, tenant_b["api_key"], session_b["id"])
    gen = client.post(
        f"/api/onboarding/sessions/{session_b['id']}/generate", headers={"X-API-Key": tenant_b["api_key"]}
    ).json()
    post_id = gen["posts"][0]["id"]

    # Tenant A cannot publish a post it doesn't own.
    resp = client.post(
        "/api/publish", json={"post_id": post_id, "adapter": "dry_run"}, headers={"X-API-Key": tenant_a["api_key"]}
    )
    assert resp.status_code == 404
