"""Stripe integration. Keys are resolved per APP_ENV (never shared).

Rules implemented here:
  * Activation requires a $0 authorization check (SetupIntent card validation),
    no charge.
  * Business subscriptions: base fee (Panel $5 / API $10) + metered $0.0199/call,
    reported as calls complete; first 50 calls of month one excluded. This is
    SEPARATE from campaign billing below — a business account's subscription
    never covers campaign/event calls.
  * Voice add-on: access flips immediately; the billing item only changes on the
    next cycle (proration_behavior="none").
  * Campaigns (event calls): always a one-time, instant, non-refundable charge
    — billed as a real one-off Stripe Invoice (not a bare PaymentIntent), so
    it gets an actual Stripe-generated invoice_pdf, the exact same mechanism
    subscription invoices use (see get_invoice_pdf). Identical for business
    and event accounts; never folded into a business account's subscription
    or metered usage.
  * Rush tier: flat $0.05/call, cap 200 — deadline window verified server-side
    by the caller before this is invoked.
  * ALL state transitions driven by webhooks (handled in webhook router), never
    by the initial API response.
"""
import httpx
import stripe

from app.core.config import settings


class SubscriptionNotActiveError(Exception):
    """Raised when an operation requires an active subscription but the
    Stripe subscription is canceled/expired — callers should turn this into
    a clear 400 response rather than letting it bubble up as a raw
    stripe.error.InvalidRequestError (which Stripe returns for most
    modification attempts on a non-active subscription)."""


class CardValidationError(Exception):
    """Raised when a new card fails its $0 authorization check."""


class NotOwnedError(Exception):
    """Raised when a payment method / invoice ID doesn't actually belong to
    the given customer — same 'own data only' discipline used everywhere
    else in this codebase, just enforced against Stripe's own records
    instead of our database."""


def _client() -> None:
    stripe.api_key = settings.stripe_secret_key


def create_customer(email: str, name: str, account_type: str, account_id: str) -> str:
    _client()
    customer = stripe.Customer.create(
        email=email, name=name,
        metadata={"account_type": account_type, "account_id": account_id},
    )
    return customer.id


def update_customer_name(customer_id: str, name: str) -> None:
    """Keeps the Stripe Customer record's name in sync with our own
    account.name — without this, changing the business name in Settings
    only updates our database; Stripe's copy stays permanently stale
    (set once, at create_customer time, and never touched again), which
    is why invoice/receipt PDFs kept showing an old name after a rename.
    Invoices/receipts already generated before this call are historical
    documents and are NOT retroactively changed — only future ones."""
    _client()
    stripe.Customer.modify(customer_id, name=name)


def validate_card(customer_id: str, payment_method_id: str) -> bool:
    """$0 authorization check via SetupIntent — validates the card, charges nothing."""
    _client()
    stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)
    stripe.Customer.modify(
        customer_id, invoice_settings={"default_payment_method": payment_method_id}
    )
    intent = stripe.SetupIntent.create(
        customer=customer_id, payment_method=payment_method_id, confirm=True,
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
    )
    return intent.status == "succeeded"


def charge_event_sim_fee(customer_id: str, payment_method_id: str, amount_usd: float) -> str:
    """One-time, immediate, non-refundable SIM/number setup fee — charged
    at event-account ACTIVATION time (moved up from first-campaign-
    checkout time per updated design). This is a real charge, unlike
    validate_card's $0 SetupIntent above — confirmed synchronously with
    the same card just validated. Returns the PaymentIntent id.

    Number provisioning itself is not available yet (see
    settings.disable_number_auto_purchase) — this fee is charged
    regardless, per instruction, since Stripe billing works today even
    though actual SIM/number registration doesn't yet."""
    _client()
    intent = stripe.PaymentIntent.create(
        amount=int(round(amount_usd * 100)),
        currency="usd",
        customer=customer_id,
        payment_method=payment_method_id,
        confirm=True,
        off_session=False,
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
    )
    return intent.id


