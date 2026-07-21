# Trading Bot — Project Status

> Session summary, written 2026-07-11 (SGT). Repo: https://github.com/tobiasceg/algotrade

## 1. GOAL

A fully automated **paper-trading experiment** testing one question: *does an AI veto
layer earn its place in a mechanical trading system?*

The system trades a 24-name AI-infrastructure watchlist (+ QQQ as regime benchmark)
on Alpaca paper money, orchestrated by GitHub Actions (free), with Telegram alerts
and an append-only decision journal. Three arms share the same data, rules, and
guardrails:

- **Arm A** — deterministic breakout rules only (current live state)
- **Arm B** — rules + Claude veto layer (activates when `ANTHROPIC_API_KEY` secret is added)
- **Arm C** — single open-ended Claude call replacing rules+veto (not built yet)

Design principle throughout: **rules propose, AI can only subtract, code enforces
limits.** Every layer can fail without the failure compounding. Exits are fully
mechanical (bracket orders + time stop) because they run while nobody is watching.

## 2. CURRENT STATE

- **All 7 pipeline steps are built, tested, and live** on `main`:
  scheduler → data fetch → rules engine → Claude veto → guardrails → execution/alerts/journal.
- **Secrets configured:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`. **Not yet:** `ANTHROPIC_API_KEY` → bot currently runs as **arm A**
  (veto layer skips itself and passes candidates through, labeled in journal).
- **Portfolio:** $100k paper, 0 positions, no trades yet — QQQ has been below its
  50-day MA since Jul 8 (risk-off), so the regime filter correctly blocks all entries.
- **Runs:** entry decision targets 10:00 ET (10 PM SGT), mechanical exit pass targets
  14:15 ET (2:15 AM SGT). Nominal cadence: one entry + one exit Telegram ping per
  trading day; 65 tests green across 4 suites (`python test_*.py`).
- **Scheduler reliability saga:** GitHub Actions cron ran 1–3.5h late all week and
  *dropped* some events entirely. Current countermeasures (all live): crons aim 4–5h
  early on odd minutes + `main.py` sleeps to the target time; staggered backup crons;
  journal-based once-per-day dedupe; wide guard windows as last resort; serialized
  concurrency. **First bullseye achieved:** Jul 10 exit ran at exactly 14:15:00 ET.
  Jul 21: closed the stale-checkout race (Jul 20 duplicate entry Telegram) with
  `journal.jsonl merge=union` in `.gitattributes` + a `git pull --rebase` step
  right before the bot runs.
- **Timing probes live since Jul 11:** crons now fire 7 days/week; on market-closed
  days each firing logs `timing_probe` (slot, actual time, lag minutes) to the journal
  instead of exiting silently — building the lag/drop dataset for the scheduler decision.
- **Short book added Jul 18** (`short_rules.py`): mirror of the long rules, active only
  when QQQ is ≥1% BELOW its 50d MA (hysteresis; the two books are mutually exclusive).
  Deliberate asymmetries: half position size (5%), tighter extension cap (4% below the
  20d low), 25% crash-from-high cap, mechanical earnings block (5d, fail-closed on
  unknown dates), 3-day time stop, Alpaca shortable/easy-to-borrow check at submission.
  Same veto layer, guardrails, brackets (stop above, target below), journal (`book`/
  `side` fields). Longs always take precedence: shorts are only consulted when the
  long book returns nothing.

## 3. FILES IN FLIGHT

| File | Role |
|---|---|
| `.github/workflows/trading-bot.yml` | Scheduler: 8 daily crons (4 entry, 4 exit), mode mapping, secrets, journal persist, snapshot artifacts |
| `main.py` | Entrypoint: guard (holiday/dedupe/window), sleep-to-target, entry & exit pipelines, Telegram composition, timing probes |
| `config.py` | Watchlist + every tunable (rule thresholds, guardrail limits, veto model, hold days) |
| `data_fetch.py` | Daily snapshot: yfinance bars (partial-bar-safe), indicators incl. ATR, earnings, 24h news, macro calendar |
| `rules.py` | Deterministic entry rules → ranked candidates with ATR stops/targets |
| `short_rules.py` | Short-side mirror: breakdown rules, risk-off regime gate with 1% hysteresis, crash cap, mechanical earnings block |
| `veto.py` | Claude APPROVE/VETO screen (claude-opus-4-8, structured outputs, fail-closed; skips = arm A) |
| `guardrails.py` | Pure-code limits: whitelist, 10%/position, 2 trades/day, 20% cash floor, sizing |
| `broker.py` | Alpaca paper: bracket orders (limit +2% cap), exit checks (5-day time stop, stop audit/re-attach) |
| `notify.py` | Telegram, fail-soft |
| `journal.py` | Append-only `journal.jsonl` + `already_ran` dedupe + `last_order_for` |
| `macro_calendar.json` | 2026 FOMC/CPI dates (verified vs Fed/BLS) — refresh each January |
| `test_rules.py`, `test_short_rules.py`, `test_guardrails.py`, `test_veto.py` | Synthetic-data suites, plain `python file.py` |
| `journal.jsonl` | Decision log; committed back by the workflow after every real run |
| `snapshots/` | Daily JSON snapshots (gitignored locally; 90-day artifacts in CI) |

## 4. CHANGED THIS SESSION

Everything — the project went from empty repo to live system in one session:

1. **Step 1** scheduler + market-aware guard (`26f2f35`)
2. **Step 2** data fetch layer, verified macro calendar (`d9c4e6e`)
3. **Step 3** rules engine + ATR stops + tests (`3a3a95e`)
4. **Steps 5+6** guardrails, Alpaca brackets, Telegram, journal (`dca8c6c`)
5. **Step 4** Claude veto layer + tests (`c21584a`)
6. **Scheduler fix v1:** wide windows + journal dedupe + concurrency (`4a7a7ce`)
7. **Scheduler fix v2:** aim early + sleep to target, odd minutes, backups (`261a6cc`)
8. **Scheduler fix v3:** aim 4–5h early after 3.5h observed lag (`38d1d84`)
9. **Timing probes** on market-closed days (`df2a05d`)

Plus operational: branch renamed `master`→`main`, repo-local git identity set,
GitHub secrets wired, repo pushed and crons activated.

## 5. FAILED ATTEMPTS

- **Narrow entry window (9:45–10:45 ET)** — assumed GitHub cron is ≤15 min late.
  Reality: 2–2.5h late on Jul 8; both entry firings landed outside the window and were
  skipped as if they were DST duplicates → missed day. *Lesson: don't use window
  narrowness for duplicate detection; that's what the journal dedupe is for.*
- **Top-of-hour cron slots (14:00/15:00 UTC)** — the most congested queue times.
  Moved to odd minutes (:07/:37). Helps marginally; does not fix lag.
- **Aim-early v1 (2h early, 3.5h sleep cap)** — Jul 10 lag exceeded even that;
  the day was saved only by the late-backup cron (fired 4 min after its slot while
  the primaries sat in queue 2.5h+). *Lesson: lag is unbounded and events can be
  **dropped outright** (Jul 9's exit events were never processed; that exit was missed
  — harmless, zero positions). Redundancy beats aim.*
- **Assuming GitHub cron is a clock.** It is a best-effort queue. All scheduler
  engineering this week flows from that mistake.
- Minor: `pip` not on PATH on this machine (use `python -m pip`); a stale test
  assertion after raising `MAX_WAIT` to 5h (5:00 AM ET became a legal sleep).

## 6. NEXT

1. **Monday Jul 13, ~10 PM SGT:** first full test of the aim-early schedule for the
   entry run. Judge punctuality over Mon–Wed.
2. **Analyze weekend probe data** (ask Claude: "tally the timing probes") — median/p95
   lag and dropped-slot count per cron. This decides the **KIV'd scheduler question**:
   if drops are frequent → move the trigger off GitHub (cron-job.org hitting
   `workflow_dispatch` with a fine-grained PAT, ~10 min setup) or a ~$5/mo VPS.
   Bot code is trigger-agnostic; nothing else changes.
3. **Add `ANTHROPIC_API_KEY` secret** (console.anthropic.com) → flips arm A to arm B.
   First live veto call happens the first day a candidate fires with the key present.
4. **Build arm C** — one open-ended Claude call replacing rules+veto, same guardrails.
5. **Step 7 monthly review script** — read `journal.jsonl`: returns vs QQQ, drawdown,
   win rate, and the **veto audit** (trades B skipped that A took — did skipping help?).
6. Watch items: repo must **stay public** (sleeping runners are only free there);
   refresh `macro_calendar.json` in January 2027; QQQ regime flip will produce the
   first real candidates — verify the first live bracket order looks sane.
