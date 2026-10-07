#!/usr/bin/env python3
"""Login una tantum (da rifare solo se lo script te lo chiede via Telegram).

    python setup_auth.py spotify    # login Spotify nel browser
    python setup_auth.py ytm        # login YouTube Music (codice da inserire su google.com/device)
    python setup_auth.py telegram   # trova il chat ID e manda un messaggio di prova
"""

from __future__ import annotations

import os
import sys

import requests

from ytmsync.config import load_config


def spotify(cfg) -> None:
    from ytmsync.spotify_side import make_auth

    if cfg.spotify_token_path.exists():
        cfg.spotify_token_path.unlink()
    auth = make_auth(cfg, open_browser=True)
    print("Si apre il browser per il login Spotify. Se non si apre, copia questo indirizzo:")
    print(auth.get_authorize_url())
    auth.get_access_token(as_dict=False)
    os.chmod(cfg.spotify_token_path, 0o600)
    import spotipy

    me = spotipy.Spotify(auth_manager=auth).me()
    print(f"OK: collegato come {me.get('display_name') or me.get('id')}")


def ytm(cfg) -> None:
    from ytmusicapi import setup_oauth

    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
    setup_oauth(cfg.ytm_client_id, cfg.ytm_client_secret, filepath=str(cfg.ytm_token_path), open_browser=True)
    os.chmod(cfg.ytm_token_path, 0o600)
    from ytmsync.ytm_side import YTMSide

    print(f"OK: YouTube Music collegato (canale: {YTMSide(cfg).check()})")


def telegram(cfg) -> None:
    base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
    if not cfg.telegram_chat_id or cfg.telegram_chat_id == "auto":
        input("Manda un messaggio qualsiasi al tuo bot su Telegram, poi premi Invio qui...")
        updates = requests.get(f"{base}/getUpdates", timeout=20).json().get("result", [])
        chats = {u["message"]["chat"]["id"] for u in updates if "message" in u}
        print("Chat ID trovati:", ", ".join(map(str, chats)) or "nessuno")
        print("Metti il tuo in TELEGRAM_CHAT_ID dentro .env e rilancia questo comando.")
        return
    r = requests.post(f"{base}/sendMessage", json={"chat_id": cfg.telegram_chat_id, "text": "✅ Notifiche del sync attive"}, timeout=20)
    print("OK: messaggio di prova inviato" if r.ok else f"Errore: {r.text}")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("spotify", "ytm", "telegram"):
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "telegram":
        os.environ.setdefault("TELEGRAM_CHAT_ID", "auto")
    cfg = load_config()
    {"spotify": spotify, "ytm": ytm, "telegram": telegram}[sys.argv[1]](cfg)


if __name__ == "__main__":
    main()
