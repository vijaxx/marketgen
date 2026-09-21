from __future__ import annotations

from tests.conftest import FULL_STEP_ANSWERS, complete_all_steps, make_client_and_session, make_tenant


def test_create_tenant_and_session(client):
    tenant = make_tenant(client)
    client_id, session = make_client_and_session(client, tenant["api_key"])
    assert session["status"] == "in_progress"
    assert session["current_step"] == 1
    assert session["answers"] == {}


def test_step_submission_persists_and_advances(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    headers = {"X-API-Key": tenant["api_key"]}

    resp = client.post(
        f"/api/onboarding/sessions/{session['id']}/steps/1",
        json={"answers": FULL_STEP_ANSWERS[1], "advance": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current_step"] == 2
    assert body["completed_steps"] == [1]
    assert body["answers"]["brand_display_name"] == "Acme"


def test_step_validation_error_returns_422(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    headers = {"X-API-Key": tenant["api_key"]}

    resp = client.post(
        f"/api/onboarding/sessions/{session['id']}/steps/1",
        json={"answers": {"brand_legal_name": "A"}, "advance": True},
        headers=headers,
    )
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert "brand_display_name" in errors


def test_resume_partial_submission(client):
    """A session started, partially filled, and abandoned mid-step must resume
    with previously-saved answers intact when fetched again."""
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    headers = {"X-API-Key": tenant["api_key"]}

    client.post(
        f"/api/onboarding/sessions/{session['id']}/steps/1",
        json={"answers": FULL_STEP_ANSWERS[1], "advance": True},
        headers=headers,
    )

    # Simulate "coming back later": fetch the session fresh.
    resumed = client.get(f"/api/onboarding/sessions/{session['id']}", headers=headers)
    assert resumed.status_code == 200
    body = resumed.json()
    assert body["current_step"] == 2
    assert body["answers"]["brand_display_name"] == "Acme"
    assert body["status"] == "in_progress"


def test_resume_does_not_require_redoing_earlier_steps(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    headers = {"X-API-Key": tenant["api_key"]}
    session_id = session["id"]

    for step in (1, 2, 3):
        r = client.post(
            f"/api/onboarding/sessions/{session_id}/steps/{step}",
            json={"answers": FULL_STEP_ANSWERS[step], "advance": True},
            headers=headers,
        )
        assert r.status_code == 200

    resumed = client.get(f"/api/onboarding/sessions/{session_id}", headers=headers).json()
    assert resumed["completed_steps"] == [1, 2, 3]
    assert resumed["current_step"] == 4
    # earlier answers still present
    assert resumed["answers"]["offer_flagship_name"] == "Acme Pro"


def test_progress_tracking_reports_percent_complete(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    headers = {"X-API-Key": tenant["api_key"]}
    session_id = session["id"]

    before = client.get(f"/api/onboarding/sessions/{session_id}/progress", headers=headers).json()
    assert before["overall_percent"] == 0.0

    client.post(
        f"/api/onboarding/sessions/{session_id}/steps/1",
        json={"answers": FULL_STEP_ANSWERS[1], "advance": True},
        headers=headers,
    )
    after = client.get(f"/api/onboarding/sessions/{session_id}/progress", headers=headers).json()
    assert after["overall_percent"] > before["overall_percent"]


def test_full_seven_step_flow_completes_session(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    final = complete_all_steps(client, tenant["api_key"], session["id"])
    assert final["status"] == "completed"
    assert final["completed_steps"] == [1, 2, 3, 4, 5, 6, 7]
    assert final["progress"]["overall_percent"] == 100.0


def test_generation_requires_completed_onboarding(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    headers = {"X-API-Key": tenant["api_key"]}
    resp = client.post(f"/api/onboarding/sessions/{session['id']}/generate", headers=headers)
    assert resp.status_code == 409


def test_duplicate_client_name_returns_409_not_500(client):
    """clients has a (tenant_id, name) UNIQUE constraint; creating a second
    client with the same name for the same tenant must be a clean 409, not
    an unhandled sqlite3.IntegrityError bubbling up as a 500."""
    tenant = make_tenant(client)
    headers = {"X-API-Key": tenant["api_key"]}

    first = client.post("/api/clients", json={"name": "Acme Retail"}, headers=headers)
    assert first.status_code == 201, first.text

    dupe = client.post("/api/clients", json={"name": "Acme Retail"}, headers=headers)
    assert dupe.status_code == 409, dupe.text


def test_missing_api_key_rejected(client):
    resp = client.get("/api/clients")
    assert resp.status_code == 401


def test_invalid_api_key_rejected(client):
    resp = client.get("/api/clients", headers={"X-API-Key": "mg_not_a_real_key"})
    assert resp.status_code == 401
