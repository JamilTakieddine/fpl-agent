"""Email: the gameweek summary after each save, and alerts when a human is needed (D36).

Gmail over SMTP with an app password: no extra service to sign up for. The address and the app
password come from the environment (FPL_EMAIL, FPL_EMAIL_APP_PASSWORD); in the cloud job Cloud
Run fills them from Secret Manager, so neither is ever in the code or the public repo. Without
them (local runs), messages are printed instead.
"""

from __future__ import annotations

import os
import smtplib
from collections.abc import Mapping
from email.message import EmailMessage
from typing import Protocol

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SMTP over TLS
TIMEOUT_S = 30


class Mailer(Protocol):
    def send(self, subject: str, body: str) -> None: ...


class GmailMailer:
    """Sends from the account to itself."""

    def __init__(self, address: str, app_password: str) -> None:
        self.address = address
        self.app_password = app_password

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = msg["To"] = self.address
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT_S) as smtp:
            smtp.login(self.address, self.app_password)
            smtp.send_message(msg)


class PrintMailer:
    def send(self, subject: str, body: str) -> None:
        print(
            f"\n=== EMAIL (not sent: FPL_EMAIL / FPL_EMAIL_APP_PASSWORD not set) ===\n"
            f"Subject: {subject}\n\n{body}"
        )


def mailer_from_env(env: Mapping[str, str] = os.environ) -> Mailer:
    address, password = env.get("FPL_EMAIL"), env.get("FPL_EMAIL_APP_PASSWORD")
    if address and password:
        return GmailMailer(address, password.replace(" ", ""))  # Google shows it in groups of 4
    return PrintMailer()
