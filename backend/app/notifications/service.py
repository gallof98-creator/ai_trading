"""Central notification service: every alert is stored in-app and optionally pushed to external channels.
A failing channel is logged and never interrupts trading or risk handling."""
from __future__ import annotations

import smtplib
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage

from ..core.logging import get_logger
from ..database.base import Database
from ..database.models import Notification
from ..database.settings_store import NotificationSettings

log = get_logger(__name__)
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
CATEGORIES = ("signal", "execution", "order_rejected", "approval", "broker", "risk", "reconciliation", "sizing", "model", "strategy", "scanner", "system")


class Channel:
    name = "channel"

    def send(self, title: str, body: str, severity: str) -> None:
        raise NotImplementedError


class EmailChannel(Channel):
    name = "email"

    def __init__(self, host: str, port: int, user: str, password: str, to: str, smtp_factory: Callable = smtplib.SMTP) -> None:
        self.host, self.port, self.user, self.password, self.to, self._smtp = host, port, user, password, to, smtp_factory

    def send(self, title: str, body: str, severity: str) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = f"[QUANT AI TRADER][{severity.upper()}] {title}", self.user or "qat@localhost", self.to
        msg.set_content(body or title)
        with self._smtp(self.host, self.port, timeout=10) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, http_post: Callable) -> None:
        self._token, self.chat_id, self._post = token, chat_id, http_post

    def send(self, title: str, body: str, severity: str) -> None:
        text = f"[{severity.upper()}] {title}" + (f"\n{body}" if body else "")
        response = self._post(f"https://api.telegram.org/bot{self._token}/sendMessage", json={"chat_id": self.chat_id, "text": text[:4000]}, timeout=10)
        response.raise_for_status()


class Notifier:
    def __init__(self, db: Database, settings: Callable[[], NotificationSettings], channels: list[Channel] | None = None) -> None:
        self.db, self._settings, self.channels = db, settings, channels or []

    def notify(self, ts: datetime, category: str, severity: str, title: str, body: str = "") -> None:
        try:
            with self.db.session() as session:
                session.add(Notification(ts=ts, category=category, severity=severity, title=title[:200], body=body))
        except Exception as exc:  # the trading loop must survive a database hiccup
            log.error("notification_store_failed", error=str(exc), title=title)
        cfg = self._settings()
        if category in cfg.muted_categories or SEVERITY_RANK[severity] < SEVERITY_RANK[cfg.external_min_severity]:
            return
        for channel in self.channels:
            try:
                channel.send(title, body, severity)
            except Exception as exc:
                log.error("notification_channel_failed", channel=channel.name, error=str(exc))
