# CallConfirm — Automated Appointment & Event Confirmation Calling Platform

FastAPI backend for two products sharing one Twilio call engine: automated appointment reminder calls for small service businesses, and pre-sale event confirmation calling for artists/promoters. Python 3.12, FastAPI (async), PostgreSQL, Celery + Redis, Stripe, Twilio, S3, Docker.

## Architecture at a glance

Three physically separated API surfaces, mounted as independent FastAPI sub-applications in `app/main.py`:

| Surface | Path | Auth | Docs |
|---|---|---|---|
| Public API | `/api/public/v1` | API keys (`sk_live_...`/`sk_test_...`), scoped, paid tier | `/api/public/docs` — the **only** generated docs |
| Internal admin | `/api/internal` | JWT + explicit `platform_admin` check per request | none (`openapi_url=None` — structurally undiscoverable) |
| Company panels | `/api/company` | JWT (business/event owners) | none |

Business and Event accounts are separate tables (`business_accounts`, `event_accounts`), not a type column. Passwords use Argon2; API keys are stored as SHA-256 hashes with per-key scopes (`appointments:read`, `appointments:write`, `events:create`).

Key modules:

- `app/services/demo_mode.py` — Demo Mode rules (self-call-only, lifetime cap, cooldown, shared-number hourly cap), all server-side.
- `app/services/number_provisioning.py` — on-demand per-country numbers: reuse released numbers first, purchase from Twilio in real time otherwise, route to the manual-approval queue when the auto-purchase cap is hit.
- `app/services/risk/` — 4-layer trust & safety pipeline (keyword scan, template deviation, composite 0–100 rules-based score with hold >60 / monitor 30–60, and the post-call press-2/press-9 feedback loop in `app/tasks/feedback.py`). Structural hard blocks (payment-detail requests, prize framing without stored opt-in) reject outright before scoring.
- `app/api/public/v1/webhooks.py` — Twilio call lifecycle + Stripe events. **All paid/suspended/campaign-state transitions happen only here**, never in checkout responses.
- `app/tasks/` — Celery: due-appointment scanner (every 60s, timezone-aware), dialer (Twilio's default queued rate limiting; no custom parallel dialing in Phase 1), campaign sender, 90-day voice-recording export+delete, 30-day account deletion, risk feedback loop.

## Environment variables

Copy `.env.example` to `.env` and fill it in. The important design rule: **test and production Twilio/Stripe credentials are fully separate variables** (`TWILIO_TEST_*` / `TWILIO_PROD_*`, `STRIPE_TEST_*` / `STRIPE_PROD_*`). `APP_ENV=test|production` selects which set is active at runtime; the two are never mixed, and every `Call` row records the environment it was placed from.

Other notable variables: `TWILIO_DEMO_NUMBER` (the single shared demo-mode number), `TWILIO_WEBHOOK_BASE_URL` (public HTTPS URL for Twilio callbacks — use ngrok or similar locally), the four `STRIPE_PRICE_*` price IDs, and optional `ANTHROPIC_API_KEY` for AI column-mapping on bulk uploads (a heuristic mapper is used when unset).

## Local development (Docker Compose)

```bash
cp .env.example .env       # fill in at least JWT_SECRET; Twilio/Stripe test keys as needed
docker compose up --build  # api (hot reload) + worker + beat + Postgres + Redis
docker compose exec api alembic upgrade head
docker compose exec api python scripts/create_admin.py admin@example.com 'a-strong-password'
```

API: http://localhost:8000 — health at `/healthz`, public docs at `/api/public/docs`. The same Dockerfile/image is used locally and in production; only environment configuration differs. One image serves the web process, Celery worker, and beat (compose overrides the start command).

## Stripe setup (both environments)

Create in each Stripe environment: a $5/mo price (Panel), $10/mo price (API tier), a $0.0199/call **metered** price on the same product, and a $1/mo voice-messaging add-on price; put their IDs in the matching `STRIPE_PRICE_*` variables. In Billing settings, enable **Smart Retries** and set the after-final-retry subscription status to **"unpaid"** (not canceled). Point a webhook endpoint at `/api/public/v1/webhooks/stripe` subscribed to `invoice.payment_failed`, `invoice.paid`, `customer.subscription.updated`, and `payment_intent.succeeded`, and copy the signing secret into `STRIPE_*_WEBHOOK_SECRET`.

Card validation at activation uses a confirmed $0 SetupIntent — no charge. The voice add-on toggle changes feature access immediately but modifies the subscription with `proration_behavior="none"`, so billing only changes at the next cycle. The first 50 calls in month one are tracked on the account and excluded from metered usage reporting.

## Production deployment (EC2, manual)

Prerequisites on the instance: Docker + Compose plugin, a checkout of this repo, `/etc/callconfirm/.env` containing production values (`APP_ENV=production`, RDS `DATABASE_URL`, ElastiCache `REDIS_URL`, `TWILIO_PROD_*`, `STRIPE_PROD_*`). RDS and ElastiCache are managed AWS services reached over the network — only the api/worker/beat containers run on the instance.

```bash
./scripts/deploy.sh
```

The script pulls the latest code, rebuilds the shared image, runs `alembic upgrade head`, and restarts the containers via `docker-compose.prod.yml`. Put a reverse proxy (nginx/ALB) with TLS in front of port 8000 and set `TWILIO_WEBHOOK_BASE_URL` to that public HTTPS origin.

## Data retention

Voice recordings are exported by email to the account owner and deleted from S3 after 90 days (daily Celery task). Verified account-deletion requests purge all account data within 30 days. Operational data (appointments, call logs) is retained while the account is active.

## Phase 2 extension points (documented, not built)

Waitlist/slot-refill attaches at the marked comment in `app/models/domain.py::Appointment`; custom concurrent dialing wraps `dial_call` in `app/tasks/calling.py`; third-party ID verification (e.g. Stripe Identity) hooks into account verification for high-risk accounts; multilingual notifications localize `app/services/notifications.py` using the already-stored `preferred_language`. Also out of scope for Phase 1: "money saved" analytics, no-show pattern flagging, SMS fallback, multi-location accounts, white-label/speech-recognition replies.
