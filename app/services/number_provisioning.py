"""On-demand, per-country number provisioning.

No pre-purchased pools. On subscription activation:
  1. reuse a released number for the account's country if one exists;
  2. otherwise purchase a new one from Twilio in real time — unless the
     auto-purchase cap (global or per-country) is reached, in which case the
     signup routes into a manual-approval queue for the platform admin.

Released numbers go back to `available` for their country; they are never
deleted, and a number is never assigned to two accounts at once.

SIM/number CHANGES (change_number, below) are a separate concept from full
release: changing a number never returns the old one to the shared pool for
reuse by another account — every number an account has ever used stays
permanently linked to it (assigned_account_type/assigned_account_id never
cleared), as a historical record, so past call recipients' caller ID history
stays meaningful and no future account could ever be assigned a number with
someone else's calling history behind it. `is_active` tracks which single
row is the account's CURRENT number for dispatch; changing numbers flips it
off on the old row and on for the new one, nothing else.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import NumberStatus, PhoneNumberPool, PlatformConfig, ProvisioningRequest
from app.services import twilio_service


async def _auto_purchase_caps(db: AsyncSession) -> dict[str, int]:
    """Flat {country: cap} dict — no global cap. A country not present
    here is already fully blocked (see _acquire_number below), so a
    cross-country global ceiling on top of that was redundant complexity,
    not real protection: every country you actually support already has
    its own explicit, deliberately-chosen limit."""
    row = await db.get(PlatformConfig, "number_auto_purchase_caps")
    if row:
        return {k: int(v) for k, v in row.value.items()}
    return {}


async def _acquire_number(
    db: AsyncSession, account_type: str, account_id: uuid.UUID, country: str
) -> PhoneNumberPool | None:
    """The actual acquisition logic (reuse released / check caps / real
    purchase), shared by both assign_number (first-ever number) and
    change_number (getting a new one to switch to). Always creates or
    claims a row with is_active=True — callers are responsible for
    deactivating whatever the account's previous active row was, if any."""
    now = datetime.now(timezone.utc)

    # 1) Reuse a released number for this country (row-locked to avoid races).
    # Note: only rows genuinely returned via a FULL release (status =
    # available) are reusable here — a row that's merely inactive for its
    # own account (is_active = False) is NOT available for anyone else;
    # that's the whole point of the distinction (see module docstring).
    reusable = (
        await db.execute(
            select(PhoneNumberPool)
            .where(PhoneNumberPool.country == country,
                   PhoneNumberPool.status == NumberStatus.available)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
    ).scalar_one_or_none()
    if reusable:
        reusable.status = NumberStatus.assigned
        reusable.assigned_account_type = account_type
        reusable.assigned_account_id = account_id
        reusable.assigned_at = now
        reusable.is_active = True
        await db.commit()
        return reusable

    # 2) Check auto-purchase cap before buying. A country with NO entry
    # here is NOT supported at all — always routes to manual review, never
    # auto-purchases. Explicitly configuring a country here (see the admin
    # Number Pool page's Auto-purchase caps editor) is what makes it
    # purchasable AND what makes it selectable at registration (see the
    # public /meta/registration-options endpoint, which reads these same
    # keys). No global/cross-country cap — each supported country's own
    # limit is the only ceiling that applies.
    per_country = await _auto_purchase_caps(db)
    country_cap = per_country.get(country)
    if country_cap is None:
        db.add(ProvisioningRequest(account_type=account_type, account_id=account_id, country=country))
        await db.commit()
        return None
    country_total = (
        await db.execute(
            select(func.count(PhoneNumberPool.id)).where(PhoneNumberPool.country == country)
        )
    ).scalar_one()
    if country_total >= country_cap:
        db.add(ProvisioningRequest(account_type=account_type, account_id=account_id, country=country))
        await db.commit()
        return None

    # 3) Real-time purchase from Twilio. If Twilio has no inventory for this
    # country (e.g. regulatory restrictions requiring local registration —
    # observed with Vietnam), fall back to the same manual-approval path used
    # for the auto-purchase-cap case above, instead of crashing.
    # Also skipped entirely (same fallback) while
    # settings.disable_number_auto_purchase is on — lets testing continue
    # without depending on live Twilio inventory, without touching this
    # purchase logic itself.
    if settings.disable_number_auto_purchase:
        db.add(ProvisioningRequest(account_type=account_type, account_id=account_id, country=country))
        await db.commit()
        return None
    try:
        number, sid = twilio_service.purchase_number(country)
    except RuntimeError:
        db.add(ProvisioningRequest(account_type=account_type, account_id=account_id, country=country))
        await db.commit()
        return None
    row = PhoneNumberPool(
        number=number, country=country, twilio_sid=sid,
        status=NumberStatus.assigned,
        assigned_account_type=account_type, assigned_account_id=account_id, assigned_at=now,
        is_active=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def assign_number(
    db: AsyncSession, account_type: str, account_id: uuid.UUID, country: str
) -> PhoneNumberPool | None:
    """First-ever number for an account (activation). Returns the
    assigned number, or None if routed to manual approval."""
    return await _acquire_number(db, account_type, account_id, country)


async def change_number(
    db: AsyncSession, account_type: str, account_id: uuid.UUID, country: str
) -> PhoneNumberPool | None:
    """Requests a NEW number for an account that already has one — e.g. a
    "change my SIM" self-service action. The account's current active
    number is deactivated (is_active=False) but stays permanently linked
    to the account as a historical record; it is NEVER returned to the
    shared pool for another account to be assigned (that would risk a
    future account inheriting a number with someone else's call/caller-ID
    history behind it — see module docstring).

    Returns the new number, or None if routed to manual approval (same
    fallback behavior as assign_number — the account keeps its old
    number, still active, until a new one is actually available; this
    function does NOT deactivate the old row unless a new one was
    actually acquired)."""
    new_row = await _acquire_number(db, account_type, account_id, country)
    if new_row is None:
        return None  # old number stays active — nothing to switch to yet

    current = (
        await db.execute(
            select(PhoneNumberPool).where(
                PhoneNumberPool.assigned_account_type == account_type,
                PhoneNumberPool.assigned_account_id == account_id,
                PhoneNumberPool.is_active.is_(True),
                PhoneNumberPool.id != new_row.id,
            )
        )
    ).scalars().all()
    for row in current:
        row.is_active = False
    await db.commit()
    return new_row


async def release_number(db: AsyncSession, account_type: str, account_id: uuid.UUID) -> None:
    """Full release — e.g. account cancellation. Genuinely returns the
    number to the shared pool (status=available) for reuse by ANY future
    account. This is intentionally different from change_number above:
    cancellation means this account is done with the number entirely, so
    the caller-ID-history concern that keeps change_number from doing
    this no longer applies the same way — an account that no longer
    exists has no ongoing relationship for a past recipient to be
    confused about."""
    row = (
        await db.execute(
            select(PhoneNumberPool).where(
                PhoneNumberPool.assigned_account_type == account_type,
                PhoneNumberPool.assigned_account_id == account_id,
                PhoneNumberPool.status == NumberStatus.assigned,
                PhoneNumberPool.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row:
        row.status = NumberStatus.available
        row.assigned_account_type = None
        row.assigned_account_id = None
        row.assigned_at = None
        row.is_active = False
        await db.commit()


async def get_assigned_number(
    db: AsyncSession, account_type: str, account_id: uuid.UUID
) -> str | None:
    """The account's CURRENT number for dispatch — filtered on is_active,
    not just status, since an account can now have multiple historical
    rows (see change_number) and exactly one of them should ever be used
    for a real outbound call."""
    row = (
        await db.execute(
            select(PhoneNumberPool.number).where(
                PhoneNumberPool.assigned_account_type == account_type,
                PhoneNumberPool.assigned_account_id == account_id,
                PhoneNumberPool.status == NumberStatus.assigned,
                PhoneNumberPool.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    return row