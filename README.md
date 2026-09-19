# MarketGen

An AI marketing automation platform: a multi-tenant client-onboarding wizard
(87 questions across 7 steps, with real conditional branching and resumable
progress), a content-generation pipeline behind a provider interface, and
publishing adapters that are wired up as real interfaces but can never post
to a live account from this codebase.

This repository runs **end to end with zero credentials**. Every test in
`tests/` and every curl command in this README was actually executed against
the code in this repo before it was committed.

## Contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [The onboarding wizard: 87 questions, 7 steps, branching](#the-onboarding-wizard-87-questions-7-steps-branching)
- [Multi-tenant isolation](#multi-tenant-isolation)
- [Content generation: the provider interface](#content-generation-the-provider-interface)
- [Publishing adapters: interfaces only](#publishing-adapters-interfaces-only)
- [Credential handling](#credential-handling)
- [Testing](#testing)
- [What requires live services (honest gaps)](#what-requires-live-services-honest-gaps)

## Quick start

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # optional -- defaults already work with zero edits

uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` — that's the whole frontend, one static HTML
file served by the API itself. Create a workspace, walk through the 7-step
wizard, and it will generate a campaign brief plus per-channel posts using
the offline stub provider, then let you dry-run "publish" them.

Run the tests:

```bash
pytest -q
# 69 passed
```

Drive the API directly:

```bash
curl -s -X POST localhost:8000/api/tenants \
  -H "Content-Type: application/json" \
  -d '{"slug":"acme","name":"Acme Co","actor_email":"you@acme.com"}'
# -> {"tenant": {...}, "api_key": "mg_..."}  (save the api_key)
```

## Architecture

```
app/
  main.py            FastAPI app: auth, tenant resolution, all routes
  tenancy.py          TenantContext + the static SQL-scoping guard
  store.py            SQLite storage layer, every method tenant-scoped
  questions.py         Loads/validates the question bank, branching, progress
  config.py            Env-based settings, zero secrets hardcoded
  data/questions.json  The 87-question, 7-step definition
  generation/
    provider.py         ContentProvider interface, StubProvider, GeminiProvider
  publishing/
    publisher.py         Publisher interface, DryRunPublisher, disabled live adapters
  static/index.html      The entire frontend (vanilla JS, no build step)
migrations/
  postgres/               Real Postgres schema + row-level security (reviewed, not run here)
  sqlite/                 The schema that actually runs locally and in tests
n8n/                    Exported workflow JSON (importable, not executed here)
tests/                  69 pytest tests
```

Everything is one FastAPI process talking to one SQLite file. There's no
build step for the frontend — `app/static/index.html` is fetched, loads
`/api/questions`, and drives itself.

## The onboarding wizard: 87 questions, 7 steps, branching

`app/data/questions.json` defines all 87 questions, each with a `step`
(1–7), a `type` (text / textarea / url / email / number / scale / boolean /
single_select / multi_select), validation constraints (`min_length`,
`pattern`, `min`/`max`, `min_select`/`max_select`, etc.), and an optional
`show_if` condition:

```json
{
  "id": "brand_industry_other",
  "step": 1,
  "show_if": { "question_id": "brand_industry", "op": "eq", "value": "other" },
  ...
}
```

`app/questions.py` evaluates `show_if` against the accumulated answers for
that session (`is_visible`), so a question that depends on an earlier answer
only appears — and is only required — once its condition is met. Operators
supported: `truthy`, `falsy`, `eq`, `ne`, `includes`, `excludes`. There's also
a `gte_question` cross-field check (e.g. "oldest age" must be ≥ "youngest
age").

Steps:

1. Brand Foundations (13 questions)
2. Products & Offers (13)
3. Audience & Personas (13)
4. Voice & Tone (12)
5. Channels & Cadence (12)
6. Goals & Metrics (12)
7. Competitors & Constraints (12)

`POST /api/onboarding/sessions/{id}/steps/{step}` validates only the
questions that are currently *visible* for that step given answers so far,
rejects answers submitted for a question that isn't applicable, merges the
cleaned values into the session's persisted `answers` JSON blob, and — with
`advance: true` — moves `current_step` forward and marks the step complete.
Sessions are rows in `onboarding_sessions` (SQLite locally); nothing lives
only in browser memory, so **closing the tab and coming back later resumes
exactly where you left off** — `GET /api/onboarding/sessions/{id}` returns
the same `answers`, `current_step`, and `completed_steps` it had before.
`GET /api/onboarding/sessions/{id}/progress` reports per-step and overall
completion percentage, correctly excluding questions hidden by branching.

## Multi-tenant isolation

This is the part most worth reading carefully if you're evaluating the repo.

**The model.** Every tenant-scoped table (`clients`, `onboarding_sessions`,
`campaign_briefs`, `generated_posts`, `publish_attempts`, `api_keys`) carries
a `tenant_id` column. A request authenticates with an `X-API-Key` header,
which resolves (via a SHA-256 hash lookup — the raw key is never stored) to
exactly one tenant. Every subsequent database call in that request is made
through a `TenantContext` carrying that tenant's id, and `tenant_id` is
**never** taken from the request body or query string for authorization
purposes — a client cannot ask to see a different tenant's data by passing a
different id in the payload.

**Three independent layers enforce isolation:**

1. **Application-level SQL guard** (`app/tenancy.py`, exercised locally). Every
   `Store` method that touches a tenant-scoped table goes through
   `execute_scoped()` / `query_scoped()`, which calls
   `assert_tenant_scoped(sql)` — a static check that the SQL text actually
   filters on `tenant_id = ?` (or, for `INSERT`, sets a `tenant_id` column) —
   *and* asserts the `tenant_id` value itself is bound as a query parameter.
   A statement missing either raises `TenantGuardError` before it ever
   reaches SQLite. This is what the 69 tests actually exercise.
2. **Row-level security in Postgres** (`migrations/postgres/002_row_level_security.sql`,
   reviewed but not run here — no Postgres in this environment). The
   application connects as a role *without* `BYPASSRLS`; every table has
   `FORCE ROW LEVEL SECURITY` and a policy comparing `tenant_id` against a
   per-transaction `app.tenant_id` setting via `set_config()`. A request that
   forgets to set the setting sees **zero rows**, not all of them — it fails
   closed.
3. **Ordinary `WHERE tenant_id = ?` predicates** on every read/write, which is
   the layer doing the actual filtering in both databases.

**Proof, not assertion.** `tests/test_tenant_isolation.py` creates two
tenants and shows, through the real HTTP API, that:

- Tenant A's client list never contains Tenant B's clients.
- Fetching Tenant B's session id from Tenant A's key returns `404`, not
  `403` — the row is invisible, not merely forbidden, which is what you want
  from a leak-prevention posture (no "resource exists but you can't see it"
  signal).
- Tenant A cannot submit onboarding answers into Tenant B's session.
- Tenant A cannot fetch Tenant B's generated campaign brief.
- Tenant A cannot publish (even dry-run) Tenant B's post.
- Two tenants can each independently create a client with the *same name* —
  proving the uniqueness constraint is `(tenant_id, name)`, not a global
  `name` constraint (checked against both the SQLite and Postgres schemas).
- The storage guard itself rejects an unscoped `SELECT` and rejects a query
  where `tenant_id` isn't actually bound as a parameter, independent of the
  API layer.

## Content generation: the provider interface

`app/generation/provider.py` defines `ContentProvider` with two methods:
`generate_brief(tenant_id, answers) -> CampaignBrief` and
`generate_post(brief, channel, answers) -> GeneratedPost`.

- **`StubProvider`** (the default, `CONTENT_PROVIDER=stub`) is fully offline
  and deterministic: it derives headlines, body copy, hashtags, and even a
  stable "image ref" from the onboarding answers via a SHA-256-seeded
  selection, so identical answers always produce identical output — which is
  what makes it unit-testable without mocking a model. It also enforces the
  onboarding-supplied banned-word list (redacts matches) and appends the
  regulatory disclaimer text when the client is in a regulated industry.
- **`GeminiProvider`** is a real adapter for the Google Gemini API
  (`generativelanguage.googleapis.com`). It only exists to show the intended
  shape of a live integration; `get_provider()` only returns it when
  `CONTENT_PROVIDER=gemini` **and** `GEMINI_API_KEY` is actually set — with no
  key present, the app silently uses the stub instead of failing. **No test in
  this repo calls `GeminiProvider`**, and no key is shipped.

## Publishing adapters: interfaces only

`app/publishing/publisher.py` defines a `Publisher` interface with one method,
`publish(post) -> PublishResult`. Three implementations exist:

- **`DryRunPublisher`** — the only one that actually runs. Validates the post
  has a non-empty headline/body/CTA, and returns a `PublishResult` describing
  exactly what *would* be sent, with `dry_run=True`. No HTTP client is even
  imported.
- **`InstagramGraphPublisher`** and **`LinkedInPublisher`** — exist to show the
  shape a real adapter would take (docstrings note the actual Graph/Posts API
  calls they'd need to make), but their `publish()` methods **unconditionally
  raise `LivePublishingDisabled`**. There is no code path anywhere in this
  repository that makes an outbound HTTP call to Instagram or LinkedIn. The
  `ALLOW_LIVE_PUBLISHING` env flag exists for documentation/future-config
  purposes only — flipping it does not unlock anything, because the network
  logic itself was never written.

The `/api/publish` endpoint calls whichever adapter is requested; requesting
a live one returns HTTP 200 with `status: "blocked"`, not a 500 — the
"can't post live" case is handled, not an accident.

## Credential handling

- All secrets (`GEMINI_API_KEY`, `DATABASE_URL`) are read server-side from
  environment variables in `app/config.py`. Nothing in `app/static/index.html`
  contains, requests, or has access to any of them.
- `GET /api/config`, the one config endpoint the frontend calls, returns only
  booleans (`gemini_enabled`, `allow_live_publishing`) and the active
  provider *name* — never a key value.
- API keys presented by clients (`X-API-Key`) are stored server-side as
  SHA-256 hashes (`api_keys.key_hash`), never in plaintext; the raw key is
  returned once, at creation time, exactly like a typical API key UX.
- `.env.example` is committed with placeholder values; `.env` is gitignored.
  This mirrors the original design's idea of keeping provider credentials
  inside a managed credential store (n8n's credential manager / Supabase
  service role) rather than in application code or the client bundle.

## Testing

```bash
pytest -q
```

69 tests across 5 files:

- `tests/test_question_bank.py` — all 87 questions present, valid step
  assignment, no duplicate ids, `show_if` references resolve, genuine
  branching exists across ≥5 steps.
- `tests/test_validation_and_branching.py` — per-type validation (length,
  pattern, numeric range, select membership, multi-select bounds), branching
  visibility (hidden questions aren't required, answers to inapplicable
  questions are rejected), cross-field `gte_question` checks, progress
  accounting under branching.
- `tests/test_onboarding_api.py` — the full session lifecycle over HTTP:
  creation, per-step submission and advancement, 422 on invalid submission,
  **resume** (fetch a session after a partial submission and see prior
  answers/step intact), the full 7-step happy path completing the session,
  generation correctly gated on completion, auth rejection.
- `tests/test_tenant_isolation.py` — see [above](#multi-tenant-isolation).
- `tests/test_generation_and_publishing.py` — stub provider determinism,
  per-channel variation, banned-word redaction, regulatory disclaimer
  injection, end-to-end generation via the API, `DryRunPublisher` behaviour
  (including blocking incomplete posts), both live adapters raising on every
  call, `/api/publish` never 500ing when a live adapter is requested.

## What requires live services (honest gaps)

The original brief for this class of product envisions hosted Supabase,
n8n Cloud, and the Gemini API. None of that runs in this repository. Being
specific about the boundary:

| Piece | What's here | What's NOT exercised here |
|---|---|---|
| Database | Real SQLite schema (`migrations/sqlite/001_schema.sql`) that the app actually runs against, plus a reviewed, hand-written Postgres schema with genuine RLS policies (`migrations/postgres/*.sql`) | No Postgres instance exists in this environment. The Postgres migrations were never applied or executed against a real database — they are reviewed SQL, not tested SQL. Tenant isolation is instead proven against the SQLite path plus the application-level guard. |
| Workflow orchestration | Two real, importable n8n workflow JSON exports (`n8n/*.json`) describing how a hosted n8n instance would call this API on webhook/schedule triggers | No n8n instance runs here. The JSON was hand-authored to n8n's real export schema but never opened in an actual n8n editor or executed. Treat it as a documented integration sketch, not a tested workflow. |
| Content generation | `GeminiProvider` class with real Gemini REST call logic | Never invoked — no `GEMINI_API_KEY` exists in this environment, and no test calls it. If you add a real key via `.env`, `CONTENT_PROVIDER=gemini` activates it; that path is untested by this repo's own test suite. |
| Publishing | `Publisher` interface, `InstagramGraphPublisher`/`LinkedInPublisher` classes | These do not contain working HTTP logic at all — they exist to define the shape and unconditionally raise `LivePublishingDisabled`. There is no live posting code path to run, on purpose. |
| Auth / tenant provisioning | A simple, workable `POST /api/tenants` bootstrap endpoint | A production system would put this behind Supabase Auth + an admin-only service role, not a public endpoint. It's open here specifically so the whole flow is curl-able with zero external setup. |

Everything else in this README — the wizard, the branching, the validation,
resumability, tenant isolation, the stub generation pipeline, and dry-run
publishing — was run, in this repository, before it was committed.

---

## License

MIT.
