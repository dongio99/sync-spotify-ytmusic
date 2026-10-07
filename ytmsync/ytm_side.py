"""Lato YouTube Music, approccio ibrido.

- Playlist (lettura, creazione, aggiunte, rimozioni): API ufficiale YouTube Data v3 con il tuo login OAuth.
  Da agosto 2025 YouTube Music rifiuta le richieste OAuth fatte tramite ytmusicapi, mentre l'API
  ufficiale le accetta; le playlist di YouTube Music sono playlist YouTube a tutti gli effetti.
- Ricerca dei brani: ytmusicapi senza login (stessi risultati dell'app, con il numero di ascolti).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi.auth.oauth.exceptions import BadOAuthClient, UnauthorizedOAuthClient
from ytmusicapi.exceptions import YTMusicServerError

from .config import Config
from .models import Track
from .retry import AuthRequired, ConfigError, TransientHTTPError, with_retry

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
SEARCH_PAUSE_S = 0.8
WRITE_PAUSE_S = 0.3
TOPIC_SUFFIX = re.compile(r"\s*-\s*Topic$", re.IGNORECASE)
UNAVAILABLE_TITLES = {"Deleted video", "Private video", "Video eliminato", "Video privato"}


class QuotaExceeded(Exception):
    """Quota giornaliera dell'API YouTube esaurita: si riprende alla prossima esecuzione."""


def parse_views(text: str | None) -> int | None:
    """'1.4M' -> 1400000, '12K' -> 12000, '1,234' -> 1234 (interfaccia in inglese)."""
    if not text:
        return None
    m = re.match(r"\s*([\d.,]+)\s*([KMB]?)", text.strip(), re.IGNORECASE)
    if not m:
        return None
    num, suffix = m.groups()
    try:
        value = float(num.replace(",", "")) if suffix else float(num.replace(",", "").replace(".", ""))
    except ValueError:
        return None
    return int(value * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[suffix.upper()])


def parse_iso_duration(value: str | None) -> int | None:
    """'PT4M13S' -> 253."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m or not value or value == "P":
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _search_track(item: dict) -> Track | None:
    """Risultato di ricerca ytmusicapi -> Track."""
    if not item or not item.get("videoId") or not item.get("title"):
        return None
    return Track(
        platform="ytm",
        id=item["videoId"],
        title=item["title"],
        artists=[a["name"] for a in item.get("artists") or [] if a and a.get("name")],
        duration_s=item.get("duration_seconds"),
        raw_title=item["title"],
        video_type=item.get("videoType"),
        views=parse_views(item.get("views")),
        available=item.get("isAvailable", True) is not False,
    )


def playlist_item_track(item: dict) -> Track | None:
    """Voce di playlist dell'API Data v3 -> Track (la durata si aggiunge dopo)."""
    sn = item.get("snippet") or {}
    video_id = (item.get("contentDetails") or {}).get("videoId") or (sn.get("resourceId") or {}).get("videoId")
    title = sn.get("title") or ""
    if not video_id or not title:
        return None
    channel = sn.get("videoOwnerChannelTitle") or ""
    is_topic = bool(TOPIC_SUFFIX.search(channel))
    available = bool(channel) and title not in UNAVAILABLE_TITLES
    return Track(
        platform="ytm",
        id=video_id,
        title=title,
        # "Artista - Topic" è il canale automatico dell'artista; altrimenti il canale (VEVO, etichetta...)
        artists=[TOPIC_SUFFIX.sub("", channel)] if channel else [],
        raw_title=title,
        video_type="MUSIC_VIDEO_TYPE_ATV" if is_topic else "YOUTUBE_VIDEO",
        set_video_id=item.get("id"),  # id della voce di playlist, serve per rimuoverla
        available=available,
    )


