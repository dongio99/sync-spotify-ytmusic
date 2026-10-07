from types import SimpleNamespace

from ytmsync.judge import AIJudge, Verdict
from ytmsync.models import Track


def make(verdict_index, cache=None):
    j = AIJudge("sk-test", "claude-haiku-4-5", 2, 10, cache if cache is not None else {})
    j.calls_made = []

    def parse(**kw):
        j.calls_made.append(kw)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=Verdict(match_index=verdict_index, confidence="high", reason="ok"))

    j.client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    return j


src = Track("spotify", "sp1", "Volare", ["Domenico Modugno"], 215)
cands = [Track("ytm", "v1", "Karaoke", ["X"]), Track("ytm", "v2", "Nel blu dipinto di blu (Volare)", ["Domenico Modugno"])]


def test_judge_and_cache():
    cache = {}
    j = make(1, cache)
    assert j(src, cands) == 1
    tool = j.calls_made[0]["tools"][0]
    assert tool["type"] == "web_search_20250305" and tool["max_uses"] == 2
    assert cache == {"sp1||v1": False, "sp1||v2": True}
    # Seconda volta: dalla cache, nessuna chiamata
    j2 = make(0, cache)
    assert j2(src, cands) == 1 and not j2.calls_made


def test_judge_budget_defers():
    j = make(0)
    j.max_calls = 0
    assert j(src, cands) is None
