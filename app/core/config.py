"""Application configuration.

Environment separation is a hard requirement: test and production run with
fully separate Twilio and Stripe credentials. The active set is selected by
APP_ENV; nothing is ever shared between environments.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Runtime -----------------------------------------------------------
    app_env: Literal["test", "production"] = "test"
    debug: bool = False
    api_base_url: str = "http://localhost:8000"

    # --- Database / cache --------------------------------------------------
    database_url: str = "postgresql+asyncpg://callconfirm:callconfirm@db:5432/callconfirm"
    sync_database_url: str = ""  # derived if empty (used by Alembic/Celery)
    redis_url: str = "redis://redis:6379/0"

    # --- Auth --------------------------------------------------------------
    jwt_secret: str = Field(default="change-me", min_length=8)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 120          # short-lived (15-30 min band)
    refresh_token_days: int = 14            # 7-30 day band
    password_reset_token_hours: int = 1

    # --- Twilio (separate credential sets per environment) -----------------
    twilio_test_account_sid: str = ""
    twilio_test_auth_token: str = ""
    twilio_prod_account_sid: str = ""
    twilio_prod_auth_token: str = ""
    twilio_demo_number: str = ""            # the one shared system demo number
    twilio_webhook_base_url: str = ""       # public base URL Twilio calls back to

    # --- Stripe (separate credential sets per environment) -----------------
    stripe_test_secret_key: str = ""
    stripe_test_webhook_secret: str = ""
    stripe_prod_secret_key: str = ""
    stripe_prod_webhook_secret: str = ""

    # Stripe price IDs (created in the matching Stripe environment)
    stripe_price_panel_base: str = ""       # $5/mo
    stripe_price_api_base: str = ""         # $10/mo
    stripe_price_metered_call: str = ""     # $0.0199/call metered item
    stripe_price_voice_addon: str = ""      # +$1/mo
    per_call_rate_usd: float = 0.0199        # display constant — mirrors the metered Stripe price

    # --- AWS ---------------------------------------------------------------
    aws_region: str = "ap-southeast-1"
    s3_bucket: str = "callconfirm-media"
    # Voice message recordings: local disk in test/dev, S3 in production —
    # same environment-driven split already used for Twilio/Stripe
    # credentials, so local testing never depends on real AWS access.
    local_recordings_dir: str = "/srv/app/data/recordings"

    # --- AI (sentence-check + CSV column mapping; single shared key, not
    # environment-split like Twilio/Stripe since it carries no money/PII risk
    # comparable to those) ---------------------------------------------------
    openai_api_key: str = ""

    # --- Demo mode ---------------------------------------------------------
    demo_lifetime_call_cap: int = 3         # 1-3 lifetime calls per account
    demo_cooldown_minutes: int = 10
    demo_shared_number_calls_per_hour: int = 30  # cap across ALL accounts

    # --- Number provisioning -----------------------------------------------
    number_auto_purchase_cap_global: int = 500
    # per-country caps configurable at runtime via internal admin API (DB-backed)
    # Testing toggle: when True, skip the real Twilio purchase attempt
    # entirely and route straight to manual-approval instead — avoids
    # depending on Twilio inventory (e.g. Vietnam has none) or hitting
    # "No Twilio numbers available" during active dev/testing. The purchase
    # code itself is untouched; flip this back to False whenever real
    # purchasing needs testing again.
    disable_number_auto_purchase: bool = False

    # --- Billing -----------------------------------------------------------
    free_calls_first_month: int = 50
    price_per_call_usd: float = 0.0199
    event_tier1_calls: int = 500
    event_tier1_price: float = 0.02
    event_tier2_price: float = 0.05
    events_enabled: bool = False
    rush_tier_price: float = 0.05
    rush_tier_call_cap: int = 200
    rush_tier_window_minutes: int = 60      # only unlockable within 1h of deadline

    # One-time SIM/number fee for event accounts' first-ever campaign
    # checkout — business accounts already have a number via subscription
    # activation, so this never applies to them. Forced to 0 automatically
    # while disable_number_auto_purchase is True (see campaign_checkout) —
    # charging for a number that can't actually be purchased/assigned yet
    # would be wrong.
    event_sim_fee_usd: float = 3.0
    # One-time fee for requesting an ADDITIONAL number beyond an account's
    # first — either account type, self-service via Settings, after an
    # admin has manually prepared the real number. Higher than the first-
    # number cost on purpose: numbers are a limited resource, this is
    # deliberate friction so only accounts that genuinely need a second
    # number pay for one.
    additional_sim_fee_usd: float = 15.0
    # Rough per-call infrastructure cost estimate (Twilio + TTS), used ONLY
    # to compute an ESTIMATED profit figure on the admin billing page.
    # There is no real per-call cost ledger anywhere in this system — every
    # call incurs a real Twilio charge regardless of whether it was
    # billable to the customer (free-50 calls, event/campaign calls billed
    # as a lump invoice rather than metered), so this applies uniformly to
    # every call placed, not just billable ones. Update this to your real
    # blended per-call cost once you have actual Twilio billing data —
    # this default is a placeholder, not a verified figure.
    estimated_cost_per_call_usd: float = 0.02

    # Minimum contacts required on an event before a campaign can be paid
    # for and sent. Testing toggle below lets a tiny test list through.
    min_campaign_contacts: int = 50
    disable_min_campaign_contacts: bool = False

    # TESTING ONLY — when set, forces every campaign checkout to charge
    # this flat amount instead of real per-call pricing, so a small test
    # contact list can clear Stripe's ~$0.50 minimum-charge floor. Set to
    # None for real pricing (campaign_price_usd's tiered/rush formula).
    # REVERTED to None — real pricing is now active again.
    force_test_campaign_price_usd: float | None = None

    # TESTING ONLY — when True, campaign_checkout skips all trust & safety
    # scoring and always proceeds. Not currently used: the risk pipeline
    # was removed from campaign checkout entirely this session (it was
    # never supposed to be there — see project history), so this flag has
    # no effect anywhere right now. Left in place only in case a future,
    # deliberately-requested reintroduction of risk scoring on campaigns
    # needs a testing bypass.
    disable_risk_pipeline: bool = False

    # --- Risk pipeline thresholds (defaults; DB-configurable) --------------
    risk_hold_threshold: int = 60           # > 60 => auto-hold + manual review
    risk_monitor_threshold: int = 30        # 30-60 => send but monitor
    decline_rate_threshold: float = 0.30    # post-call feedback loop
    abuse_report_rate_threshold: float = 0.02

    # --- Retention ---------------------------------------------------------
    voice_message_retention_days: int = 90
    account_deletion_window_days: int = 30

    # ----------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def twilio_account_sid(self) -> str:
        return self.twilio_prod_account_sid if self.is_production else self.twilio_test_account_sid

    @property
    def twilio_auth_token(self) -> str:
        return self.twilio_prod_auth_token if self.is_production else self.twilio_test_auth_token

    @property
    def stripe_secret_key(self) -> str:
        return self.stripe_prod_secret_key if self.is_production else self.stripe_test_secret_key

    @property
    def stripe_webhook_secret(self) -> str:
        return self.stripe_prod_webhook_secret if self.is_production else self.stripe_test_webhook_secret

    @property
    def sync_db_url(self) -> str:
        if self.sync_database_url:
            return self.sync_database_url
        return self.database_url.replace("+asyncpg", "+psycopg2")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()