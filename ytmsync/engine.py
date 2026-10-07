"""Logica di sincronizzazione.

Fonte di verità: il confronto diretto tra le due playlist, scaricate entrambe a ogni esecuzione.
La cache serve a (1) non ripetere lavoro già fatto e (2) capire se un brano manca da YouTube Music
perché è stato tolto da Spotify. Regole:
- brano solo su Spotify            -> lo aggiungo su YouTube Music (nuovo, o sparito da YTM)
- brano solo su YTM, mai sincronizzato -> lo aggiungo su Spotify
- brano solo su YTM, già sincronizzato -> è stato tolto da Spotify: lo tolgo da YTM
  (solo con cache valida, dopo conferma su più esecuzioni e sotto i limiti di sicurezza)
Su Spotify lo script aggiunge soltanto, non rimuove mai.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from .config import Config
from .matching import Choice, Judge, Matcher, most_views, spotify_preferred
from .models import Track
from .normalize import Normalizer
from .retry import SafetyStop

log = logging.getLogger(__name__)

SYNC_MARKER = "Sincronizzata automaticamente da Spotify."


@dataclass
class Report:
    added_to_ytm: list[str] = field(default_factory=list)
    added_to_spotify: list[str] = field(default_factory=list)
    removed_from_ytm: list[str] = field(default_factory=list)
    pending_removal: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    gave_up: int = 0

    def summary(self) -> str:
        return (
            f"+{len(self.added_to_ytm)} su YTM, +{len(self.added_to_spotify)} su Spotify, "
            f"-{len(self.removed_from_ytm)} da YTM, {len(self.pending_removal)} rimozioni in attesa di conferma, "
            f"{len(self.not_found)} non trovati, {len(self.deferred)} rimandati, {self.gave_up} abbandonati"
        )


def _label(t: Track) -> str:
    return f"{t.primary_artist} - {t.title}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _over_limit(count: int, base: int, fraction: float, absolute: int) -> bool:
    return count > absolute or (count > 3 and count > fraction * max(base, 1))


class Engine:
    def __init__(self, cfg: Config, sp, yt, judge: Judge, state: dict, cache_valid: bool, dry_run: bool = False):
        self.cfg = cfg
        self.sp = sp
        self.yt = yt
        self.judge = judge
        self.state = state
        self.cache_valid = cache_valid
        self.dry_run = dry_run
        self.n = Normalizer(cfg.extra_tail_keywords)
        self.m = Matcher(
            self.n,
            high=cfg.score_high,
            low=cfg.score_low,
            tie_margin=cfg.tie_margin,
            duration_tolerance_s=cfg.duration_tolerance_s,
        )
        self.report = Report()

    # ---------- playlist di destinazione ----------

    def resolve_ytm_playlist(self, meta: dict) -> str | None:
        pid = self.cfg.ytm_playlist_id or ""
        if not pid and self.cfg.ytm_playlist_file.exists():
            pid = self.cfg.ytm_playlist_file.read_text(encoding="utf-8").strip()
        if not pid:
            pid = self.yt.find_playlist(meta["name"]) or ""
            if pid:
                log.info("Trovata playlist YouTube Music esistente con lo stesso nome: %s", pid)
        if not pid:
            if self.dry_run:
                log.info("[prova] creerei la playlist YouTube Music %r", meta["name"])
                return None
            desc = ((meta.get("description") or "").strip() + "\n\n" + SYNC_MARKER).strip()
            pid = self.yt.create_playlist(meta["name"], desc, self.cfg.ytm_privacy)
            log.info("Creata playlist YouTube Music %r: %s", meta["name"], pid)
            self._save_cover(meta)
        if not self.dry_run:
            self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
            self.cfg.ytm_playlist_file.write_text(pid + "\n", encoding="utf-8")
        return pid

    def _save_cover(self, meta: dict) -> None:
        """ytmusicapi non può impostare copertine: salvo quella di Spotify per caricarla a mano una volta."""
        images = meta.get("images") or []
        if not images:
            return
        try:
            r = requests.get(images[0]["url"], timeout=20)
            r.raise_for_status()
            (self.cfg.data_dir / "cover.jpg").write_bytes(r.content)
            log.info("Copertina Spotify salvata in data/cover.jpg")
        except Exception as e:  # noqa: BLE001
            log.warning("Copertina non scaricata: %s", e)

    # ---------- ricerca del corrispettivo ----------

    def _find_on_ytm(self, s: Track) -> Choice:
        query = f"{s.primary_artist} {self.n.strip_tails(s.title)}".strip()
        result = Choice("none")
        for kind in ("songs", "videos"):
            result = self.m.choose(s, self.yt.search(query, kind), self.judge, most_views)
            if result.status != "none":
                return result
        return result

    def _find_on_spotify(self, y: Track) -> Choice:
        # Proviamo le varie letture del titolo YTM ("Artista - Titolo", campi separati, ...)
        queries: list[tuple[str, str]] = [(self.n.strip_tails(y.title), y.primary_artist)]
        split = Normalizer.split_raw(self.n.strip_tails(y.raw_title or y.title))
        if split:
            queries += [(split[1], split[0]), (split[0], split[1])]
        seen: dict[str, Track] = {}
        for title, artist in queries:
            for t in self.sp.search(title, artist):
                seen.setdefault(t.id, t)
            # Basta la prima lettura che dà un risultato certo
            quick = self.m.choose(y, list(seen.values()), lambda *_: None, spotify_preferred)
            if quick.status == "found":
                return quick
        return self.m.choose(y, list(seen.values()), self.judge, spotify_preferred)

    # ---------- esecuzione ----------

    def run(self) -> Report:
        st = self.state
        meta = self.sp.playlist_meta()
        ytm_pid = self.resolve_ytm_playlist(meta)

        sp_id = self.cfg.spotify_playlist_id
        if self.cache_valid and (st.get("spotify_playlist_id") != sp_id or st.get("ytm_playlist_id") != ytm_pid):
            log.info("Playlist cambiate rispetto alla cache: la cache non vale per le rimozioni")
            self.cache_valid = False
        if not self.cache_valid:
            st["pairs"], st["pending_removals"] = [], {}
        st["spotify_playlist_id"], st["ytm_playlist_id"] = sp_id, ytm_pid

        S = self.sp.tracks()
        Y_all = self.yt.tracks(ytm_pid) if ytm_pid else []
        Y = [y for y in Y_all if y.available]
        log.info("Spotify: %d brani; YouTube Music: %d brani (%d non disponibili)", len(S), len(Y_all), len(Y_all) - len(Y))

        old_pairs: list[dict] = st["pairs"]
        if old_pairs and (not S or not Y_all):
            raise SafetyStop(
                f"una delle due playlist risulta vuota (Spotify {len(S)}, YouTube Music {len(Y_all)}) "
                "ma prima erano sincronizzate. Non ho toccato nulla: controlla che le playlist siano a posto."
            )

        # 1) Coppie già note dalla cache (ottimizzazione: niente IA per brani già verificati)
        s_by_id = {s.id: s for s in S}
        y_by_id: dict[str, Track] = {}
        for y in Y:
            y_by_id.setdefault(y.id, y)
        matched: list[tuple[Track, Track]] = []
        used_s: set[str] = set()
        used_y: set[str] = set()  # setVideoId (o videoId) delle voci YTM già abbinate
        for p in old_pairs:
            s, y = s_by_id.get(p["sp"]), y_by_id.get(p["yt"])
            if s and y and s.id not in used_s and (y.set_video_id or y.id) not in used_y:
                matched.append((s, y))
                used_s.add(s.id)
                used_y.add(y.set_video_id or y.id)

        # 2) Confronto testuale + IA sui rimanenti
        rest_s = [s for s in S if s.id not in used_s]
        rest_y = [y for y in Y if (y.set_video_id or y.id) not in used_y]
        pr = self.m.pair(rest_s, rest_y, self.judge)
        for s, y, how in pr.pairs:
            log.debug("Abbinati (%s): %s <-> %s", how, _label(s), _label(y))
            matched.append((s, y))
            used_s.add(s.id)
            used_y.add(y.set_video_id or y.id)

        matched_y_ids = {y.id for _, y in matched}
        # videoId sincronizzati in passato il cui brano Spotify non c'è più
        cached_yt_ids = {p["yt"] for p in old_pairs if p["sp"] not in s_by_id}
        not_found = st["not_found"]

        def given_up(t: Track) -> bool:
            return not_found.get(t.id, {}).get("attempts", 0) >= self.cfg.not_found_max_attempts

        to_ytm = [s for s in S if s.id not in used_s and s.id not in pr.blocked_left]
        to_spotify: list[Track] = []
        removal_candidates: list[Track] = []
        for y in Y:
            if (y.set_video_id or y.id) in used_y or y.id in pr.blocked_right:
                continue
            if y.id in matched_y_ids:
                continue  # doppione nella playlist YTM di un brano già abbinato: lo lascio stare
            if self.cache_valid and y.id in cached_yt_ids:
                removal_candidates.append(y)  # era sincronizzato e su Spotify non c'è più
            else:
                to_spotify.append(y)

        self.report.deferred += [_label(t) for t in S if t.id in pr.blocked_left]
        self.report.gave_up = sum(given_up(t) for t in to_ytm + to_spotify)
        to_ytm = [t for t in to_ytm if not given_up(t)]
        to_spotify = [t for t in to_spotify if not given_up(t)]

        # 3) Rimozioni: confermate solo se viste in più esecuzioni consecutive
        pending_old = st["pending_removals"]
        pending_new = {y.id: pending_old.get(y.id, 0) + 1 for y in removal_candidates}
        confirmed = [y for y in removal_candidates if pending_new[y.id] >= self.cfg.removal_confirm_runs]
        waiting = [y for y in removal_candidates if pending_new[y.id] < self.cfg.removal_confirm_runs]

        # 4) Limiti di sicurezza, prima di qualsiasi scrittura
        if _over_limit(len(confirmed), len(Y), self.cfg.max_remove_fraction, self.cfg.max_remove_absolute):
            raise SafetyStop(
                f"dovrei togliere {len(confirmed)} brani su {len(Y)} da YouTube Music in un colpo solo. "
                "Non ho toccato nulla. Se è voluto, alza max_remove_absolute/max_remove_fraction in config.toml."
            )
        if old_pairs:
            if _over_limit(len(to_ytm), len(Y), self.cfg.max_add_fraction, self.cfg.max_add_absolute):
                raise SafetyStop(
                    f"dovrei aggiungere {len(to_ytm)} brani su YouTube Music (che ne ha {len(Y)}) in un colpo solo. "
                    "Non ho toccato nulla. Se è voluto, alza max_add_absolute/max_add_fraction in config.toml."
                )
            if _over_limit(len(to_spotify), len(S), self.cfg.max_add_fraction, self.cfg.max_add_absolute):
                raise SafetyStop(
                    f"dovrei aggiungere {len(to_spotify)} brani su Spotify (che ne ha {len(S)}) in un colpo solo. "
                    "Non ho toccato nulla. Se è voluto, alza max_add_absolute/max_add_fraction in config.toml."
                )

        # 5) Ricerca dei corrispettivi
        add_ytm: list[tuple[Track, Track]] = []
        y_ids_present = {y.id for y in Y_all}
        for s in to_ytm:
            ch = self._find_on_ytm(s)
            if ch.status == "found":
                if ch.track.id in y_ids_present or any(c.id == ch.track.id for _, c in add_ytm):
                    log.info("Già presente su YTM, non lo aggiungo: %s", _label(s))
                    continue
                log.info("Da aggiungere su YTM (%s): %s -> %s", ch.how, _label(s), _label(ch.track))
                add_ytm.append((s, ch.track))
            else:
                self._miss(s, ch)

        add_sp: list[tuple[Track, Track]] = []
        s_ids_present = set(s_by_id)
        for y in to_spotify:
            ch = self._find_on_spotify(y)
            if ch.status == "found":
                if ch.track.id in s_ids_present or any(c.id == ch.track.id for _, c in add_sp):
                    log.info("Già presente su Spotify, non lo aggiungo: %s", _label(y))
                    continue
                log.info("Da aggiungere su Spotify (%s): %s -> %s", ch.how, _label(y), _label(ch.track))
                add_sp.append((y, ch.track))
            else:
                self._miss(y, ch)

        # 6) Scritture
        new_pairs = [(s, y) for s, y in matched]
        if self.dry_run:
            log.info("[prova] aggiungerei %d brani su YTM, %d su Spotify, toglierei %d da YTM",
                     len(add_ytm), len(add_sp), len(confirmed))
            return self.report

        if add_ytm:
            added = set(self.yt.add(ytm_pid, [y.id for _, y in add_ytm]))
            for s, y in add_ytm:
                if y.id in added:
                    new_pairs.append((s, y))
                    self.report.added_to_ytm.append(_label(s))
                    not_found.pop(s.id, None)
        if add_sp:
            self.sp.add([s.id for _, s in add_sp])
            for y, s in add_sp:
                new_pairs.append((s, y))
                self.report.added_to_spotify.append(_label(y))
                not_found.pop(y.id, None)
        if confirmed:
            self.yt.remove(ytm_pid, confirmed)
            self.report.removed_from_ytm += [_label(y) for y in confirmed]
            for y in confirmed:
                log.info("Tolto da YTM perché rimosso da Spotify: %s (%s)", _label(y), y.id)

        # 7) Aggiornamento cache
        keep_old = {y.id for y in waiting} | set(pr.blocked_right)
        pairs = [
            {"sp": s.id, "sp_key": self.m.primary_key(s), "yt": y.id, "yt_key": self.m.primary_key(y)}
            for s, y in new_pairs
        ]
        known = {p["yt"] for p in pairs}
        pairs += [p for p in old_pairs if p["yt"] in keep_old and p["yt"] not in known]
        st["pairs"] = pairs
        st["pending_removals"] = {y.id: pending_new[y.id] for y in waiting}
        self.report.pending_removal = [_label(y) for y in waiting]

        present = {t.id for t in S} | {t.id for t in Y}
        for tid in list(not_found):
            if tid not in present:
                not_found.pop(tid)
        for s, y in matched:
            not_found.pop(s.id, None)
            not_found.pop(y.id, None)
        if len(st["ai_verdicts"]) > 5000:
            st["ai_verdicts"] = dict(list(st["ai_verdicts"].items())[-3000:])
        return self.report

    def _miss(self, t: Track, ch: Choice) -> None:
        if ch.status == "undecided":
            self.report.deferred.append(_label(t))
            return
        entry = self.state["not_found"].setdefault(t.id, {"attempts": 0})
        entry["attempts"] += 1
        entry["last"] = _now()
        entry["label"] = _label(t)
        self.report.not_found.append(_label(t))
        log.info("Corrispettivo non trovato (tentativo %d): %s", entry["attempts"], _label(t))
