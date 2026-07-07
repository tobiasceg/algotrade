"""Step 5: hard guardrails. Code — never the model — enforces the limits.

This runs AFTER the AI veto layer (step 4), which means even a bad AI day,
a malformed AI response, or a bug upstream can only ever cost a missed
trade, never an oversized or off-whitelist position.

Pure function of (candidates, account) so tests and dry runs can pass a
fake account. Account shape:

    {"equity": float, "cash": float,
     "positions": {symbol: {...}},          # currently held
     "open_order_symbols": [symbol, ...],   # pending orders
     "buys_today": int}                     # entries already placed today
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
    buys = account["buys_today"]
    committed = set(account["positions"]) | set(account.get("open_order_symbols", []))

    def reject(c: dict, reason: str) -> None:
        rejected.append({"symbol": c["symbol"], "reason": reason})
        print(f"[guardrails] REJECT {c['symbol']}: {reason}")

    for c in candidates:
        symbol = c["symbol"]

        if symbol not in config.WATCHLIST:
            reject(c, "not on the whitelist")
            continue

        stop, target, close = c.get("stop"), c.get("target"), c.get("close")
        if not stop or not target or not (stop < close < target):
            reject(c, f"malformed stop/target (stop={stop}, close={close}, target={target})")
            continue

        if symbol in committed:
            reject(c, "already holding a position or open order")
            continue

        if buys >= config.MAX_NEW_TRADES_PER_DAY:
            reject(c, f"daily cap of {config.MAX_NEW_TRADES_PER_DAY} new trades reached")
            continue

        # Marketable DAY limit slightly above the signal close: fills on a
        # normal open, dies unfilled if the stock gapped past the setup.
        limit_price = round(close * (1 + config.MAX_ENTRY_SLIP_PCT / 100), 2)

        budget = equity * config.MAX_POSITION_PCT
        qty = int(budget // limit_price)
        if qty < 1:
            reject(c, f"one share at {limit_price} exceeds the {config.MAX_POSITION_PCT:.0%} position budget")
            continue

        # Cash floor: shrink the position to fit, and if even one share
        # would breach the floor, skip the trade entirely.
        floor = equity * config.CASH_FLOOR_PCT
        if cash - qty * limit_price < floor:
            qty = int((cash - floor) // limit_price)
            if qty < 1:
                reject(c, f"cash floor ({config.CASH_FLOOR_PCT:.0%} of equity) leaves no room")
                continue
            print(f"[guardrails] {symbol}: position shrunk to {qty} shares to respect cash floor")

        cost = round(qty * limit_price, 2)
        approved.append({**c, "qty": qty, "limit_price": limit_price, "est_cost": cost})
        buys += 1
        cash -= cost
        committed.add(symbol)
        print(f"[guardrails] APPROVE {symbol}: {qty} shares @ <={limit_price} (~{cost})")

    return approved, rejected
