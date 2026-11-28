"""Builds the survey email and hands it to a delivery provider. No provider is chosen yet."""
import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from . import config


class ProviderNotConfigured(RuntimeError):
    pass


def survey_link(token) -> str:
    return f"{config.PUBLIC_BASE_URL}/r/{token}"


def unsubscribe_link(token) -> str:
    return f"{config.PUBLIC_BASE_URL}/u/{token}"


def build_message(*, token, to_email, first_name, site, visit_date_text) -> EmailMessage:
    """One survey email. Sent from the single feedback@ address, with the site named in the display name."""
    name = (first_name or "").strip().title()
    hello = f"Hi {name}," if name else "Hi there,"
    link, unsub = survey_link(token), unsubscribe_link(token)
    site_name = site["name"]
    subject = f"{name + ', how' if name else 'How'} was your visit to Tap & Tandoor {site_name}?"

    text = (
        f"{hello}\n\n"
        f"Thank you for dining with us at Tap & Tandoor {site_name} on {visit_date_text}.\n\n"
        "Would you tell us how it went? It takes about a minute, and you can rate the dishes you had.\n\n"
        f"Share your feedback: {link}\n\n"
        "This link is just for you and works once.\n\n"
        f"Tap & Tandoor {site_name}, {site['address']}\n"
        f"You are receiving this because you dined with us. Stop these emails: {unsub}\n"
    )
    safe = html.escape
    body = f"""<!doctype html><html lang="en"><body style="margin:0;background:#f6f4ee;font-family:Arial,Helvetica,sans-serif;color:#17150f">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden">
<tr><td style="background:#141414;padding:20px 24px"><img src="{config.PUBLIC_BASE_URL}/assets/logo.png" width="190" alt="Tap &amp; Tandoor" style="display:block;border:0;color:#ffd503;font-weight:bold"></td></tr>
<tr><td style="padding:28px 24px 8px;font-size:18px;line-height:1.5">
<p style="margin:0 0 14px;font-size:22px;font-weight:bold">{safe(hello)}</p>
<p style="margin:0 0 14px">Thank you for dining with us at Tap &amp; Tandoor {safe(site_name)} on {safe(visit_date_text)}.</p>
<p style="margin:0 0 22px">Would you tell us how it went? It takes about a minute, and you can rate the dishes you had.</p>
<p style="margin:0 0 22px"><a href="{link}" style="display:inline-block;background:#ffd503;color:#17150f;font-weight:bold;text-decoration:none;padding:15px 28px;border-radius:12px">Tell us how it was</a></p>
<p style="margin:0 0 8px;font-size:14px;color:#5d5a50">This link is just for you and works once.</p>
</td></tr>
<tr><td style="padding:18px 24px 26px;font-size:12px;line-height:1.5;color:#7a766a;border-top:1px solid #eee">
Tap &amp; Tandoor {safe(site_name)}, {safe(site['address'])}<br>
You are receiving this because you dined with us. <a href="{unsub}" style="color:#7a766a">Stop these emails</a>.
</td></tr></table></td></tr></table></body></html>"""

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((f"Tap & Tandoor {site_name}", config.SENDER_ADDRESS))
    msg["Reply-To"] = config.SENDER_ADDRESS
    msg["To"] = to_email
    msg["List-Unsubscribe"] = f"<{unsub}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    return msg


class DryRunProvider:
    """Writes each email to previews/ and sends nothing."""
    name = "dry-run"

    def __init__(self, out_dir):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)

    def send(self, msg: EmailMessage, key: str) -> str:
        with open(os.path.join(self.out_dir, f"{key}.eml"), "wb") as fh:
            fh.write(bytes(msg))
        html_part = msg.get_body(preferencelist=("html",))
        with open(os.path.join(self.out_dir, f"{key}.html"), "w", encoding="utf-8") as fh:
            fh.write(html_part.get_content())
        return f"dry-run:{key}"


class SmtpProvider:
    """Works with any SMTP relay (Amazon SES, Postmark, Google Workspace, and so on).
    Credentials come from SMTP_HOST, SMTP_PORT, SMTP_USER and SMTP_PASSWORD and are never logged."""
    name = "smtp"

    def __init__(self):
        self.host = os.environ.get("SMTP_HOST", "")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        if not (self.host and self.user and self.password):
            raise ProviderNotConfigured("SMTP_HOST, SMTP_USER and SMTP_PASSWORD must all be set")

    def send(self, msg: EmailMessage, key: str) -> str:
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.user, self.password)
            smtp.send_message(msg, from_addr=config.SENDER_ADDRESS)
        return f"smtp:{key}"


def get_provider(live: bool, out_dir: str):
    if not live:
        return DryRunProvider(out_dir)
    choice = os.environ.get("FEEDBACK_PROVIDER", "").strip().lower()
    if choice == "smtp":
        return SmtpProvider()
    raise ProviderNotConfigured("FEEDBACK_PROVIDER is not set to a supported provider (supported: smtp)")