def charge_additional_sim_fee(customer_id: str, payment_method_id: str, amount_usd: float) -> str:
    """One-time, immediate, non-refundable fee for requesting an
    ADDITIONAL number beyond an account's first — deliberately priced
    higher (see settings.additional_sim_fee_usd, default $15) than the
    first-number cost, which is free/subsidized into the subscription
    margin for business accounts and the flat $3 activation fee for event
    accounts. This higher price is intentional friction: numbers are a
    limited resource ("we dont provide many sims to every company"), so
    this only ever fires when the account holder deliberately pays for a
    genuine additional SIM via Settings, after an admin has manually
    prepared/purchased the real number for them (see number_pool.py's
    admin approval flow) — never automatically. Applies identically to
    business and event accounts; this isn't an account-type-specific fee
    the way the $3 one is."""
    _client()
    intent = stripe.PaymentIntent.create(
        amount=int(round(amount_usd * 100)),
        currency="usd",
        customer=customer_id,
        payment_method=payment_method_id,
        confirm=True,
        off_session=False,
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
    )
    return intent.id


def create_business_subscription(customer_id: str, tier: str, voice_addon: bool) -> str:
    _client()
    items = [
        {"price": settings.stripe_price_api_base if tier == "api" else settings.stripe_price_panel_base},
        {"price": settings.stripe_price_metered_call},
    ]
    if voice_addon:
        items.append({"price": settings.stripe_price_voice_addon})
    sub = stripe.Subscription.create(
        customer=customer_id,
        items=items,
        payment_behavior="default_incomplete",
        payment_settings={"save_default_payment_method": "on_subscription"},
        # Stripe's 2025-03-31 "basil" API version removed the direct
        # `payment_intent` field from Invoice entirely (to support multiple
        # partial payments per invoice) — the PaymentIntent is now reached
        # through the invoice's `payments` array instead. Only expanded to
        # `.payment` here (4 levels — Stripe's hard cap); the PaymentIntent
        # itself is fetched in a separate retrieve() call below, since
        # expanding one level further ("...payment.payment_intent") exceeds
        # that cap and Stripe rejects the whole request. See:
        # https://docs.stripe.com/changelog/basil/2025-03-31/add-support-for-multiple-partial-payments-on-invoices
        expand=["latest_invoice.payments.data.payment"],
        # Smart Retries + final status "unpaid" (not canceled) are configured in
        # the Stripe dashboard billing settings; documented in README.
    )
    # default_incomplete leaves the subscription stuck "incomplete" until its
    # first invoice's PaymentIntent is explicitly confirmed in a SEPARATE
    # request — Stripe does not do this automatically. Without this step,
    # confirmed via live testing (three real subscriptions, all silently
    # died this exact way): the subscription sits unconfirmed for exactly 23
    # hours, then Stripe auto-voids the invoice and marks the subscription
    # incomplete_expired — a terminal, irreversible state — with NO explicit
    # cancel action from anyone. The card was already validated in
    # validate_card() above, which also set it as the customer's default
    # payment method, so this confirmation should succeed immediately for
    # any genuinely valid card.
    invoice = sub.latest_invoice
    if invoice and invoice.payments and invoice.payments.data:
        payment = invoice.payments.data[0].payment
        if payment and payment.type == "payment_intent" and payment.payment_intent:
            # payment.payment_intent is just the ID string here (not
            # expanded, per the 4-level cap above) — fetch the full object
            # separately to check its actual status before confirming.
            payment_intent = stripe.PaymentIntent.retrieve(payment.payment_intent)
            if payment_intent.status == "requires_confirmation":
                stripe.PaymentIntent.confirm(payment_intent.id)
    return sub.id


def list_payment_methods(customer_id: str) -> list[dict]:
    _client()
    customer = stripe.Customer.retrieve(customer_id)
    default_pm = customer.invoice_settings.default_payment_method
    methods = stripe.PaymentMethod.list(customer=customer_id, type="card")
    return [
        {
            "id": pm.id,
            "brand": pm.card.brand,
            "last4": pm.card.last4,
            "exp_month": pm.card.exp_month,
            "exp_year": pm.card.exp_year,
            "is_default": pm.id == default_pm,
        }
        for pm in methods.data
    ]


