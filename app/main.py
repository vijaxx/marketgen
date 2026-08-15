"""MarketGen FastAPI application.

Auth model for this demo: a request carries `X-API-Key`. The key hashes to a
row in `api_keys` that names exactly one tenant; every subsequent DB call is
scoped to that tenant via `TenantContext`. There is no way for a request to
name a different tenant than the one its key belongs to -- `tenant_id` is
never read from the request body or query string for authorization purposes.
"""

from __future__ import annotations

import secrets
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import questions as qbank
from app.config import settings
from app.generation.provider import get_provider
from app.publishing.publisher import LivePublishingDisabled, get_publisher
from app.store import Store, get_store
from app.tenancy import TenantContext, TenantIsolationError

app = FastAPI(title="MarketGen API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth / tenant resolution
# ---------------------------------------------------------------------------


def get_db() -> Store:
    return get_store()


def get_tenant_context(
    x_api_key: Optional[str] = Header(default=None), db: Store = Depends(get_db)
) -> TenantContext:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    row = db.resolve_api_key(x_api_key)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    tenant = db.get_tenant_by_id(row["tenant_id"])
    if tenant is None:
        raise HTTPException(status_code=401, detail="Tenant not found for API key")
    return TenantContext(
        tenant_id=tenant["id"], tenant_slug=tenant["slug"], actor_email=row["actor_email"], role=row["role"]
    )


# ---------------------------------------------------------------------------
# Public / meta endpoints -- no secret values are ever returned here.
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/api/config")
def public_config() -> dict[str, Any]:
    """Frontend-safe config: booleans only, never a key."""
    return {
        "content_provider_active": get_provider().name,
        "gemini_enabled": settings.gemini_enabled,
        "allow_live_publishing": settings.allow_live_publishing,
        "questionnaire_version": qbank.questionnaire_version(),
    }


@app.get("/api/questions")
def get_questions() -> dict[str, Any]:
    definition = qbank.load_definition()
    return {"version": definition["version"], "steps": definition["steps"], "questions": definition["questions"]}


# ---------------------------------------------------------------------------
# Demo bootstrap: create a tenant + API key. In production this would be an
# admin-only, out-of-band operation (Supabase auth + service role), not a
# public endpoint -- it is open here purely so the whole flow is curl-able
# with zero external setup.
# ---------------------------------------------------------------------------


class TenantSignupRequest(BaseModel):
    slug: str = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=160)
    actor_email: str


@app.post("/api/tenants", status_code=201)
def create_tenant(req: TenantSignupRequest, db: Store = Depends(get_db)) -> dict[str, Any]:
    if db.get_tenant_by_slug(req.slug) is not None:
        raise HTTPException(status_code=409, detail="slug already taken")
    tenant = db.create_tenant(req.slug, req.name)
    raw_key = f"mg_{secrets.token_urlsafe(32)}"
    db.create_api_key(tenant["id"], raw_key, req.actor_email, role="admin")
    return {"tenant": tenant, "api_key": raw_key}


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class ClientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)


@app.post("/api/clients", status_code=201)
def create_client(
    req: ClientCreateRequest, ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)
) -> dict[str, Any]:
    return db.create_client(ctx, req.name)


@app.get("/api/clients")
def list_clients(ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)) -> list[dict[str, Any]]:
    return db.list_clients(ctx)


# ---------------------------------------------------------------------------
# Onboarding sessions -- the 7-step, 87-question wizard
# ---------------------------------------------------------------------------


class SessionCreateRequest(BaseModel):
    client_id: str


@app.post("/api/onboarding/sessions", status_code=201)
def create_session(
    req: SessionCreateRequest, ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)
) -> dict[str, Any]:
    client = db.get_client(ctx, req.client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="client not found")
    session = db.create_session(ctx, req.client_id, qbank.questionnaire_version())
    return _session_view(session)


@app.get("/api/onboarding/sessions")
def list_sessions(ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)) -> list[dict[str, Any]]:
    return [_session_view(s) for s in db.list_sessions(ctx)]


@app.get("/api/onboarding/sessions/{session_id}")
def get_session(
    session_id: str, ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)
) -> dict[str, Any]:
    session = _require_session(db, ctx, session_id)
    return _session_view(session)


class StepSubmitRequest(BaseModel):
    answers: dict[str, Any]
    advance: bool = True


