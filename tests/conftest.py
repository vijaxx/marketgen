from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SEED_DEMO_TENANTS", "false")


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture()
def store(db_path: str):
    from app.store import Store

    s = Store(db_path)
    yield s
    s.close()


@pytest.fixture()
def client(db_path: str, monkeypatch):
    """A TestClient wired to an isolated, temporary SQLite database."""
    import app.store as store_module

    monkeypatch.setattr(store_module, "_store", None)
    test_store = store_module.Store(db_path)
    store_module._store = test_store

    from app.main import app

    with TestClient(app) as c:
        yield c

    test_store.close()
    store_module._store = None


def make_tenant(client, name="Acme Co", email="owner@acme.test", slug=None):
    import uuid

    slug = slug or ("acme-" + uuid.uuid4().hex[:8])
    resp = client.post("/api/tenants", json={"slug": slug, "name": name, "actor_email": email})
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_client_and_session(client, api_key, client_name="Test Client"):
    headers = {"X-API-Key": api_key}
    c = client.post("/api/clients", json={"name": client_name}, headers=headers)
    assert c.status_code == 201, c.text
    client_id = c.json()["id"]
    s = client.post("/api/onboarding/sessions", json={"client_id": client_id}, headers=headers)
    assert s.status_code == 201, s.text
    return client_id, s.json()


# A minimal, fully-valid answer set per step, used by end-to-end flow tests.
# Chosen so that every branching condition it triggers has its dependent
# question answered too (e.g. brand_industry != "other").
FULL_STEP_ANSWERS = {
    1: {
        "brand_legal_name": "Acme Marketing Pvt Ltd",
        "brand_display_name": "Acme",
        "brand_website": "https://acme.example.com",
        "brand_industry": "saas",
        "brand_one_liner": "Acme helps teams launch marketing campaigns faster.",
        "brand_mission": "We help teams ship marketing campaigns quickly and reliably for everyone.",
        "brand_values": ["trust", "innovation"],
        "brand_has_style_guide": False,
    },
    2: {
        "offer_type": "saas",
        "offer_catalog_size": 3,
        "offer_flagship_name": "Acme Pro",
        "offer_flagship_description": "Acme Pro is our flagship subscription plan for growing marketing teams.",
        "offer_price_band": "mid",
        "offer_avg_price_inr": 2999,
        "offer_usp": "Only platform combining AI content and multi-channel scheduling in one dashboard.",
        "offer_proof_points": ["testimonials"],
        "offer_proof_details": "50+ five star reviews on G2 from marketing teams across India.",
        "offer_has_seasonality": False,
        "offer_launch_planned": False,
    },
    3: {
        "audience_primary_segment": "Marketing managers at Series A startups",
        "audience_age_min": 25,
        "audience_age_max": 45,
        "audience_genders": ["all"],
        "audience_geographies": ["india_metro"],
        "audience_languages": ["en"],
        "audience_income_band": "15l_35l",
        "audience_pain_points": "Not enough time to produce consistent content across channels every week.",
        "audience_buying_triggers": ["content_discovery"],
        "audience_objections": "Worried AI content sounds generic and off-brand.",
        "audience_has_secondary_segment": False,
    },
    4: {
        "tone_primary": "friendly",
        "tone_formality": 3,
        "tone_humour": 2,
        "tone_emoji_usage": "sparing",
        "tone_emoji_allowlist": "rocket, sparkles",
        "tone_person": "second",
        "tone_reading_level": "standard",
        "tone_cta_style": "direct",
    },
    5: {
        "channels_active": ["instagram", "linkedin"],
        "channel_instagram_handle": "@acme",
        "channel_instagram_posts_per_week": 4,
        "channel_instagram_formats": ["reel", "static"],
        "channel_linkedin_page_url": "https://linkedin.com/company/acme",
        "channel_linkedin_posts_per_week": 3,
        "channel_linkedin_content_pillars": ["thought_leadership"],
        "content_approval_required": False,
        "content_publishing_timezone": "Asia/Kolkata",
    },
    6: {
        "goal_primary": "lead_gen",
        "goal_horizon_days": 90,
        "goal_target_metric": "signups",
        "goal_target_value": 500,
        "goal_baseline_value": 100,
        "goal_budget_band_inr": "50k_2l",
        "goal_paid_media": False,
        "goal_attribution_tool": "ga4",
        "goal_reporting_cadence": "monthly",
    },
    7: {
        "comp_primary_name": "Rival Co",
        "comp_primary_url": "https://rival.example.com",
        "comp_differentiator": "We ship campaigns in hours, not weeks, thanks to automated onboarding.",
        "constraint_regulated_industry": False,
        "constraint_image_rights": "ai_generated_ok",
        "constraint_ai_disclosure": True,
    },
}


def complete_all_steps(client, api_key, session_id):
    headers = {"X-API-Key": api_key}
    last = None
    for step in range(1, 8):
        resp = client.post(
            f"/api/onboarding/sessions/{session_id}/steps/{step}",
            json={"answers": FULL_STEP_ANSWERS[step], "advance": True},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        last = resp.json()
    return last
