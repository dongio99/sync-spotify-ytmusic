from __future__ import annotations

import logging
import random
import socket
import time
from typing import Callable, TypeVar

import requests
from spotipy.exceptions import SpotifyException
from ytmusicapi.exceptions import YTMusicServerError

log = logging.getLogger(__name__)
T = TypeVar("T")


class AuthRequired(Exception):
    """Serve un nuovo login manuale su un servizio."""

    def __init__(self, service: str, detail: str):
        super().__init__(f"{service}: {detail}")
        self.service = service
        self.detail = detail


class ConfigError(Exception):
    """Configurazione sbagliata o permessi mancanti: serve un intervento."""


class SafetyStop(Exception):
    """Il blocco di sicurezza ha fermato le scritture."""


class TransientHTTPError(Exception):
    """Errore HTTP temporaneo (429/5xx) da ritentare."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(exc: Exception) -> float | None:
    if isinstance(exc, TransientHTTPError):
        return exc.retry_after
    if isinstance(exc, SpotifyException) and exc.headers:
        value = exc.headers.get("Retry-After") or exc.headers.get("retry-after")
        if value:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (TransientHTTPError, requests.ConnectionError, requests.Timeout, socket.timeout, ConnectionError)):
        return True
    if isinstance(exc, SpotifyException):
        return exc.http_status in (429, 500, 502, 503, 504) or exc.http_status is None
    if isinstance(exc, YTMusicServerError):
        msg = str(exc)
        return not any(code in msg for code in ("400", "401", "403", "404"))
    return False


def with_retry(fn: Callable[[], T], what: str, attempts: int = 5, base: float = 2.0) -> T:
    """Esegue fn con backoff esponenziale sugli errori temporanei (rete, 429, 5xx)."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts or not _is_transient(exc):
                raise
            wait = _retry_after(exc) or base**attempt + random.uniform(0, 1)
            wait = min(wait, 300)
            log.warning("%s: errore temporaneo (%s), ritento tra %.0fs [%d/%d]", what, exc, wait, attempt, attempts)
            time.sleep(wait)
    raise AssertionError("unreachable")


def wait_for_network(hosts: list[str], max_wait_s: int = 300) -> bool:
    """Utile subito dopo l'accensione: aspetta che la rete risponda."""
    deadline = time.monotonic() + max_wait_s
    while True:
        for host in hosts:
            try:
                with socket.create_connection((host, 443), timeout=5):
                    return True
            except OSError:
                pass
        if time.monotonic() > deadline:
            return False
        time.sleep(15)
