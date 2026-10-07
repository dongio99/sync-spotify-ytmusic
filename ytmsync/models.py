from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Track:
    platform: str  # "spotify" | "ytm"
    id: str  # Spotify URI oppure YouTube videoId
    title: str
    artists: list[str]
    duration_s: int | None = None
    # Solo YouTube Music
    raw_title: str | None = None
    video_type: str | None = None
    set_video_id: str | None = None
    views: int | None = None
    available: bool = True
    # Solo Spotify
    album_type: str | None = None
    release_date: str | None = None
    # Interpretazioni (titolo, artista) normalizzate, calcolate da matching.interpretations()
    _interp: list[tuple[str, str]] = field(default_factory=list, repr=False, compare=False)

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    def describe(self) -> str:
        parts = [f"titolo: {self.title!r}", f"artisti: {', '.join(self.artists) or '?'}"]
        if self.duration_s:
            parts.append(f"durata: {self.duration_s // 60}:{self.duration_s % 60:02d}")
        if self.video_type:
            kind = {
                "MUSIC_VIDEO_TYPE_ATV": "brano audio ufficiale",
                "MUSIC_VIDEO_TYPE_OMV": "videoclip ufficiale",
                "MUSIC_VIDEO_TYPE_UGC": "video caricato da utente",
                "YOUTUBE_VIDEO": "video YouTube (canale indicato come artista)",
            }.get(self.video_type, self.video_type)
            parts.append(f"tipo: {kind}")
        if self.album_type:
            parts.append(f"tipo uscita: {self.album_type}")
        return "; ".join(parts)
