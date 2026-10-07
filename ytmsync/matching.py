"""Confronto in due fasi: prima testuale, poi (solo per i dubbi) il giudice IA."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from rapidfuzz import fuzz

from .models import Track
from .normalize import Normalizer

# Il giudice riceve (brano di riferimento, candidati) e restituisce:
#   indice del candidato uguale | -1 se nessuno è lo stesso brano | None se non sa decidere ora
ALT_WEIGHT = 0.8  # titoli alternativi tra parentesi: al massimo "dubbio", decide l'IA
VIDEO_TYPES = {"MUSIC_VIDEO_TYPE_OMV", "MUSIC_VIDEO_TYPE_UGC", "YOUTUBE_VIDEO"}

Judge = Callable[[Track, list[Track]], "int | None"]


@dataclass
class PairResult:
    pairs: list[tuple[Track, Track, str]] = field(default_factory=list)  # (sinistra, destra, come)
    blocked_left: set[str] = field(default_factory=set)
    blocked_right: set[str] = field(default_factory=set)


@dataclass
class Choice:
    status: str  # "found" | "none" | "undecided"
    track: Track | None = None
    how: str = ""


class Matcher:
    def __init__(
        self,
        normalizer: Normalizer,
        high: float = 90,
        low: float = 60,
        tie_margin: float = 2,
        duration_tolerance_s: int = 10,
        max_doubt_candidates: int = 3,
    ):
        self.n = normalizer
        self.high = high
        self.low = low
        self.tie_margin = tie_margin
        self.duration_tolerance_s = duration_tolerance_s
        self.max_doubt_candidates = max_doubt_candidates

    # ---------- interpretazioni e chiavi ----------

    def interpretations(self, t: Track) -> list[tuple[str, str, float]]:
        """Letture (titolo, artista, peso). Peso < 1: lettura plausibile ma mai sufficiente da sola."""
        if t._interp:
            return t._interp
        out: list[tuple[str, str, float]] = []

        def add(title: str, artist: str, weight: float = 1.0) -> None:
            item = (self.n.title(title), self.n.artist(artist), weight)
            if item[0] and not any(o[:2] == item[:2] for o in out):
                out.append(item)

        add(t.title, t.primary_artist)
        if t.platform == "ytm":
            # Stringa grezza "Artista - Titolo (Official Video)": proviamo entrambi i versi
            split = Normalizer.split_raw(self.n.strip_tails(t.raw_title or t.title))
            if split:
                left, right = split
                add(right, left)
                add(left, right)
                # A volte il canale è l'artista e il titolo contiene "Titolo - Altro"
                add(right, t.primary_artist)
                add(left, t.primary_artist)
        for alt in self.n.alt_titles(t.title):
            add(alt, t.primary_artist, ALT_WEIGHT)
        t._interp = out
        return out

    def keys(self, t: Track) -> set[str]:
        return {f"{ti}|{ar}" for ti, ar, w in self.interpretations(t) if ar and w == 1.0}

    def primary_key(self, t: Track) -> str:
        ti, ar, _ = self.interpretations(t)[0] if self.interpretations(t) else ("", "", 1.0)
        return f"{ti}|{ar}"

    def _bag(self, t: Track) -> set[str]:
        return set((self.n.text(self.n.strip_tails(t.raw_title or t.title)) + " " + self.n.artist(t.primary_artist)).split())

    # ---------- punteggio ----------

    def score(self, a: Track, b: Track) -> float:
        """0-100. Il titolo comanda: l'artista può solo confermare o abbassare, mai alzare un titolo diverso."""
        best = 0.0
        for ta, aa, wa in self.interpretations(a):
            for tb, ab, wb in self.interpretations(b):
                ts = fuzz.token_sort_ratio(ta, tb)
                ar = fuzz.ratio(aa, ab) if aa and ab else 50.0
                best = max(best, ts * (0.6 + 0.4 * ar / 100) * min(wa, wb))

        # Lettura "a sacchetto di parole" per titoli YTM strani: vale solo se le parole del titolo
        # di ciascuno compaiono nell'altro
        bag_a, bag_b = self._bag(a), self._bag(b)
        words_a = set(self.interpretations(a)[0][0].split()) if self.interpretations(a) else set()
        words_b = set(self.n.title(b.raw_title or b.title).split())
        if words_a and words_b:
            cover = min(len(words_a & bag_b) / len(words_a), len(words_b & bag_a) / len(words_b))
            best = max(best, 0.97 * cover * fuzz.ratio(" ".join(sorted(bag_a)), " ".join(sorted(bag_b))))

        if a.duration_s and b.duration_s:
            diff = abs(a.duration_s - b.duration_s)
            # I videoclip hanno spesso intro/finali più lunghi del brano
            is_video = any(t.video_type in VIDEO_TYPES for t in (a, b))
            tolerance = max(self.duration_tolerance_s, 45) if is_video else self.duration_tolerance_s
            if diff > tolerance:
                best -= min(40.0, 5 + (diff - tolerance) * 0.5)
        return max(best, 0.0)

    def same_key(self, a: Track, b: Track) -> bool:
        return bool(self.keys(a) & self.keys(b))

    # ---------- abbinamento tra due playlist ----------

    def pair(self, left: list[Track], right: list[Track], judge: Judge) -> PairResult:
        res = PairResult()
        used_r: set[int] = set()
        free_l: list[Track] = []

        # Fase 1a: chiave normalizzata identica
        by_key: dict[str, list[int]] = {}
        for i, r in enumerate(right):
            for k in self.keys(r):
                by_key.setdefault(k, []).append(i)
        for l in left:
            hit = next((i for k in self.keys(l) for i in by_key.get(k, []) if i not in used_r), None)
            if hit is None:
                free_l.append(l)
            else:
                used_r.add(hit)
                res.pairs.append((l, right[hit], "chiave"))

        # Fase 1b: somiglianza alta
        scored = sorted(
            ((self.score(l, right[i]), li, i) for li, l in enumerate(free_l) for i in range(len(right)) if i not in used_r),
            reverse=True,
        )
        used_l: set[int] = set()
        for s, li, i in scored:
            if s < self.high:
                break
            if li in used_l or i in used_r:
                continue
            used_l.add(li)
            used_r.add(i)
            res.pairs.append((free_l[li], right[i], f"somiglianza {s:.0f}"))

        # Fase 2: zona dubbia -> giudice IA
        doubts: dict[int, list[tuple[float, int]]] = {}
        for s, li, i in scored:
            if self.low <= s < self.high and li not in used_l and i not in used_r:
                doubts.setdefault(li, []).append((s, i))
        for li in sorted(doubts, key=lambda k: -doubts[k][0][0]):
            cands = [(s, i) for s, i in doubts[li] if i not in used_r][: self.max_doubt_candidates]
            if not cands:
                continue
            verdict = judge(free_l[li], [right[i] for _, i in cands])
            if verdict is None:
                res.blocked_left.add(free_l[li].id)
                res.blocked_right.update(right[i].id for _, i in cands)
            elif verdict >= 0:
                i = cands[verdict][1]
                used_r.add(i)
                res.pairs.append((free_l[li], right[i], "IA"))
        return res

    # ---------- scelta tra risultati di ricerca ----------

    def choose(
        self,
        source: Track,
        candidates: list[Track],
        judge: Judge,
        tiebreak: Callable[[list[Track]], Track],
    ) -> Choice:
        cands = [c for c in candidates if c.available]
        if not cands:
            return Choice("none")
        scored = sorted(((self.score(source, c), idx, c) for idx, c in enumerate(cands)), key=lambda x: (-x[0], x[1]))

        exact = [c for _, _, c in scored if self.same_key(source, c)]
        best = scored[0][0]
        if best >= self.high or exact:
            top = max(best, self.high)
            ties = [c for s, _, c in scored if s >= top - self.tie_margin or c in exact]
            return Choice("found", tiebreak(ties), f"testo {best:.0f}")

        doubt = [c for s, _, c in scored if s >= self.low][: self.max_doubt_candidates]
        if not doubt:
            return Choice("none")
        verdict = judge(source, doubt)
        if verdict is None:
            return Choice("undecided")
        if verdict < 0:
            return Choice("none")
        return Choice("found", doubt[verdict], "IA")


def most_views(ties: list[Track]) -> Track:
    # max() mantiene il primo a parità: l'ordine di rilevanza di YouTube Music
    return max(ties, key=lambda t: t.views if t.views is not None else -1)


_ALBUM_RANK = {"album": 0, "single": 1, "compilation": 2}


def spotify_preferred(ties: list[Track]) -> Track:
    # Spotify non espone più "popularity": preferiamo l'uscita originale e più vecchia
    return min(ties, key=lambda t: (_ALBUM_RANK.get(t.album_type or "", 3), t.release_date or "9999"))