class YTMSide:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.token_path: Path = cfg.ytm_token_path
        if not self.token_path.exists():
            raise AuthRequired("YouTube Music", "nessun login salvato. Esegui: python setup_auth.py ytm")
        self.creds = OAuthCredentials(cfg.ytm_client_id, cfg.ytm_client_secret)
        self.token = json.loads(self.token_path.read_text(encoding="utf-8"))
        self._refresh()
        self.http = requests.Session()
        self.search_client = YTMusic(language="en")  # senza login
        self._last_search = 0.0

    # ---------- token ----------

    def _refresh(self) -> None:
        """Rinnova l'access token. Se il refresh token è revocato serve un nuovo login."""
        try:
            fresh = with_retry(lambda: self.creds.refresh_token(self.token["refresh_token"]), "YouTube refresh token")
        except UnauthorizedOAuthClient as e:
            raise AuthRequired("YouTube Music", "il token non appartiene al client OAuth configurato. Esegui: python setup_auth.py ytm") from e
        except BadOAuthClient as e:
            raise ConfigError(f"YouTube: client OAuth non valido ({e}). Controlla YTM_CLIENT_ID/SECRET") from e
        if "access_token" not in fresh:
            err = fresh.get("error", "?")
            if err in ("invalid_grant", "invalid_client", "unauthorized_client"):
                raise AuthRequired(
                    "YouTube Music",
                    f"login scaduto o revocato ({err}). Esegui: python setup_auth.py ytm "
                    "(e verifica che l'app Google Cloud sia 'In production', non 'Testing')",
                )
            raise ConnectionError(f"refresh token YouTube fallito: {fresh}")
        self.token["access_token"] = fresh["access_token"]
        self.token["expires_in"] = fresh["expires_in"]
        self.token["expires_at"] = int(time.time()) + fresh["expires_in"]
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.token), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.token_path)

    # ---------- API Data v3 ----------

    def _api(self, method: str, path: str, what: str, params: dict | None = None, body: dict | None = None) -> dict:
        def once() -> dict:
            if self.token.get("expires_at", 0) - time.time() < 120:
                self._refresh()
            r = self.http.request(
                method,
                f"{API}/{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {self.token['access_token']}"},
                timeout=30,
            )
            if r.status_code == 401:
                self._refresh()
                raise TransientHTTPError(f"{what}: token rifiutato, rinnovato")
            # 409 "SERVICE_UNAVAILABLE / The operation was aborted" capita sulle scritture ravvicinate: è temporaneo
            if r.status_code in (409, 429) or r.status_code >= 500:
                ra = r.headers.get("Retry-After")
                raise TransientHTTPError(f"{what}: HTTP {r.status_code}", float(ra) if ra and ra.isdigit() else None)
            if r.status_code >= 400:
                self._raise_api_error(r, what)
            return r.json() if r.content else {}

        return with_retry(once, what)

    @staticmethod
    def _raise_api_error(r: requests.Response, what: str) -> None:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        reasons = {e.get("reason") for e in err.get("errors", [])}
        msg = err.get("message") or r.text[:300]
        if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
            raise QuotaExceeded(f"{what}: quota YouTube esaurita ({msg})")
        if reasons & {"accessNotConfigured", "SERVICE_DISABLED"} or "has not been used" in msg:
            raise ConfigError(f"YouTube Data API v3 non abilitata nel progetto Google Cloud: {msg}")
        if reasons & {"youtubeSignupRequired", "channelNotFound"}:
            raise ConfigError(f"L'account Google non ha un canale YouTube: {msg}")
        if r.status_code == 404 and "playlist" in what.lower():
            raise ConfigError(f"{what}: playlist non trovata ({msg})")
        raise requests.HTTPError(f"{what}: HTTP {r.status_code} {sorted(r for r in reasons if r)} {msg}", response=r)

    def check(self) -> str:
        """Verifica il login; restituisce il nome del canale."""
        data = self._api("GET", "channels", "verifica account", params={"part": "snippet", "mine": "true"})
        items = data.get("items") or []
        if not items:
            raise ConfigError("L'account Google non ha un canale YouTube")
        return items[0]["snippet"]["title"]

    # ---------- playlist ----------

    def find_playlist(self, title: str) -> str | None:
        token = None
        while True:
            params = {"part": "snippet", "mine": "true", "maxResults": 50}
            if token:
                params["pageToken"] = token
            data = self._api("GET", "playlists", "elenco playlist", params=params)
            for p in data.get("items") or []:
                if p["snippet"]["title"] == title:
                    return p["id"]
            token = data.get("nextPageToken")
            if not token:
                return None

    def create_playlist(self, title: str, description: str, privacy: str) -> str:
        clean = lambda s: s.replace("<", "").replace(">", "")  # noqa: E731
        body = {
            "snippet": {"title": clean(title)[:150], "description": clean(description)[:5000]},
            "status": {"privacyStatus": privacy.lower()},
        }
        return self._api("POST", "playlists", "creazione playlist", params={"part": "snippet,status"}, body=body)["id"]

    def tracks(self, playlist_id: str) -> list[Track]:
        out: list[Track] = []
        token = None
        while True:
            params = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": 50}
            if token:
                params["pageToken"] = token
            data = self._api("GET", "playlistItems", "lettura playlist YouTube", params=params)
            out += [t for t in (playlist_item_track(i) for i in data.get("items") or []) if t]
            token = data.get("nextPageToken")
            if not token:
                break
        self._fill_durations(out)
        return out

    def _fill_durations(self, tracks: list[Track]) -> None:
        ids = list(dict.fromkeys(t.id for t in tracks if t.available))
        durations: dict[str, int | None] = {}
        for i in range(0, len(ids), 50):
            data = self._api(
                "GET", "videos", "durate video", params={"part": "contentDetails", "id": ",".join(ids[i : i + 50])}
            )
            for v in data.get("items") or []:
                durations[v["id"]] = parse_iso_duration((v.get("contentDetails") or {}).get("duration"))
        for t in tracks:
            t.duration_s = durations.get(t.id)

    def add(self, playlist_id: str, video_ids: list[str]) -> list[str]:
        """Aggiunge i brani uno per uno; restituisce quelli effettivamente aggiunti."""
        added: list[str] = []
        for vid in video_ids:
            body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": vid}}}
            try:
                self._api("POST", "playlistItems", "aggiunta brano", params={"part": "snippet"}, body=body)
                added.append(vid)
            except QuotaExceeded as e:
                log.warning("%s. Aggiunti %d su %d, gli altri alla prossima esecuzione", e, len(added), len(video_ids))
                break
            except requests.HTTPError as e:
                log.warning("Brano %s non aggiunto: %s", vid, e)
            time.sleep(WRITE_PAUSE_S)
        return added

    def remove(self, playlist_id: str, tracks: list[Track]) -> None:
        for t in tracks:
            if not t.set_video_id:
                continue
            try:
                self._api("DELETE", "playlistItems", "rimozione brano", params={"id": t.set_video_id})
            except QuotaExceeded as e:
                log.warning("%s: le altre rimozioni alla prossima esecuzione", e)
                return
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    log.info("Voce già rimossa: %s", t.id)
                else:
                    raise
            time.sleep(WRITE_PAUSE_S)

    # ---------- ricerca (ytmusicapi, senza login) ----------

    def search(self, query: str, kind: str) -> list[Track]:
        wait = SEARCH_PAUSE_S - (time.monotonic() - self._last_search)
        if wait > 0:
            time.sleep(wait)
        try:
            results = with_retry(lambda: self.search_client.search(query, filter=kind, limit=10), f"ricerca YTM ({kind})")
        except YTMusicServerError as e:
            log.warning("Ricerca YTM fallita per %r: %s", query, e)
            results = []
        finally:
            self._last_search = time.monotonic()
        return [t for t in (_search_track(r) for r in results) if t]
