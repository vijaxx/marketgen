"""Content generation provider interface.

`ContentProvider` is the seam between the onboarding answers and whatever
actually writes the copy. Two implementations exist:

* `StubProvider`  -- deterministic, offline, zero credentials. The default,
  and the only one exercised by the test suite.
* `GeminiProvider` -- a real adapter for the Google Gemini API. It only
  activates when `GEMINI_API_KEY` is set (see app/config.py) and is never
  invoked in tests or in the zero-credential local run path.

`get_provider()` is the single factory the rest of the app should call.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

CHANNEL_FORMAT_DEFAULTS = {
    "instagram": "carousel",
    "linkedin": "text_post",
    "facebook": "static",
    "x": "text_post",
    "youtube": "short",
    "pinterest": "pin",
    "email": "newsletter",
    "blog": "article",
    "whatsapp": "broadcast",
}


@dataclass
class CampaignBrief:
    tenant_id: str
    client_name: str
    headline: str
    summary: str
    key_messages: list[str]
    target_channels: list[str]
    tone_descriptor: str
    disclaimer: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_name": self.client_name,
            "headline": self.headline,
            "summary": self.summary,
            "key_messages": self.key_messages,
            "target_channels": self.target_channels,
            "tone_descriptor": self.tone_descriptor,
            "disclaimer": self.disclaimer,
            "metadata": self.metadata,
        }


@dataclass
class GeneratedPost:
    channel: str
    format: str
    headline: str
    body: str
    call_to_action: str
    hashtags: list[str]
    image_prompt: str | None = None
    image_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "format": self.format,
            "headline": self.headline,
            "body": self.body,
            "call_to_action": self.call_to_action,
            "hashtags": self.hashtags,
            "image_prompt": self.image_prompt,
            "image_ref": self.image_ref,
        }


class ContentProvider(ABC):
    """Interface every generation backend implements."""

    name: str = "base"

    @abstractmethod
    def generate_brief(self, tenant_id: str, answers: dict[str, Any]) -> CampaignBrief: ...

    @abstractmethod
    def generate_post(self, brief: CampaignBrief, channel: str, answers: dict[str, Any]) -> GeneratedPost: ...


class StubProvider(ContentProvider):
    """Deterministic offline provider. No network calls, ever.

    Determinism is achieved by deriving every piece of "creative" output from
    the onboarding answers themselves (a stable hash selects among a fixed set
    of templates), so the same answers always yield the same brief and posts.
    This makes the generation pipeline fully testable without mocking a model.
    """

    name = "stub"

    _CTA_TEMPLATES = {
        "direct": "Get started with {brand} today.",
        "soft": "See what {brand} can do for you.",
        "question": "Ready to try {brand}?",
        "urgency": "Limited spots — join {brand} now.",
    }

    def _seed(self, *parts: str) -> int:
        digest = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    def generate_brief(self, tenant_id: str, answers: dict[str, Any]) -> CampaignBrief:
        brand = answers.get("brand_display_name", "The Brand")
        one_liner = answers.get("brand_one_liner", "")
        usp = answers.get("offer_usp", "")
        primary_goal = answers.get("goal_primary", "awareness")
        segment = answers.get("audience_primary_segment", "our audience")
        tone_primary = answers.get("tone_primary", "friendly")
        channels = answers.get("channels_active", ["instagram"])

        key_messages = [m for m in [one_liner, usp, f"Built for {segment}."] if m]

        disclaimer = None
        if answers.get("constraint_regulated_industry"):
            disclaimer = answers.get("constraint_disclaimer_text")

        return CampaignBrief(
            tenant_id=tenant_id,
            client_name=brand,
            headline=f"{brand}: {one_liner or 'campaign brief'}".strip(": "),
            summary=(
                f"A {tone_primary} campaign for {brand}, targeting {segment}, "
                f"optimised for {primary_goal.replace('_', ' ')}."
            ),
            key_messages=key_messages,
            target_channels=list(channels),
            tone_descriptor=tone_primary,
            disclaimer=disclaimer,
            metadata={"provider": self.name, "seed_basis": "answers-hash"},
        )

    def generate_post(self, brief: CampaignBrief, channel: str, answers: dict[str, Any]) -> GeneratedPost:
        seed = self._seed(brief.tenant_id, channel, brief.headline)
        fmt = CHANNEL_FORMAT_DEFAULTS.get(channel, "text_post")
        cta_style = answers.get("tone_cta_style", "direct")
        cta_template = self._CTA_TEMPLATES.get(cta_style, self._CTA_TEMPLATES["direct"])
        cta = cta_template.format(brand=brief.client_name)

        message = brief.key_messages[seed % len(brief.key_messages)] if brief.key_messages else brief.summary
        headline = f"{brief.client_name} on {channel.title()}"
        body_parts = [message, brief.summary]
        if brief.disclaimer:
            body_parts.append(brief.disclaimer)
        body = " ".join(body_parts)

        banned_raw = answers.get("tone_banned_words", "") or ""
        banned = [w.strip().lower() for w in banned_raw.split(",") if w.strip()]
        for word in banned:
            if word and word in body.lower():
                # Hard filter: banned words are redacted, never shipped.
                body = _redact(body, word)

        values = answers.get("brand_values", [])
        hashtags = [f"#{v.replace('_', '')}" for v in values[:3]] or ["#marketing"]

        image_prompt = (
            f"{brief.tone_descriptor} lifestyle photography for {brief.client_name}, "
            f"emphasising {', '.join(values) or 'the brand'}, no text overlay"
        )

        return GeneratedPost(
            channel=channel,
            format=fmt,
            headline=headline,
            body=body,
            call_to_action=cta,
            hashtags=hashtags,
            image_prompt=image_prompt,
            image_ref=f"stub://{brief.tenant_id}/{channel}/{seed}.png",
        )


class GeminiProvider(ContentProvider):
    """Real Google Gemini adapter.

    This class only activates when a live API key is supplied through
    `app.config.settings.gemini_api_key` -- see `get_provider()` below. It is
    never called by the test suite or by the zero-credential local run path,
    and this repository ships no key. Network calls happen through `httpx`,
    imported lazily so the dependency is optional for the offline path.
    """

    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("GeminiProvider requires a non-empty API key")
        self._api_key = api_key
        self._model = model

    def _call_gemini(self, prompt: str) -> str:
        import httpx  # local import: keeps httpx optional for the offline path

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:generateContent?key={self._api_key}"
        )
        response = httpx.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    def generate_brief(self, tenant_id: str, answers: dict[str, Any]) -> CampaignBrief:
        prompt = (
            "Produce a JSON campaign brief with keys headline, summary, key_messages "
            f"(array) for this brand profile: {json.dumps(answers)}"
        )
        raw = self._call_gemini(prompt)
        parsed = json.loads(raw)
        return CampaignBrief(
            tenant_id=tenant_id,
            client_name=answers.get("brand_display_name", "The Brand"),
            headline=parsed["headline"],
            summary=parsed["summary"],
            key_messages=parsed.get("key_messages", []),
            target_channels=list(answers.get("channels_active", [])),
            tone_descriptor=answers.get("tone_primary", "friendly"),
            disclaimer=answers.get("constraint_disclaimer_text") if answers.get("constraint_regulated_industry") else None,
            metadata={"provider": self.name, "model": self._model},
        )

    def generate_post(self, brief: CampaignBrief, channel: str, answers: dict[str, Any]) -> GeneratedPost:
        prompt = (
            f"Write a {channel} post as JSON with keys headline, body, call_to_action, hashtags "
            f"(array) for brief: {json.dumps(brief.to_dict())}"
        )
        raw = self._call_gemini(prompt)
        parsed = json.loads(raw)
        return GeneratedPost(
            channel=channel,
            format=CHANNEL_FORMAT_DEFAULTS.get(channel, "text_post"),
            headline=parsed["headline"],
            body=parsed["body"],
            call_to_action=parsed["call_to_action"],
            hashtags=parsed.get("hashtags", []),
            image_prompt=parsed.get("image_prompt"),
            image_ref=None,
        )


def _redact(text: str, word: str) -> str:
    import re as _re

    return _re.sub(_re.escape(word), "*" * len(word), text, flags=_re.IGNORECASE)


def get_provider() -> ContentProvider:
    """Factory: chooses the provider based on config, defaulting to the stub.

    Even when CONTENT_PROVIDER=gemini is requested, this falls back to the
    stub unless a real API key is present -- the app never silently attempts
    a network call it cannot authenticate.
    """
    from app.config import settings

    if settings.content_provider == "gemini" and settings.gemini_enabled:
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model)  # type: ignore[arg-type]
    return StubProvider()
