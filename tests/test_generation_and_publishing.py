from __future__ import annotations

import pytest

from app.generation.provider import GeneratedPost, StubProvider, get_provider
from app.publishing.publisher import (
    DryRunPublisher,
    InstagramGraphPublisher,
    LinkedInPublisher,
    LivePublishingDisabled,
    get_publisher,
)
from tests.conftest import FULL_STEP_ANSWERS, complete_all_steps, make_client_and_session, make_tenant

MERGED_ANSWERS = {k: v for step in FULL_STEP_ANSWERS.values() for k, v in step.items()}


def test_default_provider_is_stub_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = get_provider()
    assert provider.name == "stub"


def test_stub_provider_is_deterministic():
    p1 = StubProvider()
    p2 = StubProvider()
    brief1 = p1.generate_brief("tenant-x", MERGED_ANSWERS)
    brief2 = p2.generate_brief("tenant-x", MERGED_ANSWERS)
    assert brief1.headline == brief2.headline
    assert brief1.summary == brief2.summary

    post1 = p1.generate_post(brief1, "instagram", MERGED_ANSWERS)
    post2 = p2.generate_post(brief2, "instagram", MERGED_ANSWERS)
    assert post1.body == post2.body
    assert post1.image_ref == post2.image_ref


def test_stub_provider_differs_by_channel():
    p = StubProvider()
    brief = p.generate_brief("tenant-x", MERGED_ANSWERS)
    ig = p.generate_post(brief, "instagram", MERGED_ANSWERS)
    li = p.generate_post(brief, "linkedin", MERGED_ANSWERS)
    assert ig.image_ref != li.image_ref


def test_stub_provider_respects_banned_words():
    answers = dict(MERGED_ANSWERS)
    p = StubProvider()
    brief = p.generate_brief("tenant-x", answers)
    # Force a banned word that is guaranteed to be in the brief summary.
    answers["tone_banned_words"] = "campaign"
    post = p.generate_post(brief, "instagram", answers)
    assert "campaign" not in post.body.lower()


def test_stub_provider_appends_regulatory_disclaimer_when_regulated():
    answers = dict(MERGED_ANSWERS)
    answers["constraint_regulated_industry"] = True
    answers["constraint_disclaimer_text"] = "Terms and conditions apply, see website for details."
    p = StubProvider()
    brief = p.generate_brief("tenant-x", answers)
    assert brief.disclaimer == "Terms and conditions apply, see website for details."
    post = p.generate_post(brief, "instagram", answers)
    assert "Terms and conditions apply" in post.body


def test_generation_pipeline_end_to_end_via_api(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    complete_all_steps(client, tenant["api_key"], session["id"])
    resp = client.post(
        f"/api/onboarding/sessions/{session['id']}/generate", headers={"X-API-Key": tenant["api_key"]}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["brief"]["provider"] == "stub"
    channels = {p["channel"] for p in body["posts"]}
    assert channels == {"instagram", "linkedin"}
    for post in body["posts"]:
        assert post["headline"]
        assert post["body"]
        assert post["call_to_action"]


# -- publishing ---------------------------------------------------------


def _sample_post() -> GeneratedPost:
    return GeneratedPost(
        channel="instagram",
        format="carousel",
        headline="Hello world",
        body="Some campaign copy here.",
        call_to_action="Get started today.",
        hashtags=["#brand"],
        image_prompt="a photo",
        image_ref="stub://x.png",
    )


def test_dry_run_publisher_never_makes_network_call():
    publisher = DryRunPublisher()
    result = publisher.publish(_sample_post())
    assert result.status == "dry_run"
    assert result.dry_run is True
    assert "No network call was made." in result.detail["message"]


def test_dry_run_publisher_blocks_incomplete_post():
    bad_post = GeneratedPost(
        channel="instagram", format="carousel", headline="", body="", call_to_action="", hashtags=[]
    )
    result = DryRunPublisher().publish(bad_post)
    assert result.status == "blocked"


def test_instagram_publisher_always_raises():
    with pytest.raises(LivePublishingDisabled):
        InstagramGraphPublisher().publish(_sample_post())


def test_linkedin_publisher_always_raises():
    with pytest.raises(LivePublishingDisabled):
        LinkedInPublisher().publish(_sample_post())


def test_get_publisher_defaults_to_dry_run():
    publisher = get_publisher()
    assert isinstance(publisher, DryRunPublisher)


def test_get_publisher_rejects_unknown_adapter():
    with pytest.raises(ValueError):
        get_publisher("carrier_pigeon")


def test_publish_endpoint_dry_run_records_attempt(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    complete_all_steps(client, tenant["api_key"], session["id"])
    gen = client.post(
        f"/api/onboarding/sessions/{session['id']}/generate", headers={"X-API-Key": tenant["api_key"]}
    ).json()
    post_id = gen["posts"][0]["id"]

    resp = client.post(
        "/api/publish", json={"post_id": post_id, "adapter": "dry_run"}, headers={"X-API-Key": tenant["api_key"]}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dry_run"

    attempts = client.get(
        f"/api/posts/{post_id}/publish-attempts", headers={"X-API-Key": tenant["api_key"]}
    ).json()
    assert len(attempts) == 1
    assert attempts[0]["dry_run"] is True


def test_publish_endpoint_rejects_unknown_adapter_with_400(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    complete_all_steps(client, tenant["api_key"], session["id"])
    gen = client.post(
        f"/api/onboarding/sessions/{session['id']}/generate", headers={"X-API-Key": tenant["api_key"]}
    ).json()
    post_id = gen["posts"][0]["id"]

    resp = client.post(
        "/api/publish",
        json={"post_id": post_id, "adapter": "carrier_pigeon"},
        headers={"X-API-Key": tenant["api_key"]},
    )
    assert resp.status_code == 400
    assert "carrier_pigeon" in resp.json()["detail"]


def test_publish_endpoint_blocks_live_adapter_without_raising_500(client):
    tenant = make_tenant(client)
    _, session = make_client_and_session(client, tenant["api_key"])
    complete_all_steps(client, tenant["api_key"], session["id"])
    gen = client.post(
        f"/api/onboarding/sessions/{session['id']}/generate", headers={"X-API-Key": tenant["api_key"]}
    ).json()
    post_id = gen["posts"][0]["id"]

    resp = client.post(
        "/api/publish",
        json={"post_id": post_id, "adapter": "instagram_graph"},
        headers={"X-API-Key": tenant["api_key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["dry_run"] is False