@app.post("/api/onboarding/sessions/{session_id}/steps/{step}")
def submit_step(
    session_id: str,
    step: int,
    req: StepSubmitRequest,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Store = Depends(get_db),
) -> dict[str, Any]:
    if step < 1 or step > 7:
        raise HTTPException(status_code=400, detail="step must be between 1 and 7")
    session = _require_session(db, ctx, session_id)

    if session["status"] == "completed":
        raise HTTPException(status_code=409, detail="session is already completed")

    try:
        cleaned = qbank.validate_step_answers(step, req.answers, session["answers"])
    except qbank.ValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc

    merged_answers = {**session["answers"], **cleaned}
    completed_steps = set(session["completed_steps"])
    if req.advance:
        completed_steps.add(step)

    next_step = session["current_step"]
    if req.advance:
        next_step = min(step + 1, 7)

    status = "in_progress"
    all_step_ids = {s["id"] for s in qbank.steps()}
    if req.advance and completed_steps >= all_step_ids:
        status = "completed"

    updated = db.save_session_progress(
        ctx,
        session_id,
        merged_answers,
        sorted(completed_steps),
        next_step,
        status,
    )
    return _session_view(updated)


@app.get("/api/onboarding/sessions/{session_id}/progress")
def session_progress(
    session_id: str, ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)
) -> dict[str, Any]:
    session = _require_session(db, ctx, session_id)
    return qbank.step_progress(session["answers"])


@app.get("/api/onboarding/sessions/{session_id}/steps/{step}")
def get_step_questions(
    session_id: str,
    step: int,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Store = Depends(get_db),
) -> dict[str, Any]:
    session = _require_session(db, ctx, session_id)
    visible = qbank.visible_questions_for_step(step, session["answers"])
    return {
        "step": step,
        "questions": visible,
        "answers": {q["id"]: session["answers"].get(q["id"]) for q in visible if q["id"] in session["answers"]},
    }


# ---------------------------------------------------------------------------
# Generation pipeline
# ---------------------------------------------------------------------------


@app.post("/api/onboarding/sessions/{session_id}/generate", status_code=201)
def generate_campaign(
    session_id: str, ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)
) -> dict[str, Any]:
    session = _require_session(db, ctx, session_id)
    if session["status"] != "completed":
        raise HTTPException(status_code=409, detail="onboarding must be completed before generation")

    provider = get_provider()
    brief = provider.generate_brief(ctx.tenant_id, session["answers"])
    brief_row = db.create_brief(ctx, session_id, provider.name, brief.to_dict())

    posts = []
    for channel in brief.target_channels:
        post = provider.generate_post(brief, channel, session["answers"])
        posts.append(db.add_post(ctx, brief_row["id"], post.to_dict()))

    return {"brief": brief_row, "posts": posts}


@app.get("/api/briefs/{brief_id}")
def get_brief(
    brief_id: str, ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)
) -> dict[str, Any]:
    brief = db.get_brief(ctx, brief_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="brief not found")
    posts = db.list_posts_for_brief(ctx, brief_id)
    return {"brief": brief, "posts": posts}


# ---------------------------------------------------------------------------
# Publishing -- dry run only
# ---------------------------------------------------------------------------


class PublishRequest(BaseModel):
    post_id: str
    adapter: str = "dry_run"


@app.post("/api/publish")
def publish_post(
    req: PublishRequest, ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)
) -> dict[str, Any]:
    # Look up the post through a tenant-scoped brief lookup chain is enforced
    # by requiring the caller to have already fetched brief/post via a scoped
    # endpoint; we still re-verify ownership defensively here.
    from app.generation.provider import GeneratedPost

    matching = None
    for row in db.query_scoped(  # type: ignore[attr-defined]
        ctx.tenant_id, "SELECT * FROM generated_posts WHERE tenant_id = ? AND id = ?", (ctx.tenant_id, req.post_id)
    ):
        matching = dict(row)
    if matching is None:
        raise HTTPException(status_code=404, detail="post not found")

    import json as _json

    post = GeneratedPost(
        channel=matching["channel"],
        format=matching["format"],
        headline=matching["headline"],
        body=matching["body"],
        call_to_action=matching["call_to_action"],
        hashtags=_json.loads(matching["hashtags"]),
        image_prompt=matching["image_prompt"],
        image_ref=matching["image_ref"],
    )

    publisher = get_publisher(req.adapter)
    try:
        result = publisher.publish(post)
    except LivePublishingDisabled as exc:
        result_row = db.record_publish_attempt(
            ctx, req.post_id, req.adapter, dry_run=False, status="blocked", detail={"reason": str(exc)}
        )
        return result_row

    result_row = db.record_publish_attempt(
        ctx, req.post_id, result.adapter, result.dry_run, result.status, result.detail
    )
    return result_row


@app.get("/api/posts/{post_id}/publish-attempts")
def list_publish_attempts(
    post_id: str, ctx: TenantContext = Depends(get_tenant_context), db: Store = Depends(get_db)
) -> list[dict[str, Any]]:
    return db.list_publish_attempts(ctx, post_id)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_session(db: Store, ctx: TenantContext, session_id: str) -> dict[str, Any]:
    session = db.get_session(ctx, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _session_view(session: dict[str, Any]) -> dict[str, Any]:
    progress = qbank.step_progress(session["answers"])
    return {**session, "progress": progress}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
