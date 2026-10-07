from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .retry import ConfigError

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    """Carica un file .env semplice (CHIAVE=valore) senza sovrascrivere l'ambiente."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    spotify_playlist: str
    ytm_privacy: str = "PRIVATE"
    ytm_playlist_id: str = ""
    model: str = "claude-haiku-4-5"
    max_web_searches: int = 2
    max_ai_calls_per_run: int = 25
    score_high: float = 90
    score_low: float = 60
    tie_margin: float = 2
    duration_tolerance_s: int = 10
    not_found_max_attempts: int = 5
    max_add_fraction: float = 0.30
    max_add_absolute: int = 15
    max_remove_fraction: float = 0.20
    max_remove_absolute: int = 10
    removal_confirm_runs: int = 2
    alert_repeat_days: int = 3
    failure_alert_after: int = 3
    extra_tail_keywords: list[str] = field(default_factory=list)

    # segreti (da .env)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    ytm_client_id: str = ""
    ytm_client_secret: str = ""
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    root: Path = ROOT

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def spotify_token_path(self) -> Path:
        return self.secrets_dir / "spotify_token.json"

    @property
    def ytm_token_path(self) -> Path:
        return self.secrets_dir / "ytm_oauth.json"

    @property
    def ytm_playlist_file(self) -> Path:
        return self.data_dir / "ytm_playlist_id.txt"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def spotify_playlist_id(self) -> str:
        # Accetta ID, URI (spotify:playlist:...) o link (https://open.spotify.com/playlist/...?si=...)
        p = self.spotify_playlist.strip()
        if "playlist/" in p:
            p = p.split("playlist/", 1)[1]
        if p.startswith("spotify:playlist:"):
            p = p.rsplit(":", 1)[1]
        return p.split("?", 1)[0].strip("/")


def load_config(root: Path = ROOT) -> Config:
    _load_env(root / ".env")
    cfg_path = root / "config.toml"
    if not cfg_path.exists():
        raise ConfigError(f"Manca {cfg_path}: copia config.example.toml in config.toml e compilalo")
    raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))

    flat: dict = {}
    for key, value in raw.items():  # le sezioni [..] servono solo a leggibilità
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    known = set(Config.__dataclass_fields__) - {"root"}
    unknown = set(flat) - known
    if unknown:
        raise ConfigError(f"Opzioni sconosciute in config.toml: {', '.join(sorted(unknown))}")
    if not flat.get("spotify_playlist"):
        raise ConfigError("config.toml: manca spotify_playlist")

    env = {
        "spotify_client_id": "SPOTIFY_CLIENT_ID",
        "spotify_client_secret": "SPOTIFY_CLIENT_SECRET",
        "spotify_redirect_uri": "SPOTIFY_REDIRECT_URI",
        "ytm_client_id": "YTM_CLIENT_ID",
        "ytm_client_secret": "YTM_CLIENT_SECRET",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": "TELEGRAM_CHAT_ID",
    }
    for attr, var in env.items():
        if os.environ.get(var):
            flat[attr] = os.environ[var]

    cfg = Config(**flat, root=root)
    missing = [var for attr, var in env.items() if attr != "spotify_redirect_uri" and not getattr(cfg, attr)]
    if missing:
        raise ConfigError(f"Variabili mancanti in .env: {', '.join(missing)}")
    return cfg
