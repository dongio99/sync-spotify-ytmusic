from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)


class Notifier:
    """Notifiche Telegram, con soppressione dei ripetuti: lo stesso avviso al massimo ogni N giorni."""

    def __init__(self, bot_token: str, chat_id: str, alerts_state: dict, repeat_days: int = 3):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.alerts = alerts_state  # chiave avviso -> ISO timestamp ultimo invio
        self.repeat = timedelta(days=repeat_days)

    def send(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            log.error("Telegram non configurato, avviso non inviato: %s", text)
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                timeout=20,
            )
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("Invio Telegram fallito: %s", e)
            return False

    def alert(self, key: str, text: str) -> None:
        now = datetime.now(timezone.utc)
        last = self.alerts.get(key)
        if last and now - datetime.fromisoformat(last) < self.repeat:
            log.info("Avviso '%s' già inviato di recente, non lo ripeto", key)
            return
        if self.send("🎵 Sync Spotify ↔ YouTube Music\n\n" + text):
            self.alerts[key] = now.isoformat()

    def resolve(self, *keys: str) -> None:
        for k in keys:
            self.alerts.pop(k, None)
