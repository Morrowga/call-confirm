"""System notifications — English only in Phase 1.

The account's preferred language is stored on the account row for future
localization; do NOT add multilingual templates here in Phase 1.

Email delivery is pluggable (SES/SMTP); Phase 1 ships a logging sender that is
trivially swappable via NOTIFICATION_BACKEND.

Every email is sent multipart: a real, branded HTML version (inline styles +
table layout, not modern CSS — most email clients, especially Outlook, only
reliably support this older subset) plus the original plain-text body as a
fallback for clients that block HTML or for accessibility tools. TEMPLATES
itself stays plain-text-only; the HTML version is generated automatically
from it at send time (see _body_to_html/_html_shell below) rather than
hand-writing a second copy of every message — one source of truth per
template, not two that could drift out of sync.
"""
import html as html_lib
import logging
import os
import smtplib
from email.message import EmailMessage

from app.core.config import settings
from app.services import twilio_service

log = logging.getLogger("notifications")

TEMPLATES = {
    "email_verify": ("Verify your email", "Confirm your account: {link}"),
    "password_reset": ("Reset your password", "Reset link (expires in 1 hour): {link}"),
    "payment_failed": (
        "Payment failed — calling paused",
        "Your latest payment failed and calling access is suspended. Update your card: {link}",
    ),
    "payment_restored": ("Payment received — access restored", "Your access has been restored. Thank you!"),
    "payment_receipt": ("Payment receipt", "Your subscription renewal succeeded. Amount: {amount}."),
    "voice_message_export": ("Your voice message recording", "A recording is attached before scheduled deletion."),
    "risk_hold_admin": ("Campaign held for review", "Risk score {score} — campaign {campaign_id} is awaiting manual review."),
    "account_restricted": (
        "Your account is under review",
        "We've noticed some unusual patterns in your recent call outcomes, so your account has "
        "been placed under manual review. Calls already in progress or already scheduled are not "
        "affected. New appointments and campaign sends are paused until our team reviews your "
        "account, which typically happens quickly. We'll email you again once this is resolved.",
    ),
    "account_review_cleared": (
        "Your account review is complete",
        "Your account has been reviewed and the restriction has been lifted. You can create new "
        "appointments and start new campaign sends again right away.",
    ),
    "sim_ready_for_payment": (
        "A new number is ready for you",
        "We've prepared a new number for your account. Complete the one-time $15 payment to "
        "activate it: {link}",
    ),
}


def _html_shell(title: str, body_html: str) -> str:
    """Table-based layout + inline styles only — no flexbox, no grid, no
    external/embedded stylesheet. Deliberate constraint of HTML email, not
    a stylistic choice: Outlook's rendering engine (Word, not a real
    browser engine) and many webmail clients strip <style> blocks and
    don't support modern layout CSS at all. This is the safe subset real
    transactional email providers actually use."""
    return f"""<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background-color:#f4f4f5;font-family:-apple-system,Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f5;padding:32px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;">
            <tr>
              <td style="background-color:#111111;padding:24px 32px;">
                <span style="color:#ffffff;font-size:18px;font-weight:600;">CallConfirm</span>
              </td>
            </tr>
            <tr>
              <td style="padding:32px;color:#27272a;font-size:15px;line-height:1.6;">
                <h1 style="font-size:20px;margin:0 0 16px 0;color:#111111;">{html_lib.escape(title)}</h1>
                {body_html}
              </td>
            </tr>
            <tr>
              <td style="padding:20px 32px;border-top:1px solid #e4e4e7;color:#71717a;font-size:12px;">
                CallConfirm — automated call confirmations. This is an automated message.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def _body_to_html(body: str, link: str | None = None) -> str:
    """Turns a plain-text template body into HTML paragraphs, and — when
    the template included a {link} — pulls the raw URL out of the running
    text and renders it as a real styled button instead of a bare pasted
    link sitting inline in a sentence."""
    text = body
    if link:
        text = text.replace(link, "").rstrip(": ").rstrip()
    escaped = html_lib.escape(text)
    paragraphs = "".join(
        f"<p style='margin:0 0 16px 0;'>{line}</p>" for line in escaped.split("\n") if line.strip()
    )
    button = ""
    if link:
        button = (
            f'<a href="{html_lib.escape(link)}" '
            'style="display:inline-block;background-color:#111111;color:#ffffff;'
            'text-decoration:none;padding:10px 22px;border-radius:6px;font-size:14px;'
            'margin-top:4px;">Open CallConfirm</a>'
        )
    return paragraphs + button


def send_email(to: str, template: str, attachments: list[tuple[str, bytes]] | None = None, **kwargs) -> None:
    subject, body = TEMPLATES[template]
    body = body.format(**kwargs)
    backend = os.environ.get("NOTIFICATION_BACKEND", "log")
    if backend == "smtp":
        msg = EmailMessage()
        msg["From"] = os.environ.get("SMTP_FROM", "no-reply@callconfirm.example")
        msg["To"] = to
        msg["Subject"] = subject
        # Plain-text first (fallback), then the HTML alternative — this
        # exact order is what makes EmailMessage build a correct
        # multipart/alternative part. Attachments added after this get
        # wrapped around the whole alternative part as multipart/mixed,
        # the standard correct structure for "HTML email + attachment."
        msg.set_content(body)
        msg.add_alternative(_html_shell(subject, _body_to_html(body, kwargs.get("link"))), subtype="html")
        for name, data in attachments or []:
            msg.add_attachment(data, maintype="audio", subtype="mpeg", filename=name)
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        log.info("EMAIL to=%s subject=%s body=%s", to, subject, body)


def send_otp_sms(to_number: str, otp: str) -> None:
    body = f"Your verification code is {otp}"
    backend = os.environ.get("NOTIFICATION_BACKEND", "log")
    if backend != "log" and settings.twilio_demo_number:
        twilio_service.send_sms(to_number, settings.twilio_demo_number, body)
    else:
        log.info("SMS OTP to=%s code=%s", to_number, otp)


def send_safety_net_sms(to_number: str, from_number: str) -> None:
    """Follow-up after reward/result-style calls: recipient can reply STOP or
    contact support if the call was unexpected."""
    body = (
        "If the recent call you received was unexpected, reply STOP to opt out "
        "or contact support@callconfirm.example."
    )
    backend = os.environ.get("NOTIFICATION_BACKEND", "log")
    if backend != "log":
        twilio_service.send_sms(to_number, from_number, body)
    else:
        log.info("SAFETY_NET_SMS to=%s from=%s body=%s", to_number, from_number, body)


# --- Local-currency display estimates --------------------------------------
# All actual charges are USD; these are display-only estimates by country.
COUNTRY_CURRENCY = {"US": "USD", "GB": "GBP", "DE": "EUR", "FR": "EUR", "VN": "VND", "AU": "AUD", "CA": "CAD", "JP": "JPY", "IN": "INR"}
STATIC_RATES = {"USD": 1.0, "GBP": 0.78, "EUR": 0.91, "VND": 25400.0, "AUD": 1.5, "CAD": 1.36, "JPY": 155.0, "INR": 84.0}


def local_estimate(amount_usd: float, country: str) -> dict:
    currency = COUNTRY_CURRENCY.get(country, "USD")
    rate = STATIC_RATES.get(currency, 1.0)
    return {
        "charge_amount_usd": round(amount_usd, 2),
        "local_estimate": {
            "currency": currency,
            "amount": round(amount_usd * rate, 2),
            "note": "Estimate only — the actual charge is processed in USD.",
        },
    }