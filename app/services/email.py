"""Transactional email service (SMTP) - welcome emails etc."""
import logging
import smtplib
from email.message import EmailMessage
from app.core.config import settings

logger = logging.getLogger(__name__)


def send_email(to_email: str, subject: str, text_body: str, html_body: str | None = None):
    """Send an email via SMTP. No-op (with a log line) if SMTP is not configured.

    Never raises: callers run this in a background task and a mail failure
    must not affect the request that triggered it.
    """
    if not settings.SMTP_HOST:
        logger.info("SMTP_HOST not set; skipping email %r to %s", subject, to_email)
        return

    from_email = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{from_email}>"
    msg["To"] = to_email
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        if settings.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20)
            server.starttls()
        with server:
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
            server.send_message(msg)
        logger.info("Sent email %r to %s", subject, to_email)
    except Exception:
        logger.exception("Failed to send email %r to %s", subject, to_email)


def send_welcome_email(name: str, to_email: str):
    app_url = settings.FRONTEND_URL.rstrip("/")
    first_name = name.strip().split(" ")[0] if name.strip() else "there"

    text_body = f"""Hi {first_name},

Welcome to SocialPilot! Your account is ready.

Here's how to get started:
1. Connect your social accounts: {app_url}/accounts
2. Create your first post: {app_url}/composer
3. Or let the AI plan a whole campaign for you: {app_url}/campaigns

Log in any time at {app_url}/login

Happy posting!
The SocialPilot Team
"""

    html_body = f"""\
<div style="font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px; color: #1f2937;">
  <h1 style="font-size: 22px; margin: 0 0 16px;">Welcome to SocialPilot 🎉</h1>
  <p>Hi {first_name},</p>
  <p>Your account is ready. Here's how to get started:</p>
  <ol style="padding-left: 20px; line-height: 1.8;">
    <li><a href="{app_url}/accounts" style="color: #4f46e5;">Connect your social accounts</a></li>
    <li><a href="{app_url}/composer" style="color: #4f46e5;">Create your first post</a></li>
    <li><a href="{app_url}/campaigns" style="color: #4f46e5;">Let the AI plan a whole campaign</a></li>
  </ol>
  <p style="margin: 24px 0;">
    <a href="{app_url}/login" style="background: #4f46e5; color: #ffffff; text-decoration: none; padding: 10px 20px; border-radius: 8px; display: inline-block;">Open SocialPilot</a>
  </p>
  <p style="color: #6b7280; font-size: 13px;">Happy posting!<br/>The SocialPilot Team</p>
</div>
"""

    send_email(to_email, "Welcome to SocialPilot 🎉", text_body, html_body)
