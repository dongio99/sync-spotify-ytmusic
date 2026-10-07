from ytmsync.matching import Matcher, most_views
from ytmsync.models import Track
from ytmsync.normalize import Normalizer
from ytmsync.ytm_side import parse_views

n = Normalizer()
m = Matcher(n)


def sp(title, artist, dur=200, id=None):
    return Track("spotify", id or f"spotify:track:{title}", title, [artist], dur)


def yt(title, artists, dur=200, id=None, views=None, vtype="MUSIC_VIDEO_TYPE_ATV"):
    return Track("ytm", id or f"v-{title}", title, artists, dur, raw_title=title, video_type=vtype, views=views,
                 set_video_id=f"set-{id or title}")


def test_strip_tails():
    assert n.title("Here Comes the Sun - Remastered 2009") == "here comes the sun"
    assert n.title("Heroes (2017 Remaster)") == "heroes"
    assert n.title("Song (feat. Someone)") == "song"
    assert n.title("Song feat. Someone") == "song"
    assert n.title("Song [Official Music Video]") == "song"
    assert n.title("Song (Lyrics)") == "song"
    assert n.title("Album Track - Deluxe Edition") == "album track"
    # Le versioni diverse restano diverse
    assert n.title("Song - Live at Wembley") == "song live at wembley"
    assert n.title("Song (Remix)") == "song remix"


def test_artist():
    assert n.artist("Beyoncé") == "beyonce"
    assert n.artist("Daft Punk, Pharrell Williams") == "daft punk"
    assert n.artist("Queen - Topic") == "queen"
    assert n.artist("TaylorSwiftVEVO") == "taylorswift"


def test_ytm_raw_title_interpretations():
    a = sp("Bohemian Rhapsody - Remastered 2011", "Queen")
    b = yt("Queen – Bohemian Rhapsody (Official Video Remastered)", ["Queen Official"], vtype="MUSIC_VIDEO_TYPE_OMV")
    assert m.same_key(a, b)


def test_score_bands():
    assert m.score(sp("Yesterday", "The Beatles"), yt("Yesterday (Remastered 2009)", ["The Beatles"])) >= 90
    live = m.score(sp("Yesterday", "The Beatles"), yt("Yesterday (Live At The BBC)", ["The Beatles"]))
    assert live < 90
    assert m.score(sp("Yesterday", "The Beatles"), yt("Hey Jude", ["The Beatles"])) < 60
    # Durata molto diversa abbassa il punteggio
    assert m.score(sp("Song", "X", 200), yt("Song", ["X"], 400)) < 90


def test_pair_uses_judge_only_for_doubts():
    calls = []

    def judge(src, cands):
        calls.append((src.title, [c.title for c in cands]))
        return 0

    left = [sp("Yesterday", "The Beatles"), sp("Volare", "Domenico Modugno"), sp("Something", "The Beatles")]
    right = [
        yt("Hey Jude", ["The Beatles"]),
        yt("The Beatles - Yesterday", ["The Beatles - Topic"]),
        yt("Nel blu dipinto di blu (Volare)", ["Domenico Modugno"]),
    ]
    res = m.pair(left, right, judge)
    pairs = {(l.title, r.title) for l, r, _ in res.pairs}
    assert ("Yesterday", "The Beatles - Yesterday") in pairs
    assert ("Volare", "Nel blu dipinto di blu (Volare)") in pairs  # deciso dall'IA
    assert [c[0] for c in calls] == ["Volare"]


def test_pair_undecided_blocks():
    left = [sp("Volare", "Domenico Modugno")]
    right = [yt("Nel blu dipinto di blu (Volare)", ["Domenico Modugno"])]
    res = m.pair(left, right, lambda *_: None)
    assert not res.pairs
    assert res.blocked_left == {left[0].id} and res.blocked_right == {right[0].id}


def test_choose_tie_most_views():
    src = sp("Song", "Artist")
    cands = [yt("Song", ["Artist"], id="a", views=1000), yt("Song", ["Artist"], id="b", views=50_000),
             yt("Other", ["Artist"], id="c", views=10**9)]
    ch = m.choose(src, cands, lambda *_: -1, most_views)
    assert ch.status == "found" and ch.track.id == "b"


def test_choose_never_picks_unconfirmed():
    src = sp("Song", "Artist")
    ch = m.choose(src, [yt("Completely Different", ["Someone"])], lambda *_: 0, most_views)
    assert ch.status == "none"


def test_parse_views():
    assert parse_views("1.4M") == 1_400_000
    assert parse_views("386M") == 386_000_000
    assert parse_views("12K") == 12_000
    assert parse_views("1,234") == 1234
    assert parse_views(None) is None


def test_iso_duration():
    from ytmsync.ytm_side import parse_iso_duration
    assert parse_iso_duration("PT4M13S") == 253
    assert parse_iso_duration("PT1H2M") == 3720
    assert parse_iso_duration("PT45S") == 45
    assert parse_iso_duration(None) is None


def test_data_api_playlist_item():
    from ytmsync.ytm_side import playlist_item_track
    song = playlist_item_track({"id": "PLI1", "snippet": {"title": "Bohemian Rhapsody", "videoOwnerChannelTitle": "Queen - Topic"},
                                "contentDetails": {"videoId": "abc"}})
    assert song.artists == ["Queen"] and song.set_video_id == "PLI1" and song.available
    assert m.same_key(sp("Bohemian Rhapsody - Remastered 2011", "Queen"), song)
    video = playlist_item_track({"id": "PLI2", "snippet": {"title": "Queen - Bohemian Rhapsody (Official Video Remastered)",
                                 "videoOwnerChannelTitle": "Queen Official"}, "contentDetails": {"videoId": "def"}})
    assert m.same_key(sp("Bohemian Rhapsody", "Queen"), video)
    gone = playlist_item_track({"id": "PLI3", "snippet": {"title": "Deleted video"}, "contentDetails": {"videoId": "x"}})
    assert gone.available is False
