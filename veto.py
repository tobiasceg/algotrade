"""Step 4: the Claude veto layer.

Deliberately narrow: for each candidate the model is shown the trade, that
ticker's headlines, the earnings date, and the macro calendar, and may only
answer APPROVE or VETO — never suggest alternatives or modify parameters.
The design premise: LLMs are decent at "is there a specific known event in
this text?" and poor at "will this stock go up?", so only the first question
is ever asked.

Failure policy is fail-closed. An API error, timeout, or malformed reply
counts as a VETO: a bad AI day can only ever cost a missed trade, never an
unreviewed position. Structured outputs (output_config.format) make the
JSON shape server-enforced, so malformed replies should be rare.

If ANTHROPIC_API_KEY is not set, the layer is skipped entirely and all
candidates pass through — that is exactly arm A of the experiment.
"""

import json
import os

import config

SYSTEM_PROMPT = """You are a risk screen for a mechanical breakout/breakdown trading \
system. You will be shown ONE candidate trade produced by deterministic rules — a \
long entry (action BUY) or a short entry (action SELL_SHORT) — along with that \
ticker's recent headlines, its next earnings date, and upcoming macro events.

You may only APPROVE or VETO the trade. You cannot suggest alternatives, adjust \
position size, or modify the stop or target.

VETO only for concrete, identifiable risks:
- earnings within 2 trading days of entry
- a pending binary event within 2 trading days that specifically matters for this \
ticker or the whole market: FOMC rate decision, CPI release, litigation ruling, \
regulatory decision, or a scheduled product/earnings-adjacent announcement
- M&A rumors or announcements involving this ticker
- for BUY: material negative news the price may not yet reflect: guidance cuts, \
accounting problems, abrupt executive departures, loss of a major customer, \
product recalls
- for SELL_SHORT: pending positive catalysts that could gap the stock up through \
its stop: M&A interest or buyout rumors, activist stakes, announced buybacks, \
guidance raises, or signs of a crowded short squeeze

The following are NOT veto reasons:
- general nervousness about valuation, the market, or the sector
- an unknown (null) earnings date by itself
- ordinary analyst commentary, price-target changes, or opinion pieces
- the stock having already moved a lot (the rules already screened for that)

Keep the reason to one sentence naming the specific risk, or for approvals, \
confirming no disqualifying event was found."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "VETO"]},
        "reason": {"type": "string"},
    },
    "required": ["decision", "reason"],
    "additionalProperties": False,
}


def build_payload(candidate: dict, snapshot: dict) -> dict:
    """Everything the model may consider, and nothing else."""
    t = snapshot["tickers"].get(candidate["symbol"], {})
    return {
        "trade": {
            "symbol": candidate["symbol"],
            "action": candidate["action"],
            "signal_date": candidate["signal_date"],
            "close": candidate["close"],
            "stop": candidate["stop"],
            "target": candidate["target"],
            "setup": candidate["reason"],
        },
        "earnings": {
            "next_earnings_date": t.get("earnings_date"),
            "days_to_earnings": t.get("days_to_earnings"),
        },
        "macro_events_next_7_days": snapshot.get("macro_events", []),
        "market": snapshot.get("market", {}),
        "headlines_last_24h": t.get("news", []),
    }


def parse_response(text: str) -> tuple[str, str]:
    """Parse the model's JSON. Anything unexpected -> VETO (fail closed)."""
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned[cleaned.index("{"):]
        data = json.loads(cleaned)
        decision = str(data["decision"]).upper()
        if decision not in ("APPROVE", "VETO"):
            raise ValueError(f"bad decision {decision!r}")
        return decision, str(data.get("reason", ""))
    except Exception as exc:  # noqa: BLE001 — any parse failure fails closed
        return "VETO", f"malformed model response (fail-closed): {exc}"


def review(candidates: list[dict], snapshot: dict) -> tuple[list[dict], list[dict]]:
    """Run each candidate past Claude. Returns (survivors, decisions).

    decisions records every call for the journal: {symbol, decision, reason}.
    The survivors list is always a subset of candidates — this layer can
    only ever shrink it.
    """
    if not candidates:
        return [], []

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[veto] no ANTHROPIC_API_KEY — veto layer OFF, all candidates pass (arm A)")
        decisions = [
            {"symbol": c["symbol"], "decision": "SKIPPED", "reason": "veto layer off (no API key)"}
            for c in candidates
        ]
        return list(candidates), decisions

    import anthropic

    client = anthropic.Anthropic()
    survivors: list[dict] = []
    decisions: list[dict] = []

    for c in candidates:
        payload = build_payload(c, snapshot)
        try:
            response = client.messages.create(
                model=config.VETO_MODEL,
                max_tokens=config.VETO_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                messages=[
                    {
                        "role": "user",
                        "content": "Candidate trade for review:\n"
                        + json.dumps(payload, indent=1, sort_keys=True),
                    }
                ],
            )
            if response.stop_reason == "refusal":
                decision, reason = "VETO", "model refused the request (fail-closed)"
            else:
                text = next(b.text for b in response.content if b.type == "text")
                decision, reason = parse_response(text)
        except Exception as exc:  # noqa: BLE001 — API problems fail closed
            decision, reason = "VETO", f"API error (fail-closed): {exc}"

        decisions.append({"symbol": c["symbol"], "decision": decision, "reason": reason})
        print(f"[veto] {c['symbol']}: {decision} — {reason}")
        if decision == "APPROVE":
            survivors.append(c)

    return survivors, decisions
