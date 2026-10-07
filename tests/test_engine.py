import itertools

import pytest

from ytmsync.config import Config
from ytmsync.engine import Engine
from ytmsync.models import Track
from ytmsync.retry import SafetyStop
from ytmsync.state import empty_state

_seq = itertools.count()


def S(title, artist, dur=200):
    return Track("spotify", f"spotify:track:{title}:{artist}", title, [artist], dur, album_type="album")


def Y(title, artists, dur=200, vid=None):
    vid = vid or f"v:{title}"
    return Track("ytm", vid, title, list(artists), dur, raw_title=title, video_type="MUSIC_VIDEO_TYPE_ATV")


class FakeSpotify:
    def __init__(self, tracks, catalog=()):
        self.pl = list(tracks)
        self.catalog = list(catalog)
        self.added = []

    def playlist_meta(self):
        return {"name": "Amici", "description": "", "images": []}

    def tracks(self):
        return [Track(**{**t.__dict__, "_interp": []}) for t in self.pl]

    def search(self, title, artist):
        return [Track(**{**t.__dict__, "_interp": []}) for t in self.catalog]

    def add(self, uris):
        by_id = {t.id: t for t in self.catalog}
        self.pl += [by_id[u] for u in uris]
        self.added += uris


class FakeYTM:
    def __init__(self, tracks=(), catalog=()):
        self.pl = [self._with_set(t) for t in tracks]
        self.catalog = list(catalog)
        self.created = None
        self.removed = []

    @staticmethod
    def _with_set(t):
        return Track(**{**t.__dict__, "_interp": [], "set_video_id": f"set{next(_seq)}"})

    def find_playlist(self, title):
        return "PL1" if self.created or self.pl else None

    def create_playlist(self, title, desc, privacy):
        self.created = title
        return "PL1"

    def tracks(self, pid):
        return [Track(**{**t.__dict__, "_interp": []}) for t in self.pl]

    def search(self, query, kind):
        return [Track(**{**t.__dict__, "_interp": []}) for t in self.catalog] if kind == "songs" else []

    def add(self, pid, vids):
        by_id = {t.id: t for t in self.catalog}
        self.pl += [self._with_set(by_id[v]) for v in vids]
        return list(vids)

    def remove(self, pid, tracks):
        ids = {t.set_video_id for t in tracks}
        self.removed += [t.id for t in tracks]
        self.pl = [t for t in self.pl if t.set_video_id not in ids]


@pytest.fixture
def cfg(tmp_path):
    return Config(spotify_playlist="https://open.spotify.com/playlist/ABC?si=x", root=tmp_path)


def run(cfg, sp, yt, state, valid=True, judge=lambda *_: -1):
    return Engine(cfg, sp, yt, judge, state, valid).run()


def test_full_cycle(cfg):
    a, b = S("Yesterday", "The Beatles"), S("Heroes", "David Bowie")
    ya, yb = Y("Yesterday (Remastered 2009)", ["The Beatles"]), Y("Heroes (2017 Remaster)", ["David Bowie"])
    sp = FakeSpotify([a, b])
    yt = FakeYTM([], catalog=[ya, yb])
    st = empty_state()

    # 1) Prima esecuzione: playlist YTM creata e riempita
    r = run(cfg, sp, yt, st, valid=False)
    assert yt.created == "Amici" and len(r.added_to_ytm) == 2 and len(st["pairs"]) == 2
    assert cfg.ytm_playlist_file.read_text().strip() == "PL1"

    # 2) Nessuna novità: nessuna scrittura
    r = run(cfg, sp, yt, st)
    assert not (r.added_to_ytm or r.added_to_spotify or r.removed_from_ytm)

    # 3) Qualcuno aggiunge un brano su YTM -> finisce su Spotify
    c_sp = S("Volare", "Domenico Modugno")
    yt.pl.append(FakeYTM._with_set(Y("Domenico Modugno - Volare (Official Video)", ["ModugnoVEVO"], vid="v:volare")))
    sp.catalog = [c_sp]
    r = run(cfg, sp, yt, st)
    assert sp.added == [c_sp.id] and len(r.added_to_spotify) == 1

    # 4) Rimosso da Spotify -> tolto da YTM solo alla seconda esecuzione consecutiva
    sp.pl = [t for t in sp.pl if t.id != b.id]
    r = run(cfg, sp, yt, st)
    assert not r.removed_from_ytm and len(r.pending_removal) == 1
    r = run(cfg, sp, yt, st)
    assert yt.removed == [yb.id] and len(st["pairs"]) == 2

    # 5) Brano sincronizzato sparito da YTM (non è una rimozione tua) -> ri-aggiunto, Spotify intatto
    yt.pl = [t for t in yt.pl if t.id != ya.id]
    r = run(cfg, sp, yt, st)
    assert len(r.added_to_ytm) == 1 and len(sp.pl) == 2


def test_cache_lost_never_removes(cfg):
    a = S("Yesterday", "The Beatles")
    old = Y("Heroes", ["David Bowie"])
    sp = FakeSpotify([a], catalog=[S("Heroes", "David Bowie")])
    yt = FakeYTM([Y("Yesterday", ["The Beatles"]), old])
    cfg.data_dir.mkdir()
    cfg.ytm_playlist_file.write_text("PL1")
    st = empty_state()
    r = run(cfg, sp, yt, st, valid=False)
    # Senza cache il brano solo-YTM viene trattato come nuovo: aggiunto su Spotify, mai rimosso
    assert not yt.removed and len(r.added_to_spotify) == 1


def test_safety_stop_on_empty_playlist(cfg):
    a = S("Yesterday", "The Beatles")
    ya = Y("Yesterday", ["The Beatles"])
    sp, yt = FakeSpotify([a]), FakeYTM([], catalog=[ya])
    st = empty_state()
    run(cfg, sp, yt, st, valid=False)
    sp.pl = []  # l'API restituisce una playlist vuota per errore
    with pytest.raises(SafetyStop):
        run(cfg, sp, yt, st)
    assert not yt.removed


def test_safety_stop_on_mass_removal(cfg):
    tracks = [S(f"Song {i}", f"Artist {i}") for i in range(20)]
    sp = FakeSpotify(tracks)
    yt = FakeYTM([], catalog=[Y(f"Song {i}", [f"Artist {i}"]) for i in range(20)])
    st = empty_state()
    run(cfg, sp, yt, st, valid=False)
    sp.pl = tracks[:5]
    run(cfg, sp, yt, st)  # prima volta: solo in attesa
    with pytest.raises(SafetyStop):
        run(cfg, sp, yt, st)
    assert not yt.removed


def test_undecided_doubt_is_deferred_not_added(cfg):
    a = S("Volare", "Domenico Modugno")
    sp = FakeSpotify([a])
    yt = FakeYTM([], catalog=[Y("Nel blu dipinto di blu (Volare)", ["Domenico Modugno"])])
    st = empty_state()
    r = run(cfg, sp, yt, st, valid=False, judge=lambda *_: None)
    assert not r.added_to_ytm and r.deferred and not st["not_found"]


def test_duplicate_not_added_twice(cfg):
    a = S("Yesterday", "The Beatles")
    ya = Y("Yesterday", ["The Beatles"])
    sp = FakeSpotify([a, a])  # brano doppio nella playlist Spotify
    yt = FakeYTM([], catalog=[ya])
    st = empty_state()
    r = run(cfg, sp, yt, st, valid=False)
    assert len(yt.pl) == 1 and len(r.added_to_ytm) == 1
