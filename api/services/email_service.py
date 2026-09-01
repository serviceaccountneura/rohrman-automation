"""Outbound email — currently just user invites.

CONFIGURATION
    Set smtp_user and smtp_password in .env. The defaults point at Gmail, which
    is free and needs no account beyond one you already have:

        1. turn on 2-step verification for the sending Google account
        2. create an App Password (Google Account -> Security -> App passwords)
        3. SMTP_USER=you@gmail.com, SMTP_PASSWORD=<the 16-character app password>

    A normal Google password will NOT work for SMTP -- Google rejects it, which
    is the usual reason a first attempt fails with "Username and Password not
    accepted".

    Gmail's free tier sends roughly 500 messages a day. Invites are occasional,
    so that is not a limit worth designing around.

SENDING IS OPTIONAL
    With no smtp_user configured, `send_invite()` reports that it did not send
    rather than raising. The caller still has the invite link and can pass it on
    by hand, so an unconfigured mail server never blocks someone from being
    added. The API says which happened so the UI can show the link when the
    email did not go.

FAILURES ARE NOT FATAL
    A refused or timed-out send is reported the same way. The invite already
    exists in the database at that point, and throwing away a valid invite
    because a mail server hiccuped would be worse than handing back a link.
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from api.config import settings

# Gmail's implicit-SSL port. Anything else is treated as STARTTLS.
_SSL_PORT = 465
_TIMEOUT_SECONDS = 20


@dataclass
class SendResult:
    """Whether the message went, and why not when it did not."""

    sent: bool
    detail: str = ""


def is_configured() -> bool:
    """True when there are credentials to send with."""
    return bool(settings.smtp_user and settings.smtp_password)


def _from_address() -> str:
    # Gmail rewrites (or rejects) a From it does not own, so default to the
    # authenticated account rather than inventing an address.
    return settings.smtp_from_email or settings.smtp_user


def _send(to_email: str, subject: str, text_body: str, html_body: str) -> SendResult:
    if not is_configured():
        return SendResult(False, "Email is not configured (SMTP_USER is unset).")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.smtp_from_name, _from_address()))
    message["To"] = to_email
    # Plain text first, HTML as the alternative: a client that cannot render
    # HTML still shows a usable message with the link in it.
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        if settings.smtp_port == _SSL_PORT:
            server = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT_SECONDS
            )
        else:
            server = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT_SECONDS
            )
        with server:
            if settings.smtp_port != _SSL_PORT:
                server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError:
        # By far the most common failure, and the fix is specific enough to say.
        return SendResult(
            False,
            "The mail server rejected the credentials. Gmail needs an App "
            "Password, not the account password.",
        )
    except Exception as e:  # noqa: BLE001 - a bad send must not lose the invite
        return SendResult(False, f"Could not send the email: {e}")

    print(f"[MAIL] invite sent to {to_email}")
    return SendResult(True)


def send_invite(to_email: str, invite_url: str, role_label: str, invited_by: str) -> SendResult:
    """Invite someone to set up their account.

    The link is the whole point of the message, so it appears as readable text
    as well as a button -- forwarded mail and locked-down clients routinely
    strip the styling, and a button nobody can click is worse than a URL.
    """
    subject = "You have been invited to Rohrman Invoice Automation"

    text_body = (
        f"{invited_by} has invited you to Rohrman Invoice Automation "
        f"as {role_label}.\n\n"
        f"Set your password and finish setting up your account here:\n\n"
        f"{invite_url}\n\n"
        f"This link works once and expires in 24 hours.\n\n"
        f"If you were not expecting this invitation, you can ignore this email.\n"
    )

    html_body = f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#f5f7fa;
               font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
               color:#16202b;line-height:1.6;">
    <div style="max-width:520px;margin:0 auto;background:#ffffff;border:1px solid #d5dce5;
                border-radius:8px;padding:32px;">
      <h1 style="margin:0 0 16px;font-size:20px;font-weight:600;">
        You have been invited
      </h1>
      <p style="margin:0 0 16px;">
        {invited_by} has invited you to <strong>Rohrman Invoice Automation</strong>
        as {role_label}.
      </p>
      <p style="margin:0 0 24px;">
        Set your password to finish setting up your account.
      </p>
      <p style="margin:0 0 24px;">
        <a href="{invite_url}"
           style="display:inline-block;background:#2b5c8a;color:#ffffff;
                  text-decoration:none;padding:12px 22px;border-radius:6px;
                  font-weight:600;">Set your password</a>
      </p>
      <p style="margin:0 0 8px;font-size:13px;color:#4a5765;">
        Or paste this link into your browser:
      </p>
      <p style="margin:0 0 24px;font-size:13px;word-break:break-all;">
        <a href="{invite_url}" style="color:#2b5c8a;">{invite_url}</a>
      </p>
      <p style="margin:0;font-size:13px;color:#6f7c8b;border-top:1px solid #d5dce5;
                padding-top:16px;">
        This link works once and expires in 24 hours. If you were not expecting
        this invitation, you can ignore this email.
      </p>
    </div>
  </body>
</html>"""

    return _send(to_email, subject, text_body, html_body)


def send_password_reset(to_email: str, reset_url: str) -> SendResult:
    """A link to choose a new password.

    Says explicitly that ignoring the mail leaves the password unchanged. People
    who did not ask for this need to know that doing nothing is safe -- without
    that line the message reads like a breach notification.

    The link is shown as text as well as a button, for the same reason as the
    invite: forwarded mail and locked-down clients strip styling.
    """
    subject = "Reset your Rohrman Invoice Automation password"

    text_body = (
        "Someone asked to reset the password for this account.\n\n"
        f"Choose a new password here:\n\n{reset_url}\n\n"
        "This link works once and expires in one hour.\n\n"
        "If you did not ask for this, ignore this email -- your password will "
        "not change.\n"
    )

    html_body = f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#f5f7fa;
               font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
               color:#16202b;line-height:1.6;">
    <div style="max-width:520px;margin:0 auto;background:#ffffff;border:1px solid #d5dce5;
                border-radius:8px;padding:32px;">
      <h1 style="margin:0 0 16px;font-size:20px;font-weight:600;">
        Reset your password
      </h1>
      <p style="margin:0 0 24px;">
        Someone asked to reset the password for this account.
      </p>
      <p style="margin:0 0 24px;">
        <a href="{reset_url}"
           style="display:inline-block;background:#2b5c8a;color:#ffffff;
                  text-decoration:none;padding:12px 22px;border-radius:6px;
                  font-weight:600;">Choose a new password</a>
      </p>
      <p style="margin:0 0 8px;font-size:13px;color:#4a5765;">
        Or paste this link into your browser:
      </p>
      <p style="margin:0 0 24px;font-size:13px;word-break:break-all;">
        <a href="{reset_url}" style="color:#2b5c8a;">{reset_url}</a>
      </p>
      <p style="margin:0;font-size:13px;color:#4a5765;">
        This link works once and expires in one hour. If you did not ask for
        this, ignore this email &mdash; your password will not change.
      </p>
    </div>
  </body>
</html>
"""
    return _send(to_email, subject, text_body, html_body)
