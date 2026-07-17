"""Step 5: hard guardrails. Code — never the model — enforces the limits.

This runs AFTER the AI veto layer (step 4), which means even a bad AI day,
a malformed AI response, or a bug upstream can only ever cost a missed
trade, never an oversized or off-whitelist position.

Handles both books: long candidates (side "long", stop < close < target)
and short candidates (side "short", target < close < stop). Shorts get half
the position budget — gaps go through stops and losses are unbounded above.

Pure function of (candidates, account) so tests and dry runs can pass a
fake account. Account shape:

    {"equity": float, "cash": float,
     "positions": {symbol: {...}},          # currently held
     "open_order_symbols": [symbol, ...],   # pending orders
     "entries_today": int}                  # entries already placed today
                                            # (long and short share the cap)
"""

import config


def apply(candidates: list[dict], account: dict) -> tuple[list[dict], list[dict]]:
    """Returns (approved orders with qty/limit price, rejections with reasons).

    Candidates are processed in the order given (rules engine ranks them
    strongest-first), so when the daily cap bites it keeps the best setups.
    """
    approved: list[dict] = []
    rejected: list[dict] = []

    equity = account["equity"]
    cash = account["cash"]
    entries = account["entries_today"]
    committed = set(account["positions"]) | set(account.get("open_order_symbols", []))

    def reject(c: dict, reason: str) -> None:
        rejected.append({"symbol": c["symbol"], "reason": reason})
        print(f"[guardrails] REJECT {c['symbol']}: {reason}")

    for c in candidates:
        symbol = c["symbol"]
        side = c.get("side", "long")

        if symbol not in config.WATCHLIST:
            reject(c, "not on the whitelist")
            continue

        stop, target, close = c.get("stop"), c.get("target"), c.get("close")
        levels_ok = (
            stop and target and close
            and (
                (side == "long" and stop < close < target)
                or (side == "short" and 0 < target < close < stop)
            )
        )
        if not levels_ok:
            reject(c, f"malformed stop/target for {side} (stop={stop}, close={close}, target={target})")
            continue

        if symbol in committed:
            reject(c, "already holding a position or open order")
            continue

        if entries >= config.MAX_NEW_TRADES_PER_DAY:
            reject(c, f"daily cap of {config.MAX_NEW_TRADES_PER_DAY} new trades reached")
            continue

        # Marketable DAY limit slightly beyond the signal close: fills on a
        # normal open, dies unfilled if the stock gapped past the setup.
        # Longs buy up to +slip; shorts sell down to -slip.
        slip = config.MAX_ENTRY_SLIP_PCT / 100
        limit_price = round(close * (1 - slip if side == "short" else 1 + slip), 2)

        max_pct = config.MAX_SHORT_POSITION_PCT if side == "short" else config.MAX_POSITION_PCT
        budget = equity * max_pct
        qty = int(budget // limit_price)
        if qty < 1:
            reject(c, f"one share at {limit_price} exceeds the {max_pct:.0%} position budget")
            continue

        # Cash floor: shrink the position to fit, and if even one share
        # would breach the floor, skip the trade entirely. For shorts the
        # committed amount is a conservative margin proxy, not spent cash.
        floor = equity * config.CASH_FLOOR_PCT
        if cash - qty * limit_price < floor:
            qty = int((cash - floor) // limit_price)
            if qty < 1:
                reject(c, f"cash floor ({config.CASH_FLOOR_PCT:.0%} of equity) leaves no room")
                continue
            print(f"[guardrails] {symbol}: position shrunk to {qty} shares to respect cash floor")

        cost = round(qty * limit_price, 2)
        approved.append({**c, "qty": qty, "limit_price": limit_price, "est_cost": cost})
        entries += 1
        cash -= cost
        committed.add(symbol)
        word = "SELL SHORT" if side == "short" else "BUY"
        print(f"[guardrails] APPROVE {symbol}: {word} {qty} shares (limit {limit_price}, ~{cost})")

    return approved, rejected
