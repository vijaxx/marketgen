"""Publishing adapter interface.

This module defines the publishing contract and ships exactly one working
implementation: `DryRunPublisher`. `InstagramGraphPublisher` and
`LinkedInPublisher` exist to show the shape a live adapter would take, but
their `publish()` methods unconditionally raise `LivePublishingDisabled` --
there is no HTTP client, no token handling, and no code path in this
repository that can post to a real account. This is intentional and is the
main thing to point out in a security review of this codebase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.generation.provider import GeneratedPost


class LivePublishingDisabled(RuntimeError):
    """Raised by every live adapter. There is no way to disable this raise
    from configuration -- flipping ALLOW_LIVE_PUBLISHING in .env does not
    change this method's behaviour, because no network code exists here to
    gate. Real publishing would require implementing HTTP calls that this
    repository deliberately does not include."""


@dataclass
class PublishResult:
    status: str  # "dry_run" | "succeeded" | "failed" | "blocked"
    adapter: str
    dry_run: bool
    detail: dict[str, Any]


class Publisher(ABC):
    """Interface every channel publishing adapter implements."""

    adapter_name: str = "base"

    @abstractmethod
    def publish(self, post: GeneratedPost, credentials: dict[str, Any] | None = None) -> PublishResult: ...


class DryRunPublisher(Publisher):
    """The only publisher that actually runs. Validates the post shape and
    records what *would* be sent, without any network call."""

    adapter_name = "dry_run"

    def publish(self, post: GeneratedPost, credentials: dict[str, Any] | None = None) -> PublishResult:
        errors = []
        if not post.headline.strip():
            errors.append("headline is empty")
        if not post.body.strip():
            errors.append("body is empty")
        if not post.call_to_action.strip():
            errors.append("call_to_action is empty")

        if errors:
            return PublishResult(
                status="blocked",
                adapter=self.adapter_name,
                dry_run=True,
                detail={"errors": errors},
            )

        payload_preview = {
            "channel": post.channel,
            "format": post.format,
            "headline": post.headline,
            "body_preview": post.body[:280],
            "call_to_action": post.call_to_action,
            "hashtags": post.hashtags,
            "would_attach_image": bool(post.image_ref),
        }
        return PublishResult(
            status="dry_run",
            adapter=self.adapter_name,
            dry_run=True,
            detail={"message": "No network call was made.", "payload_preview": payload_preview},
        )


class InstagramGraphPublisher(Publisher):
    """Shape of a real Instagram Graph API adapter. Not implemented.

    A real implementation would call the Content Publishing API
    (`POST /{ig-user-id}/media` then `POST /{ig-user-id}/media_publish`) using
    a long-lived Page access token supplied via environment variables, never
    from the frontend. None of that HTTP logic exists here."""

    adapter_name = "instagram_graph"

    def publish(self, post: GeneratedPost, credentials: dict[str, Any] | None = None) -> PublishResult:
        raise LivePublishingDisabled(
            "Instagram Graph publishing is not implemented in this repository. "
            "Use DryRunPublisher for local runs and tests."
        )


class LinkedInPublisher(Publisher):
    """Shape of a real LinkedIn adapter. Not implemented.

    A real implementation would call the LinkedIn Posts API
    (`POST /rest/posts`) using an OAuth2 access token issued to a registered
    LinkedIn app. None of that HTTP logic exists here."""

    adapter_name = "linkedin"

    def publish(self, post: GeneratedPost, credentials: dict[str, Any] | None = None) -> PublishResult:
        raise LivePublishingDisabled(
            "LinkedIn publishing is not implemented in this repository. "
            "Use DryRunPublisher for local runs and tests."
        )


PUBLISHERS: dict[str, type[Publisher]] = {
    "dry_run": DryRunPublisher,
    "instagram_graph": InstagramGraphPublisher,
    "linkedin": LinkedInPublisher,
}


def get_publisher(adapter_name: str = "dry_run") -> Publisher:
    """Factory. Defaults to, and in this repository only ever safely returns,
    the DryRunPublisher -- callers who explicitly request a live adapter get
    an object whose publish() always raises LivePublishingDisabled."""
    cls = PUBLISHERS.get(adapter_name)
    if cls is None:
        raise ValueError(f"unknown publisher adapter: {adapter_name}")
    return cls()
