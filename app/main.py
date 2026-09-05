"""CallConfirm backend.

Three physically separated API surfaces, mounted as separate sub-applications:

  /api/public   — external, versioned, documented. This is the ONLY app whose
                  OpenAPI schema is generated (docs at /api/public/docs).
  /api/internal — platform-admin-only. openapi_url=None: structurally
                  undiscoverable from docs tooling, not merely permission-hidden.
  /api/company  — backend for the Business/Event admin panels. Also undocumented.
"""
from fastapi import FastAPI

import logging

from app.core.config import settings
from app.core.error_handling import SuccessEnvelopeMiddleware, register_error_handlers

# Without this, Python's root logger defaults to WARNING and every .info()
# call throughout the app (notably notifications.py's log-backend OTP/email
# output, the only way to read verification codes in local dev without a
# real email/SMS provider) is silently dropped — the function runs, nothing
# ever appears in `docker compose logs`. Configure once, at import time, so
# it applies regardless of which sub-app/module runs first.
logging.basicConfig(
    level=logging.INFO if not settings.is_production else logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# --- Public app (the only source for generated API docs) --------------------
public_app = FastAPI(
    title="CallConfirm Public API",
    version="1.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)
from app.api.public.v1 import appointments as public_appointments  # noqa: E402
from app.api.public.v1 import event_signup as public_event_signup  # noqa: E402
from app.api.public.v1 import events as public_events  # noqa: E402
from app.api.public.v1 import meta as public_meta  # noqa: E402
from app.api.public.v1 import webhooks as public_webhooks  # noqa: E402

public_app.include_router(public_appointments.router, prefix="/v1")
public_app.include_router(public_event_signup.router, prefix="/v1")
public_app.include_router(public_events.router, prefix="/v1")
public_app.include_router(public_meta.router, prefix="/v1")
public_app.include_router(public_webhooks.router, prefix="/v1")
register_error_handlers(public_app)
# Success envelope applied ONLY to the public (sold-to-integrators) API for now —
# internal/company panels can adopt the same envelope later without affecting
# external customers already integrated against this shape.
public_app.add_middleware(SuccessEnvelopeMiddleware)

# --- Internal app (no OpenAPI schema at all) --------------------------------
internal_app = FastAPI(title="internal", docs_url=None, redoc_url=None, openapi_url=None)
from app.api.internal import (  # noqa: E402
    admin_accounts, auth as internal_auth, number_pool, platform_billing, risk_review, template_config,
    voice_config,
)

internal_app.include_router(internal_auth.router)
internal_app.include_router(admin_accounts.router)
internal_app.include_router(risk_review.router)
internal_app.include_router(number_pool.router)
internal_app.include_router(platform_billing.router)
internal_app.include_router(voice_config.router)
internal_app.include_router(template_config.router)
register_error_handlers(internal_app)

# --- Company panel app (no OpenAPI schema) ----------------------------------
company_app = FastAPI(title="company", docs_url=None, redoc_url=None, openapi_url=None)
from app.api.company import appointments as company_appointments  # noqa: E402
from app.api.company import auth as company_auth  # noqa: E402
from app.api.company import billing as company_billing  # noqa: E402
from app.api.company import dashboard as company_dashboard  # noqa: E402
from app.api.company import events as company_events  # noqa: E402
from app.api.company import settings as company_settings  # noqa: E402

company_app.include_router(company_auth.router)
company_app.include_router(company_dashboard.router)
company_app.include_router(company_events.router)
company_app.include_router(company_settings.router)
company_app.include_router(company_billing.router)
company_app.include_router(company_appointments.router)
register_error_handlers(company_app)

# --- Root -------------------------------------------------------------------
app = FastAPI(title="CallConfirm", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/api/public", public_app)
app.mount("/api/internal", internal_app)
app.mount("/api/company", company_app)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "env": settings.app_env}