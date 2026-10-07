#!/usr/bin/env python3
"""Sincronizzazione bidirezionale Spotify <-> YouTube Music. Pensato per girare da timer systemd."""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from ytmsync import state as state_mod
from ytmsync.config import ROOT, load_config
from ytmsync.engine import Engine
from ytmsync.judge import AIJudge
from ytmsync.notify import Notifier
from ytmsync.retry import AuthRequired, ConfigError, SafetyStop, wait_for_network

log = logging.getLogger("sync")


def setup_logging(verbose: bool) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_h = RotatingFileHandler(ROOT / "logs" / "sync.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file_h.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(file_h)
    root.addHandler(console)
    for noisy in ("urllib3", "httpx", "httpx2", "anthropic", "spotipy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="calcola cosa farebbe senza scrivere nulla")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)

    (ROOT / "data").mkdir(exist_ok=True)
    lock = open(ROOT / "data" / "sync.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Un'altra esecuzione è in corso, esco")
        return 0

    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("%s", e)
        return 2  # senza config non sappiamo nemmeno come mandare la notifica

    state, cache_valid = state_mod.load(cfg.state_path)
    notifier = Notifier(cfg.telegram_bot_token, cfg.telegram_chat_id, state["alerts"], cfg.alert_repeat_days)
    exit_code = 0

    try:
        if not wait_for_network(["api.spotify.com", "music.youtube.com"]):
            raise ConnectionError("rete non disponibile")

        from ytmsync.spotify_side import SpotifySide
        from ytmsync.ytm_side import YTMSide

        sp = SpotifySide(cfg)
        notifier.resolve("auth:Spotify")
        yt = YTMSide(cfg)
        notifier.resolve("auth:YouTube Music")

        judge = AIJudge(
            cfg.anthropic_api_key, cfg.model, cfg.max_web_searches, cfg.max_ai_calls_per_run, state["ai_verdicts"]
        )
        report = Engine(cfg, sp, yt, judge, state, cache_valid, dry_run=args.dry_run).run()
        log.info("Fatto: %s", report.summary())

        # Giudice IA: problemi di account -> avviso subito; errori temporanei -> avviso se si ripetono
        if judge.unavailable:
            notifier.alert("ai", f"Il giudice IA non funziona: {judge.unavailable}. "
                                 "I brani dubbi restano in attesa finché non sistemi la chiave/credito Anthropic.")
        elif judge.errors:
            state["ai_unavailable_runs"] += 1
            if state["ai_unavailable_runs"] >= cfg.failure_alert_after:
                notifier.alert("ai", f"Il giudice IA non risponde da {state['ai_unavailable_runs']} esecuzioni: "
                                     "i brani dubbi restano in attesa. Controlla logs/sync.log.")
        else:
            state["ai_unavailable_runs"] = 0
            notifier.resolve("ai")

        state["consecutive_failures"] = 0
        state["last_success"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        notifier.resolve("failure", "safety", "config")

    except AuthRequired as e:
        log.error("Serve un nuovo login: %s", e)
        notifier.alert(f"auth:{e.service}", f"Serve il tuo intervento su {e.service}: {e.detail}")
        exit_code = 3
    except ConfigError as e:
        log.error("Errore di configurazione: %s", e)
        notifier.alert("config", f"Errore di configurazione: {e}")
        exit_code = 2
    except SafetyStop as e:
        log.error("Blocco di sicurezza: %s", e)
        notifier.alert("safety", f"Blocco di sicurezza: {e}")
        exit_code = 4
    except Exception as e:  # noqa: BLE001
        log.exception("Esecuzione fallita")
        state["consecutive_failures"] += 1
        if state["consecutive_failures"] >= cfg.failure_alert_after:
            notifier.alert(
                "failure",
                f"La sincronizzazione fallisce da {state['consecutive_failures']} esecuzioni di fila. "
                f"Ultimo errore: {type(e).__name__}: {e}. Dettagli in logs/sync.log.",
            )
        exit_code = 1
    finally:
        if not args.dry_run:
            state_mod.save(cfg.state_path, state)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
