"""Trust & safety — layer 3 (composite score + manual review gate) and
layer 4 (post-call behavioral feedback loop).

Simple rules-based point system (0-100), per spec — no trained ML model.

  > hold_threshold (60): auto-hold + platform admin notified for manual review
  monitor..hold (30-60): send, flagged for passive monitoring
  < monitor (30):        proceed normally

Layer 4 runs continuously after calls complete (Celery task): if an account's
real decline rate or press-9 abuse-report rate exceeds thresholds, the
account is queued for MANUAL admin review (a RiskScore row with no
campaign_id/appointment_id, same queue campaign/appointment holds use — see
app/api/internal/risk_review.py) and the account holder is emailed.

This does NOT instantly restrict anything already in flight: calls already
dispatched keep resolving normally. Only genuinely NEW work is blocked while
review is pending — see create_appointment's and campaign_checkout's
manual_review_required checks, and send_campaign's mid-dispatch pause for a
campaign still actively sending when the flag trips. An admin clears the
account from the review queue to resume new sends (and any paused campaign).
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    Call, CallResult, KeypressResult, PhoneNumberPool, ReviewStatus, RiskScore,
)
from app.services import notifications
from app.services.risk.content import ContentCheck, evaluate_content


@dataclass
class RiskDecision:
    score: int
    breakdown: dict
    action: str                 # "proceed" | "monitor" | "hold" | "reject"
    hard_block_reasons: list[str]


async def _account_signals(db: AsyncSession, account) -> tuple[int, dict]:
    """Account age, verification completeness, payment presence. 0-15."""
    points, detail = 0, {}
    age = datetime.now(timezone.utc) - account.created_at
    if age < timedelta(days=1):
        points += 8; detail["account_under_24h"] = 8
    elif age < timedelta(days=7):
        points += 4; detail["account_under_7d"] = 4
    if not account.email_verified or not account.phone_verified:
        points += 5; detail["incomplete_verification"] = 5
    if not account.stripe_customer_id:
        points += 2; detail["no_payment_method"] = 2
    return points, detail


async def _volume_signals(
    db: AsyncSession, account, account_type: str, list_size: int
) -> tuple[int, dict]:
    """List size vs. history; number reuse across unrelated accounts. 0-15."""
    points, detail = 0, {}
    col = Call.business_account_id if account_type == "business" else Call.event_account_id
    historic = (
        await db.execute(select(func.count(Call.id)).where(col == account.id))
    ).scalar_one()
    if list_size > max(historic * 5, 100):
        points += 8; detail["list_size_vs_history"] = 8
    elif list_size > max(historic * 2, 50):
        points += 4; detail["list_size_vs_history"] = 4

    # Number reuse signal: how many distinct prior accounts used this account's
    # current number (released-number churn across unrelated accounts).
    number_row = (
        await db.execute(
            select(PhoneNumberPool).where(
                PhoneNumberPool.assigned_account_type == account_type,
                PhoneNumberPool.assigned_account_id == account.id,
            )
        )
    ).scalar_one_or_none()
    if number_row:
        prior_users = (
            await db.execute(
                select(func.count(func.distinct(Call.business_account_id))).where(
                    Call.from_number == number_row.number,
                    Call.business_account_id != (account.id if account_type == "business" else None),
                )
            )
        ).scalar_one()
        if prior_users and prior_users > 2:
            points += 7; detail["number_reused_across_accounts"] = 7
    return points, detail


async def _behavior_signals(db: AsyncSession, account, account_type: str) -> tuple[int, dict]:
    """Historical decline / press-9 report rate. 0-15 (plus the layer-4 flag)."""
    points, detail = 0, {}
    decline, report, total = await account_feedback_rates(db, account.id, account_type)
    if total >= 10:
        if report > settings.abuse_report_rate_threshold:
            points += 10; detail["abuse_report_rate"] = 10
        if decline > settings.decline_rate_threshold:
            points += 5; detail["decline_rate"] = 5
    if account.manual_review_required:
        detail["layer4_manual_review_flag"] = "pending_admin_review"
    return points, detail


async def account_feedback_rates(
    db: AsyncSession, account_id: uuid.UUID, account_type: str
) -> tuple[float, float, int]:
    """(decline_rate, abuse_report_rate, completed_call_count)."""
    col = Call.business_account_id if account_type == "business" else Call.event_account_id
    rows = (
        await db.execute(
            select(CallResult.keypress, func.count(CallResult.id))
            .join(Call, Call.id == CallResult.call_id)
            .where(col == account_id)
            .group_by(CallResult.keypress)
        )
    ).all()
    counts = {k: c for k, c in rows}
    total = sum(counts.values())
    if total == 0:
        return 0.0, 0.0, 0
    return (
        counts.get(KeypressResult.declined, 0) / total,
        counts.get(KeypressResult.suspicious_report, 0) / total,
        total,
    )


async def evaluate(
    db: AsyncSession,
    *,
    account,
    account_type: str,
    message: str,
    kind: str,
    list_size: int,
    campaign_id: uuid.UUID | None = None,
    appointment_id: uuid.UUID | None = None,
    has_reward_opt_in_record: bool = False,
) -> RiskDecision:
    content: ContentCheck = evaluate_content(message, kind)

    # Structural hard blocks — immediate rejection, not a score.
    if content.hard_blocked:
        decision = RiskDecision(100, {"hard_block": content.hard_block_reasons},
                                "reject", content.hard_block_reasons)
        await _persist(db, account, account_type, campaign_id, appointment_id, decision)
        return decision
    if content.uses_reward_framing and not has_reward_opt_in_record:
        reasons = ["reward-style communication without stored prior specific opt-in"]
        decision = RiskDecision(100, {"hard_block": reasons}, "reject", reasons)
        await _persist(db, account, account_type, campaign_id, appointment_id, decision)
        return decision

    acct_pts, acct_detail = await _account_signals(db, account)
    vol_pts, vol_detail = await _volume_signals(db, account, account_type, list_size)
    beh_pts, beh_detail = await _behavior_signals(db, account, account_type)

    score = min(content.keyword_score + content.deviation_score + acct_pts + vol_pts + beh_pts, 100)
    breakdown = {
        "layer1_keywords": {"points": content.keyword_score, "hits": content.keyword_hits},
        "layer2_template_deviation": content.deviation_score,
        "layer3_account": acct_detail,
        "layer3_volume": vol_detail,
        "layer3_behavior": beh_detail,
    }

    if account.manual_review_required or score > settings.risk_hold_threshold:
        action = "hold"
    elif score >= settings.risk_monitor_threshold:
        action = "monitor"
    else:
        action = "proceed"

    decision = RiskDecision(score, breakdown, action, [])
    await _persist(db, account, account_type, campaign_id, appointment_id, decision)
    return decision


async def _persist(db, account, account_type, campaign_id, appointment_id, decision: RiskDecision):
    db.add(RiskScore(
        account_type=account_type,
        account_id=account.id,
        campaign_id=campaign_id,
        appointment_id=appointment_id,
        composite_score=decision.score,
        factor_breakdown=decision.breakdown,
        review_status=(
            ReviewStatus.pending if decision.action in ("hold", "reject") else ReviewStatus.cleared
        ),
    ))
    await db.commit()


async def run_feedback_loop_for_account(db: AsyncSession, account, account_type: str) -> bool:
    """Layer 4 — real post-call decline/press-9 report rate, not per-item
    content. Previously this immediately hard-blocked every future send the
    moment the threshold was crossed; now it instead queues the account for
    MANUAL admin review (a RiskScore row with no campaign_id/appointment_id,
    landing in the same queue campaign/appointment holds already use — see
    app/api/internal/risk_review.py) and emails the account holder, while
    anything already dispatched or already scheduled keeps running
    untouched. Only genuinely new work is blocked from this point on (see
    create_appointment / campaign_checkout / send_campaign's mid-dispatch
    pause). Returns True if the account is currently flagged (whether newly
    this run or already pending from before) — callers that only care about
    "should I skip this account's remaining work" can use the return value
    directly; the guard below prevents re-notifying on every 30-minute tick
    while a flag is still pending review."""
    decline, report, total = await account_feedback_rates(db, account.id, account_type)
    if total >= 10 and (
        decline > settings.decline_rate_threshold
        or report > settings.abuse_report_rate_threshold
    ):
        if not account.manual_review_required:
            account.manual_review_required = True
            db.add(RiskScore(
                account_type=account_type,
                account_id=account.id,
                campaign_id=None,
                appointment_id=None,
                composite_score=round(max(decline, report) * 100),
                factor_breakdown={
                    "layer4_behavioral": {
                        "decline_rate": round(decline, 3),
                        "abuse_report_rate": round(report, 3),
                        "total_calls": total,
                    },
                },
                review_status=ReviewStatus.pending,
            ))
            await db.commit()
            notifications.send_email(account.email, "account_restricted")
        return True
    return False