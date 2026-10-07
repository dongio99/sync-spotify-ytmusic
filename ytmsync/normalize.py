"""Normalizzazione di titoli e artisti per il confronto testuale."""

from __future__ import annotations

import re
import unicodedata

# Parole che, se compaiono in una "coda" (tra parentesi o dopo un trattino),
# rendono quella coda rumore da eliminare. "live", "remix", "acoustic" ecc.
# NON sono qui di proposito: indicano una versione diversa del brano.
DEFAULT_TAIL_KEYWORDS = [
    r"remaster(?:ed)?",
    r"deluxe",
    r"feat\.?",
    r"ft\.?",
    r"featuring",
    r"prod\.?",
    r"produced by",
    r"with",
    r"official",
    r"music video",
    r"video(?:clip)?",
    r"video ufficiale",
    r"audio",
    r"lyrics?",
    r"testo",
    r"visuali[sz]er",
    r"hd",
    r"hq",
    r"4k",
    r"mv",
    r"m/v",
    r"explicit",
    r"clean",
    r"bonus track",
    r"expanded edition",
    r"anniversary edition",
    r"special edition",
    r"edition",
    r"single version",
    r"album version",
    r"radio edit",
    r"original mix",
    r"mono",
    r"stereo",
]

# Parole che indicano una versione diversa: una parentesi che le contiene non è un titolo alternativo
VERSION_KEYWORDS = re.compile(
    r"\b(live|remix|mix|edit|acoustic|acustic|unplugged|cover|instrumental|karaoke|demo|session|"
    r"reprise|extended|version|versione|sped|slowed|reverb|nightcore|mashup|medley|rework|bootleg)\b",
    re.IGNORECASE,
)

_SEPARATORS = re.compile(r"\s+[-–—|]\s+")
_BRACKETS = re.compile(r"[\(\[\{【]([^\)\]\}】]*)[\)\]\}】]")
_FEAT_INLINE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&|\+|/|\bx\b|\bvs\.?\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\band\b|\be\b)\s*",
    re.IGNORECASE,
)
_CHANNEL_NOISE = re.compile(r"(?:\s*-\s*topic$|vevo$|\s+official$|\s+official channel$|\s+music$)", re.IGNORECASE)


class Normalizer:
    def __init__(self, extra_keywords: list[str] | None = None):
        keywords = DEFAULT_TAIL_KEYWORDS + list(extra_keywords or [])
        self._noise = re.compile(r"(?:^|\b|\s)(?:" + "|".join(keywords) + r")(?:\b|\s|$)", re.IGNORECASE)

    def _is_noise(self, text: str) -> bool:
        return bool(self._noise.search(text))

    def strip_tails(self, title: str) -> str:
        """Toglie code promozionali/editoriali da un titolo grezzo."""
        t = unicodedata.normalize("NFKC", title or "").strip()

        # (Remastered 2011), [Official Video], (feat. X) ...
        t = _BRACKETS.sub(lambda m: " " if self._is_noise(m.group(1)) else m.group(0), t)

        # "Titolo - Remastered 2011", "Titolo - 2011 Remaster", "Titolo - Official Video":
        # si toglie un segmento finale solo se è breve o inizia con il rumore (non se è il titolo vero)
        parts = _SEPARATORS.split(t)
        while len(parts) > 1 and self._is_tail_segment(parts[-1]):
            parts.pop()

        # "Titolo feat. X" senza parentesi, segmento per segmento
        parts = [_FEAT_INLINE.sub("", p).strip() for p in parts]
        return " - ".join(p for p in parts if p).strip()

    def _is_tail_segment(self, segment: str) -> bool:
        m = self._noise.search(segment)
        if not m:
            return False
        return m.start() <= 1 or len(segment.split()) <= 4

    def text(self, value: str) -> str:
        """Normalizzazione finale: minuscolo, niente accenti, niente punteggiatura, spazi compattati."""
        v = unicodedata.normalize("NFKD", value or "")
        v = "".join(c for c in v if not unicodedata.combining(c))
        v = v.casefold().replace("&", " and ").replace("$", "s")
        v = re.sub(r"[^\w\s]", " ", v)
        v = v.replace("_", " ")
        return re.sub(r"\s+", " ", v).strip()

    def title(self, title: str) -> str:
        return self.text(self.strip_tails(title))

    def artist(self, artist: str) -> str:
        """Primo artista di una stringa che potrebbe contenerne più d'uno."""
        a = unicodedata.normalize("NFKC", artist or "").strip()
        a = _CHANNEL_NOISE.sub("", a).strip()
        first = _ARTIST_SPLIT.split(a, maxsplit=1)[0] if a else ""
        return self.text(first or a)

    def key(self, title: str, artist: str) -> str:
        return f"{self.title(title)}|{self.artist(artist)}"

    def alt_titles(self, title: str) -> list[str]:
        """Titoli alternativi: "Nel blu dipinto di blu (Volare)" -> ["Nel blu dipinto di blu", "Volare"]."""
        t = self.strip_tails(title)
        inner = [g.strip() for g in _BRACKETS.findall(t) if g.strip() and not VERSION_KEYWORDS.search(g)]
        if not inner or len(inner) != len(_BRACKETS.findall(t)):
            return []
        return [_BRACKETS.sub(" ", t).strip(), *inner]

    @staticmethod
    def split_raw(raw: str) -> tuple[str, str] | None:
        """Divide "Artista - Titolo" sul primo separatore. None se non c'è."""
        parts = _SEPARATORS.split(raw or "", maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            return None
        return parts[0].strip(), parts[1].strip()
