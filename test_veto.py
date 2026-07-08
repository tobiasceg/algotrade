"""Tests for the veto layer's fail-closed plumbing. No network calls —
the API interaction itself gets verified with a live cloud run.

Run:  python test_veto.py
"""

import os

import veto


def make_candidate() -> dict:
    return {
        "symbol": "VRT",
        "action": "BUY",
        "signal_date": "2026-07-06",
        "close": 142.10,
        "stop": 135.80,
        "target": 151.55,
        "reason": "closed above 20-day high (140.42) at 142.1 on 1.6x average volume",
    }


def make_snapshot() -> dict:
    return {
        "date": "2026-07-07",
        "market": {"benchmark": "QQQ", "close": 722.82, "ma50": 709.88, "above_trend": True},
        "macro_events": [{"date": "2026-07-14", "event": "CPI release (Jun)", "days_away": 7}],
        "tickers": {
            "VRT": {
                "earnings_date": "2026-07-08",
                "days_to_earnings": 1,
                "news": [{"time": "t", "title": "Vertiv earnings preview", "publisher": "x"}],
            }
        },
    }


def test_parse_clean_approve():
    assert veto.parse_response('{"decision": "APPROVE", "reason": "no events"}') == (
        "APPROVE", "no events",
    )


def test_parse_clean_veto():
    d, r = veto.parse_response('{"decision": "VETO", "reason": "earnings tomorrow"}')
    assert d == "VETO" and r == "earnings tomorrow"


def test_parse_fenced_json():
    text = '```json\n{"decision": "APPROVE", "reason": "ok"}\n```'
    assert veto.parse_response(text)[0] == "APPROVE"


def test_parse_lowercase_decision_normalized():
    assert veto.parse_response('{"decision": "approve", "reason": "ok"}')[0] == "APPROVE"


def test_parse_garbage_fails_closed():
    d, r = veto.parse_response("I think this trade looks fine, go ahead!")
    assert d == "VETO" and "fail-closed" in r


def test_parse_invalid_decision_fails_closed():
    d, _ = veto.parse_response('{"decision": "MAYBE", "reason": "hmm"}')
    assert d == "VETO"


def test_no_api_key_passes_through_as_arm_a():
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        survivors, decisions = veto.review([make_candidate()], make_snapshot())
        assert [c["symbol"] for c in survivors] == ["VRT"]
        assert decisions[0]["decision"] == "SKIPPED"
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


def test_empty_candidates_no_op():
    assert veto.review([], make_snapshot()) == ([], [])


def test_payload_contains_the_decision_inputs():
    p = veto.build_payload(make_candidate(), make_snapshot())
    assert p["trade"]["symbol"] == "VRT"
    assert p["earnings"]["days_to_earnings"] == 1
    assert p["macro_events_next_7_days"][0]["event"].startswith("CPI")
    assert p["headlines_last_24h"][0]["title"] == "Vertiv earnings preview"
    # And nothing that would invite scope creep, like full price history
    assert "tickers" not in p


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)} tests passed")
