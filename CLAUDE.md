# CLAUDE.md: LTP Crossing project context (read at the start of every session)

Repo: `ArunkumarEkambaram/LTP_Crossing` (default branch `master`).
Run: `python app.py` → http://localhost:8082. Needs the packages in `requirements.txt` (NiceGUI, requests, openpyxl, pytz).
Related repo (reference only, do not change): `ArunkumarEkambaram/BEP-AlgoDelta` (its `replay.py` was the model for this
project's Replay).

## What the app is
A NiceGUI app that watches NIFTY (and optionally SENSEX) weekly options for **LTP Crossing (CE = PE)**: inside a small
window of strikes nearest to the index, a strike's rounded CE price equal to its own rounded PE price is a match. Data comes from the
Upstox API. **Only CE = PE is in scope:** BEP Support, AlgoDelta auto-orders and Telegram were removed on purpose
(PR #1). Don't add them back unless the user asks.

## Working rules agreed with the user
- Work on a branch, open a PR, and merge **only when the user says "merge the pull request"**. No direct pushes to `master`.
- After a merge the user runs `git checkout master` and `git pull`, then restarts the app.
- `__pycache__/` and `*.pyc` are git-ignored; never commit them. `.gitignore` also keeps `/Working Project_13_10_2026`.
- The cloud session can't delete GitHub branches (proxy 403). The user deletes merged branches on GitHub.
- There is no Upstox token in the cloud session: test with a fake client (see "Testing") and say that real data is untested.
- When a new rule is ambiguous, ask the user first with a few options and a recommended one, then build what they pick
  (worked well for the 6-candle watch). Their numbers in messages are usually **examples**, not fixed levels
  (e.g. "25091–25110" meant "near any 50 / 100 strike").
- The user checks results against TradingView 1-minute charts and replays past days on market holidays.

## Rules of the CE = PE scan
- **Match rule** (`match_logic.py`, shared by live, replay and backtest):
  - **Same strike only:** each strike's own CE vs its own PE (the user was explicit: "CE and PE should be in the same
    strike price"). No cross-strike matches (they caused far too many matches before PR #4).
  - **Strikes checked:** nearest strike to the index LTP ± `STRIKES_EACH_SIDE` (default **1** → 3 strikes; side panel
    slider 1/3/5/7). Strike step: NIFTY 50, SENSEX 100. Re-centred every `RECALC_INTERVAL_SECONDS` (60 s).
  - Both prices rounded half-up (130.50 → 131); equal rounded prices = a match.
  - A strike's CE only equals its PE when the market (futures) is right at that strike, so this fires only "near the
    50s / 100s", which is what the user wants.
  - **Don't gate on a spot-NIFTY band.** PR #4 tried "strike − 10 < spot ≤ strike + 10" and missed a real crossing:
    20 Nov 2026 09:26, 25400 CE = PE ≈ 140 while spot NIFTY was 25416–25440. CE = PE happens when the *future* is
    at the strike, and spot can sit 20–40 points away. PR #5 removed the band.
- **Scan:** every `SCAN_INTERVAL_SECONDS` (10 s, side panel), only during market hours (`MARKET_OPEN` 09:15 –
  `MARKET_CLOSE` 15:40 IST, weekdays, not `MARKET_HOLIDAYS`).
- **Cooldown:** after a match the window freezes and further matches are hidden for `COOLDOWN_SECONDS` (5 min). All
  matches found in the same scan are recorded.
- **Follow-up check** (`tracker.py`, PR #6, "wait for 6 candles like BEP-AlgoDelta"): each match is watched for
  `TRACK_WATCH_CANDLES` = 6 five-minute candles (09:15 grid): the match candle + the next 5 (match 09:26 → 09:25 …
  09:50, result after 09:55 = "Checked Till"). Row shows `WATCHING n/6` meanwhile. Rule "breakout then hold":
  - above = close > P, red = open > close (flat = green).
  - First candle where a leg closes above P: in a RED candle → **Invalid (red)**, done; in a GREEN candle → that leg is
    the breakout leg (both green above: the one further above P).
  - Then the breakout leg closing below P → **Failed · hh:mm**, done.
  - After the 6th candle: breakout held → **Valid**; no breakout → **None**; every candle missing → **No data**.
  - **Best move** = breakout leg's highest high − P over the watched candles. Columns: Breakout, Best move, Result.
  - A candle still missing `TRACK_GIVE_UP_SECONDS` after it closed is skipped (still counted).
  - Colours: Valid green; Invalid / Failed / None / No data red; WATCHING plain.
- **Alerts** (`notifier.py`): on-screen toast + browser notification per match, only if `MATCH_ALERTS_ENABLED` (True)
  and not paused (Pause alerts button, `ALERTS_PAUSED`). No Telegram.

## Files
- `app.py`: the UI (single page, port 8082).
  - Side panel: Upstox token (Save & Test Connection), Scanner (strike count, Rescan, scan interval).
  - Top bar: Pause alerts, NIFTY / SENSEX checkboxes.
  - Tabs: **Live Scanner** (per index: LTP, "Strikes: 25400, 25450, 25500", CE = PE on/off, matches table,
    CSV/XLSX), **Replay**, **Backtest**.
  - Live matches table: Time, CE Strike, PE Strike, Rounded Price, Checked Till, Breakout, Best move, Result.
  - Rescan: unfreezes the cooldown and re-centres the window on the next scan.
  - Log panel at the bottom: repeated messages within the last 20 lines merge into one line with an `(xN)` counter;
    20 lines shown, 200 kept.
- `scanner.py`: `run_scan_cycle(client, state_mgr, ..., now=None)`, one scan of one index. `now` is used by replay.
- `state_manager.py`: window re-centring, freeze / cooldown (one `StateManager` per index).
- `match_logic.py`: the match rule. `models.py`: `StrikeWindow`, `MatchEvent`, `ScanState`.
- `tracker.py`: the 6-candle follow-up watch (breakout then hold).
- `replay.py`: **Replay a past day** (see below).
- `backtest.py`: Backtest tab, NIFTY only. Lists every raw same-strike crossing on 1-minute closes over each minute's
  window (same rule as live), **cooldown ignored, no 6-candle watch** (use Replay for results).
- `market_utils.py`: market hours, strike window, expiry helpers (`expiry_cycle_dates` = days that can be replayed),
  cached option instrument keys.
- `upstox_client.py`: Upstox REST calls (LTP, option chain, contracts, 1-minute / N-minute / daily candles).
- `notifier.py`: toasts + browser notifications. `export_utils.py`: CSV / XLSX downloads.
- `config.py`: all settings above. `test_option_chain.py`: standalone check of the option-chain response.

## Replay (PR #2)
- Replay tab: index + date (Load dates lists the current weekly expiry cycle) → Replay. No alerts; the live scan keeps
  running.
- `ReplayClient` wraps the Upstox client for one day, and the **live code** runs on it, one scan per 1-minute candle
  09:15–15:29: index close = LTP (re-centres the window), each window strike's 1-minute CE / PE closes = LTPs (last close
  carried forward if it didn't trade that minute), same cooldown, then `tracker.process_pending` (6-candle watch) on
  that day's 5-minute candles (today: only closed candles, so late matches stay WATCHING). Keep replay going
  through the live code so the two never drift apart.
- Table: Time, CE/PE strike, CE/PE LTP, Rounded Price, Index LTP, Checked Till, Breakout, Best move, Result.
  CSV/XLSX download.
- Limits: only days of the current weekly expiry (older contracts' keys are no longer listed by Upstox); one price per
  minute, so a crossing lasting only seconds inside a minute is missed (live scans every 10 s).

## Testing (no Upstox token in the cloud)
- Unit-test logic with a fake client class that returns made-up candles / contracts / expiries.
- UI check: run the app with `upstox_client.UpstoxClient` and `upstox_client.test_connection` patched to the fake, then
  drive it with Playwright (Chromium is pre-installed) and look at a screenshot. Patch `market_utils.is_market_open`
  to `True` to see the live scan outside market hours.
- The cloud clock is UTC. Live `MatchEvent.triggered_at` is a naive `datetime.now()` that the app treats as local time
  (IST on the user's PC), so times like "Checked Till" look 5:30 h off in the cloud only. Not a bug for the user.
- Upstox (`api.upstox.com`) is blocked from the cloud session (proxy 403), even for historical candles.

## History (PRs, all merged into `master`)
1. **#1** Kept only LTP Crossing: removed AlgoDelta, BEP Support (boards, cards, uploads, support backtest), Telegram.
   Log panel trimmed (repeats merged with `(xN)`), CE = PE alerts on, `__pycache__` untracked.
2. **#2** Replay tab (`replay.py`, `ReplayClient`), `scanner.run_scan_cycle(now=...)`, `market_utils.expiry_cycle_dates`.
3. **#3** This `CLAUDE.md`.
4. **#4** Same-strike matching only + a ±10 spot-NIFTY band (band later removed).
5. **#5** Removed the band; same-strike CE = PE on the nearest ± 1 strikes; strike count slider back.
   The user confirmed the 20 Nov 09:26 25400 match now shows.
6. **#6** 6-candle watch (breakout then hold), Breakout / Best move / Result columns, WATCHING n/6.

## Open / not yet verified
- 6-candle results on real Upstox data (e.g. 20 Nov 09:26, 25400 @ 140): needs the user's token / their check.
- Backtest is NIFTY only and has no 6-candle watch; Replay supports NIFTY and SENSEX.