def add_payment_method(customer_id: str, payment_method_id: str, set_default: bool) -> None:
    """$0 authorization check via SetupIntent — same validation as the
    original activation card, but for adding an ADDITIONAL card to an
    already-active customer. Only overwrites the default if explicitly
    requested (validate_card always sets default, since it's always the
    first/only card at that point; here, adding a second card shouldn't
    silently displace the one already in use unless asked to)."""
    _client()
    stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)
    intent = stripe.SetupIntent.create(
        customer=customer_id, payment_method=payment_method_id, confirm=True,
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
    )
    if intent.status != "succeeded":
        stripe.PaymentMethod.detach(payment_method_id)
        raise CardValidationError(f"Card validation failed for customer {customer_id}")
    if set_default:
        stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": payment_method_id})


def _assert_owns_payment_method(customer_id: str, payment_method_id: str) -> None:
    pm = stripe.PaymentMethod.retrieve(payment_method_id)
    if pm.customer != customer_id:
        raise NotOwnedError(f"Payment method {payment_method_id} does not belong to customer {customer_id}")


def remove_payment_method(customer_id: str, payment_method_id: str) -> None:
    _client()
    _assert_owns_payment_method(customer_id, payment_method_id)
    stripe.PaymentMethod.detach(payment_method_id)


def set_default_payment_method(customer_id: str, payment_method_id: str) -> None:
    _client()
    _assert_owns_payment_method(customer_id, payment_method_id)
    stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": payment_method_id})


def list_invoices(customer_id: str, limit: int = 20) -> list[dict]:
    _client()
    invoices = stripe.Invoice.list(customer=customer_id, limit=limit)
    return [
        {
            "id": inv.id,
            "amount_due": inv.amount_due / 100,
            "currency": inv.currency,
            # draft/open/paid/void/uncollectible — "Pay now" should only ever
            # show for "open" (unpaid, awaiting payment / overdue), never for
            # a routine future/draft invoice or one already paid.
            "status": inv.status,
            "created": inv.created,
            "hosted_invoice_url": inv.hosted_invoice_url,
            "invoice_pdf": inv.invoice_pdf,
            "needs_payment": inv.status == "open",
        }
        for inv in invoices.data
    ]


def sum_paid_invoices(
    customer_id: str, date_from: "datetime | None" = None, date_to: "datetime | None" = None
) -> float:
    """Sum of actually-paid subscription invoice amounts within an
    optional date range — the subscription side of the Billing page's
    "total spend" figure. Only counts status='paid' invoices (not
    draft/open/void/uncollectible). Paginates through everything in
    range via Stripe's auto_paging_iter rather than a single capped
    page, since a date range could span more than one page of results."""
    _client()
    kwargs: dict = {"customer": customer_id, "status": "paid", "limit": 100}
    created_filter: dict = {}
    if date_from:
        created_filter["gte"] = int(date_from.timestamp())
    if date_to:
        created_filter["lte"] = int(date_to.timestamp())
    if created_filter:
        kwargs["created"] = created_filter
    total = 0.0
    for invoice in stripe.Invoice.list(**kwargs).auto_paging_iter():
        total += invoice.amount_paid / 100
    return round(total, 2)


