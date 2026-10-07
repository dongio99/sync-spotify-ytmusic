from __future__ import annotations

import logging

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth

from .config import Config
from .models import Track
from .retry import AuthRequired, ConfigError, with_retry

log = logging.getLogger(__name__)

SCOPE = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
_AUTH_ERRORS = ("invalid_grant", "invalid_client", "unauthorized_client", "invalid_scope")


def make_auth(cfg: Config, open_browser: bool = False) -> SpotifyOAuth:
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
    return SpotifyOAuth(
        client_id=cfg.spotify_client_id,
        client_secret=cfg.spotify_client_secret,
        redirect_uri=cfg.spotify_redirect_uri,
        scope=SCOPE,
        cache_handler=CacheFileHandler(cache_path=str(cfg.spotify_token_path)),
        open_browser=open_browser,
    )


def _track(item: dict) -> Track | None:
    if not item or item.get("type") != "track" or item.get("is_local") or not item.get("uri"):
        return None
    album = item.get("album") or {}
    return Track(
        platform="spotify",
        id=item["uri"],
        title=item.get("name") or "",
        artists=[a["name"] for a in item.get("artists") or [] if a.get("name")],
        duration_s=(item.get("duration_ms") or 0) // 1000 or None,
        album_type=album.get("album_type"),
        release_date=album.get("release_date"),
    )


class SpotifySide:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.playlist_id = cfg.spotify_playlist_id
        auth = make_auth(cfg)
        cached = auth.cache_handler.get_cached_token()
        if not cached or "refresh_token" not in cached:
            raise AuthRequired("Spotify", "nessun login salvato. Esegui: python setup_auth.py spotify")
        if not auth._is_scope_subset(SCOPE, cached.get("scope", "")):
            raise AuthRequired("Spotify", "il login salvato non ha i permessi necessari. Esegui: python setup_auth.py spotify")
        try:
            # Refresh forzato a ogni avvio: scopre subito un token revocato
            with_retry(lambda: auth.refresh_access_token(cached["refresh_token"]), "Spotify refresh token")
        except SpotifyOauthError as e:
            if e.error in _AUTH_ERRORS:
                raise AuthRequired("Spotify", f"login scaduto o revocato ({e.error}). Esegui: python setup_auth.py spotify") from e
            raise
        self.sp = spotipy.Spotify(
            auth_manager=auth,
            requests_timeout=20,
            retries=5,
            status_retries=5,
            backoff_factor=1.0,
        )

    def _call(self, fn, what: str):
        try:
            return with_retry(fn, what)
        except SpotifyException as e:
            if e.http_status == 401:
                raise AuthRequired("Spotify", f"accesso negato ({e.msg}). Esegui: python setup_auth.py spotify") from e
            if e.http_status in (403, 404):
                raise ConfigError(f"Spotify, {what}: {e.http_status} {e.msg}") from e
            raise

    def playlist_meta(self) -> dict:
        return self._call(
            lambda: self.sp.playlist(self.playlist_id, fields="id,name,description,images,collaborative,owner(id)"),
            "lettura playlist Spotify",
        )

    def tracks(self) -> list[Track]:
        out: list[Track] = []
        offset = 0
        while True:
            page = self._call(
                lambda: self.sp.playlist_items(self.playlist_id, limit=50, offset=offset, additional_types=("track",)),
                "brani playlist Spotify",
            )
            if page is None or "items" not in page:
                raise ConfigError(
                    "Spotify non restituisce i brani della playlist: l'account usato deve esserne proprietario o collaboratore"
                )
            for entry in page["items"]:
                # Dal 2026 il campo si chiama "item" (prima "track")
                t = _track((entry or {}).get("item") or (entry or {}).get("track"))
                if t:
                    out.append(t)
            if not page.get("next"):
                return out
            offset += len(page["items"])

    def search(self, title: str, artist: str) -> list[Track]:
        queries = [f'track:"{title}" artist:"{artist}"', f"{artist} {title}"] if artist else [title]
        seen: dict[str, Track] = {}
        for q in queries:
            res = self._call(lambda: self.sp.search(q=q, type="track", limit=10, market="from_token"), "ricerca Spotify")
            for item in (res.get("tracks") or {}).get("items") or []:
                t = _track(item)
                if t and t.id not in seen:
                    seen[t.id] = t
        return list(seen.values())

    def add(self, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            self._call(lambda: self.sp.playlist_add_items(self.playlist_id, chunk), "aggiunta brani su Spotify")
