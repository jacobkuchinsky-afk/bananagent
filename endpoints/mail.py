"""Sending email from BananaGent's own Gmail account.

Every message goes out from EMAIL_ADDRESS and nowhere else — the sender isn't
an argument, so the model can't send as anyone else — and every one is marked
as BananaGent's: the display name says so, and a footer the model can't remove
says an AI assistant sent it. Gmail's SMTP server takes an app password rather
than the account password; it lives in secrets.env as GMAIL_APP_PASSWORD (see
the README). Gmail files a copy of each message in the account's Sent folder.
"""

import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, getaddresses

import config
from .registry import MAJOR, endpoint, param

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def _app_password():
    try:
        with open(os.path.join(APP, config.SECRETS_FILE), encoding="utf-8") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key.strip() == "GMAIL_APP_PASSWORD":
                    # Google shows it in four groups of four; the spaces don't count.
                    return value.strip().strip('"').strip("'").replace(" ", "")
    except OSError:
        pass
    return ""


def _recipients(field, text):
    found = [addr for _, addr in getaddresses([text or ""]) if addr]
    bad = [a for a in found if not _ADDRESS.match(a)]
    if bad:
        raise ValueError(f"Not an email address in {field}: {', '.join(bad)}")
    return found


@endpoint(
    tier=MAJOR,
    description=(
        f"Sends an email to any address from your own Gmail, {config.EMAIL_ADDRESS}, "
        "signed as you, BananaGent, with a footer saying an AI assistant sent it. "
        "Write the body as plain text and don't sign it yourself — the signature is "
        "added. Only send what the user asked you to send, to who they asked."
    ),
    params={
        "to": param("string", "Recipient address; several separated by commas.", required=True),
        "subject": param("string", "Subject line.", required=True),
        "body": param("string", "The message, plain text.", required=True),
        "cc": param("string", "Addresses to copy, separated by commas."),
    },
    returns="Who it went to, or why it couldn't be sent.",
)
def send_email(to, subject, body, cc=None):
    to_list = _recipients("to", to)
    cc_list = _recipients("cc", cc)
    if not to_list:
        raise ValueError("Give at least one address in to.")
    if not str(subject).strip():
        raise ValueError("Give it a subject.")
    password = _app_password()
    if not password:
        raise RuntimeError(
            f"No Gmail app password set up for {config.EMAIL_ADDRESS}. The user has to "
            "add GMAIL_APP_PASSWORD to secrets.env (see the README).")

    msg = EmailMessage()
    msg["From"] = formataddr((config.EMAIL_NAME, config.EMAIL_ADDRESS))
    msg["Reply-To"] = config.EMAIL_ADDRESS
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = str(subject).strip()
    msg["X-Mailer"] = "BananaGent"
    msg.set_content(f"{str(body).rstrip()}\n\n{config.EMAIL_FOOTER}\n")

    with smtplib.SMTP_SSL(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT,
                          timeout=config.EMAIL_TIMEOUT) as smtp:
        try:
            smtp.login(config.EMAIL_ADDRESS, password)
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError(
                f"Gmail refused the login for {config.EMAIL_ADDRESS}. The app password in "
                "secrets.env is wrong or was revoked — the user has to make a new one.")
        refused = smtp.send_message(msg)

    sent = [a for a in to_list + cc_list if a not in refused]
    if not sent:
        raise RuntimeError(f"Gmail refused every recipient: {', '.join(refused)}")
    note = f" Refused: {', '.join(refused)}." if refused else ""
    return f"Sent \"{msg['Subject']}\" from {config.EMAIL_ADDRESS} to {', '.join(sent)}.{note}"