def get_invoice_pdf(customer_id: str, invoice_id: str) -> bytes:
    """Fetches the actual PDF bytes server-side, so the panel can offer a
    real download instead of redirecting the owner to Stripe's hosted page —
    Stripe's invoice_pdf URL is fetched here (server-to-server, no CORS
    concern), and the caller streams these bytes back with its own
    Content-Disposition: attachment header."""
    _client()
    invoice = stripe.Invoice.retrieve(invoice_id)
    if invoice.customer != customer_id:
        raise NotOwnedError(f"Invoice {invoice_id} does not belong to customer {customer_id}")
    if not invoice.invoice_pdf:
        raise ValueError(f"Invoice {invoice_id} has no PDF available yet")
    resp = httpx.get(invoice.invoice_pdf, timeout=15.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def get_invoice_pdf_unchecked(invoice_id: str) -> bytes:
    """Same fetch as get_invoice_pdf, but without the customer-ownership
    check — for callers (like campaign receipts) that have already
    verified ownership through their own domain model (campaign -> event
    -> account), where re-deriving the Stripe customer_id purely to
    duplicate that check adds nothing."""
    _client()
    invoice = stripe.Invoice.retrieve(invoice_id)
    if not invoice.invoice_pdf:
        raise ValueError(f"Invoice {invoice_id} has no PDF available yet")
    resp = httpx.get(invoice.invoice_pdf, timeout=15.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def pay_invoice(customer_id: str, invoice_id: str) -> None:
    _client()
    invoice = stripe.Invoice.retrieve(invoice_id)
    if invoice.customer != customer_id:
        raise NotOwnedError(f"Invoice {invoice_id} does not belong to customer {customer_id}")
    stripe.Invoice.pay(invoice_id)


def report_call_usage(subscription_id: str, quantity: int = 1) -> None:
    """Report one completed billable call to the metered item."""
    _client()
    sub = stripe.Subscription.retrieve(subscription_id)
    metered_item = next(
        item for item in sub["items"]["data"]
        if item["price"]["id"] == settings.stripe_price_metered_call
    )
    stripe.SubscriptionItem.create_usage_record(
        metered_item["id"], quantity=quantity, action="increment"
    )


def get_current_period_end(subscription_id: str) -> int | None:
    """Unix timestamp of the next billing date, for display on the Billing page.
    Returns None on any lookup failure rather than raising — this is
    informational display data, not something that should ever block the
    page from rendering."""
    _client()
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        return sub.get("current_period_end")
    except Exception:
        return None


def set_voice_addon(subscription_id: str, enabled: bool) -> None:
    """Billing change applies next cycle — no proration. (Feature access is
    toggled immediately on the account row by the caller.)"""
    _client()
    sub = stripe.Subscription.retrieve(subscription_id)
    # Stripe only allows modifying cancellation_details/metadata on a
    # canceled subscription — attempting the item add/remove below on one
    # raises a raw StripeError that would otherwise crash as an unhandled
    # 500. Surface this as a clear, catchable condition instead so the
    # caller can turn it into an actionable message.
    if sub["status"] in ("canceled", "incomplete_expired"):
        raise SubscriptionNotActiveError(
            f"Subscription {subscription_id} is {sub['status']} — reactivate the plan before changing add-ons."
        )
    addon_item = next(
        (i for i in sub["items"]["data"] if i["price"]["id"] == settings.stripe_price_voice_addon),
        None,
    )
    if enabled and addon_item is None:
        stripe.Subscription.modify(
            subscription_id,
            items=[{"price": settings.stripe_price_voice_addon}],
            proration_behavior="none",
        )
    elif not enabled and addon_item is not None:
        stripe.SubscriptionItem.delete(addon_item["id"], proration_behavior="none")


def change_subscription_tier(subscription_id: str, new_tier: str) -> None:
    """Panel <-> API base-fee swap. Same rule as the voice add-on: feature
    access (caller flips subscription_tier on the account row) is immediate;
    the billing item swap here uses proration_behavior="none", so the price
    change itself only takes effect on the next invoice — no prorated
    mid-cycle charge or credit. Kept consistent with set_voice_addon above
    rather than introducing a second billing-change behavior to explain."""
    _client()
    new_price = settings.stripe_price_api_base if new_tier == "api" else settings.stripe_price_panel_base
    old_price = settings.stripe_price_panel_base if new_tier == "api" else settings.stripe_price_api_base
    sub = stripe.Subscription.retrieve(subscription_id)
    if sub["status"] in ("canceled", "incomplete_expired"):
        raise SubscriptionNotActiveError(
            f"Subscription {subscription_id} is {sub['status']} — reactivate the plan before changing tier."
        )
    base_item = next(
        (i for i in sub["items"]["data"] if i["price"]["id"] == old_price), None
    )
    if base_item is None:
        raise RuntimeError(f"Current base price item not found on subscription {subscription_id}")
    stripe.Subscription.modify(
        subscription_id,
        items=[{"id": base_item["id"], "price": new_price}],
        proration_behavior="none",
    )


def campaign_price_usd(call_count: int, rush: bool = False) -> float:
    """Marginal pricing: first 500 @ $0.02, beyond @ $0.05. Rush: flat $0.05."""
    if rush:
        return round(call_count * settings.rush_tier_price, 2)
    t1 = min(call_count, settings.event_tier1_calls) * settings.event_tier1_price
    t2 = max(0, call_count - settings.event_tier1_calls) * settings.event_tier2_price
    return round(t1 + t2, 2)


def create_campaign_invoice_payment(
    customer_id: str, call_count: int, campaign_id: str, rush: bool = False, sim_fee_usd: float = 0.0,
    force_price_usd: float | None = None,
) -> tuple[str, str, str, float, float]:
    """Bills a campaign as a real, one-off Stripe Invoice — not a bare
    PaymentIntent — so it gets Stripe's own invoice_pdf, the exact same
    downloadable-PDF mechanism the subscription invoices above already
    use (get_invoice_pdf). Identical for business and event accounts;
    never folded into a subscription.

    Mechanics: create a draft Invoice, attach one InvoiceItem for the
    total amount, finalize it (this is what causes Stripe to generate
    both the invoice_pdf AND, since collection_method is the default
    "charge_automatically", an underlying PaymentIntent — reached via the
    invoice's `payments` array on the "basil"+ API version, same pattern
    already used in create_business_subscription above). The campaign
    metadata is then written onto that PaymentIntent directly (Stripe
    doesn't propagate Invoice metadata onto the auto-created PaymentIntent
    for us), so the payment_intent.succeeded webhook handler needs no
    changes at all — it already reads campaign_id/rush/sim_fee_included
    off the PaymentIntent's own metadata.

    Returns (invoice_id, payment_intent_id, client_secret, amount_usd,
    sim_fee_usd_included). The frontend confirms payment with
    confirmCardPayment(client_secret, ...) exactly as before — nothing
    changes on that side.

    force_price_usd, when set, overrides real per-call pricing entirely
    with a flat testing amount (see settings.force_test_campaign_price_usd).
    """
    _client()
    call_amount = force_price_usd if force_price_usd is not None else campaign_price_usd(call_count, rush=rush)
    amount = round(call_amount + sim_fee_usd, 2)

    invoice = stripe.Invoice.create(
        customer=customer_id,
        collection_method="charge_automatically",
        auto_advance=False,  # we finalize explicitly below, right after adding the line item
        # Explicit — without this, Stripe defaults the Invoice's currency
        # to whatever currency is already locked on the customer object
        # (set by an earlier charge/subscription in a different currency,
        # e.g. jpy), while the InvoiceItem below always specifies usd —
        # that mismatch is what raised "You cannot combine currencies on
        # a single invoice." All campaign charges are USD; this keeps the
        # Invoice and its InvoiceItem always in agreement regardless of
        # the customer's prior currency history.
        currency="usd",
    )
    stripe.InvoiceItem.create(
        customer=customer_id,
        invoice=invoice.id,
        amount=int(round(amount * 100)),
        currency="usd",
        description=f"Campaign call batch ({call_count} calls)",
    )
    finalized = stripe.Invoice.finalize_invoice(invoice.id, expand=["payments.data.payment"])

    payment_intent_id = None
    if finalized.payments and finalized.payments.data:
        payment = finalized.payments.data[0].payment
        if payment and payment.type == "payment_intent" and payment.payment_intent:
            payment_intent_id = payment.payment_intent
    if not payment_intent_id:
        raise RuntimeError(f"Invoice {invoice.id} finalized with no PaymentIntent — cannot collect payment")

    # Metadata is set on the invoice at creation time only informationally;
    # the webhook reads it off the PaymentIntent, so it's written here too.
    stripe.PaymentIntent.modify(
        payment_intent_id,
        metadata={
            "campaign_id": campaign_id,
            "rush": str(rush).lower(),
            "sim_fee_included": str(sim_fee_usd > 0).lower(),
        },
    )
    payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    return invoice.id, payment_intent_id, payment_intent.client_secret, amount, sim_fee_usd


def construct_webhook_event(payload: bytes, sig_header: str):
    _client()
    return stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)