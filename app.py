"""
app.py — NiceGUI entry point for the LTP Crossing (CE = PE) scanner.
"""
from datetime import datetime
from nicegui import ui, run

import config
import upstox_client
import notifier
import telegram_bot
import market_utils
import backtest
import export_utils
import scanner
import tracker
from state_manager import StateManager

state_mgrs = {name: StateManager(name) for name in config.INDEXES}   # CE = PE scanner state: one per index
client = upstox_client.UpstoxClient(config.UPSTOX_ACCESS_TOKEN)

log_lines = []
log_area = None
token_connected = False

MATCH_COLUMNS = [
    {"name": "time", "label": "Time", "field": "time"},
    {"name": "ce_strike", "label": "CE Strike", "field": "ce_strike"},
    {"name": "pe_strike", "label": "PE Strike", "field": "pe_strike"},
    {"name": "ce_ltp", "label": "CE LTP", "field": "ce_ltp"},
    {"name": "pe_ltp", "label": "PE LTP", "field": "pe_ltp"},
    {"name": "price", "label": "Rounded Price", "field": "price"},
    {"name": "comment", "label": "Comment", "field": "comment"},
]
EXPORT_COLUMNS = [("time", "Time"), ("ce_strike", "CE Strike"),
                  ("pe_strike", "PE Strike"), ("ce_ltp", "CE LTP"), ("pe_ltp", "PE LTP"),
                  ("price", "Rounded Price"), ("comment", "Comment")]

LIVE_MATCH_COLUMNS = [
    {"name": "time", "label": "Time", "field": "time"},
    {"name": "ce_strike", "label": "CE Strike", "field": "ce_strike"},
    {"name": "pe_strike", "label": "PE Strike", "field": "pe_strike"},
    {"name": "price", "label": "Rounded Price", "field": "price"},
    {"name": "check_till", "label": "Checked Till", "field": "check_till"},
    {"name": "closed_above", "label": "Closed Above", "field": "closed_above"},
]
LIVE_EXPORT_COLUMNS = [(c["name"], c["label"]) for c in LIVE_MATCH_COLUMNS]

COMMENT_CELL_SLOT = '''
    <q-td key="comment" :props="props">
        <q-input v-model="props.row.comment" dense placeholder="notes..."
            @blur="() => $parent.$emit('%(event_name)s', props.row)" />
    </q-td>
'''


def make_comment_sync_handler(table):
    def handler(e):
        updated_row = e.args
        for row in table.rows:
            if row.get("id") == updated_row.get("id"):
                row["comment"] = updated_row.get("comment", "")
                break
    return handler


LOG_SHOW_LINES = 20       # lines visible in the log panel
LOG_KEEP_LINES = 200      # lines kept in memory
LOG_DEDUPE_LOOKBACK = 20  # a message repeating within this many recent lines is merged, not re-added


def _log_body(line: str) -> str:
    """Message text without the leading "[HH:MM:SS] " stamp."""
    if line.startswith("[") and "] " in line[:12]:
        return line.split("] ", 1)[1]
    return line


def log(msg: str):
    # Repeated messages (e.g. the same error every scan) are merged into one line with a "(xN)" counter
    # and the latest timestamp, instead of flooding the panel.
    body = _log_body(msg)
    for i in range(len(log_lines) - 1, max(-1, len(log_lines) - 1 - LOG_DEDUPE_LOOKBACK), -1):
        if log_lines[i]["body"] == body:
            entry = log_lines.pop(i)
            entry["count"] += 1
            entry["text"] = msg
            log_lines.append(entry)
            break
    else:
        log_lines.append({"body": body, "text": msg, "count": 1})
    del log_lines[:-LOG_KEEP_LINES]
    if log_area is not None:
        log_area.set_content("\n".join(
            e["text"] + (f" (x{e['count']})" if e["count"] > 1 else "") for e in log_lines[-LOG_SHOW_LINES:]))

def fetch_index_ltps(names):
    """Spot LTP of each index in `names` (one API call). Returns ({name: ltp}, error or None)."""
    keys = {config.INDEXES[n]["index_key"]: n for n in names}
    ltp_map, err = client.fetch_ltp(list(keys))
    if not ltp_map:
        return {}, err
    out = {keys[k]: v for k, v in ltp_map.items() if k in keys}
    if not out and len(names) == 1 and len(ltp_map) == 1:      # key came back in another format
        out[names[0]] = next(iter(ltp_map.values()))
    return out, err

@ui.page("/")
def main_page():
    global log_area
    panels = {}          # index name -> its live-tab widgets (filled when the live tab is built)

    ui.add_head_html('''
        <style>
            .q-btn { border-radius: 8px !important; transition: all 0.2s ease-in-out !important; }
            .q-btn:hover { transform: translateY(-1px); box-shadow: 0 4px 6px rgba(0,0,0,0.15); filter: brightness(0.95); }
        </style>
    ''')

    ui.run_javascript("""
        if (Notification.permission !== "granted" && Notification.permission !== "denied") {
            Notification.requestPermission();
        }
    """)

    with ui.left_drawer(value=True, fixed=False).classes("bg-gray-100 p-4") as drawer:
        ui.label("Telegram Settings").classes("text-lg font-bold")
        tg_token_input = ui.input("Bot Token", value=config.TELEGRAM_BOT_TOKEN, password=True, password_toggle_button=True).classes("w-full")
        tg_chat_id_input = ui.input("Chat ID(s) — comma-separated", value=config.TELEGRAM_CHAT_ID).classes("w-full")
        tg_status_label = ui.label("Status: not configured" if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID) else "Status: unverified")

        def save_and_test_telegram():
            bot_token = tg_token_input.value.strip()
            chat_id_raw = tg_chat_id_input.value.strip()
            if not bot_token or not chat_id_raw:
                tg_status_label.set_text("Status: bot token and chat ID(s) both required")
                config.TELEGRAM_ENABLED = False
                return
            config.TELEGRAM_BOT_TOKEN = bot_token
            config.TELEGRAM_CHAT_ID = chat_id_raw
            all_ok, results = telegram_bot.test_telegram(bot_token, chat_id_raw)
            succeeded = [cid for cid, ok, _ in results if ok]
            failed = [(cid, err) for cid, ok, err in results if not ok]
            config.TELEGRAM_ENABLED = len(succeeded) > 0

            if all_ok:
                tg_status_label.set_text(f"Status: connected — test sent to {len(succeeded)} chat ID(s)")
                log(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram saved and verified ({len(succeeded)} chat ID(s)).")
            else:
                fail_txt = "; ".join(f"{cid} ({err})" for cid, err in failed)
                if succeeded:
                    tg_status_label.set_text(f"Status: partial — sent to {len(succeeded)}, failed: {fail_txt}")
                else:
                    tg_status_label.set_text(f"Status: failed for all — {fail_txt}")
                log(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram test — failures: {fail_txt}")

        ui.button("Save & Test Telegram", on_click=save_and_test_telegram).classes("w-full")
        
        ui.separator()
        ui.label("Upstox").classes("text-lg font-bold")
        with ui.column().classes("w-full gap-1"):
            token_input = ui.input("Upstox Access Token", value=config.UPSTOX_ACCESS_TOKEN, password=True, password_toggle_button=True).classes("w-full")
            connection_label = ui.label("Status: not connected" if not config.UPSTOX_ACCESS_TOKEN else "Status: unverified")

        def save_and_test_token():
            global token_connected
            token = token_input.value.strip()
            if not token:
                connection_label.set_text("Status: token is empty")
                token_connected = False
                return
            client.access_token = token
            client.headers["Authorization"] = f"Bearer {token}"
            ok, info = upstox_client.test_connection(token)
            if ok:
                connection_label.set_text(f"Status: connected ({info})")
                token_connected = True
                log(f"[{datetime.now().strftime('%H:%M:%S')}] Upstox token saved and verified.")
            else:
                connection_label.set_text(f"Status: connection failed — {info}")
                token_connected = False
                log(f"[{datetime.now().strftime('%H:%M:%S')}] Token test failed: {info}")

        ui.button("Save & Test Connection", on_click=save_and_test_token).classes("w-full")

        ui.separator()
        ui.label("Scanner").classes("text-lg font-bold")
        with ui.column().classes("w-full gap-1"):
            default_total_strikes = 2 * config.STRIKES_EACH_SIDE + 1
            strike_slider = ui.slider(min=1, max=11, step=2, value=default_total_strikes).classes("w-full")
            strike_count_label = ui.label(f"Currently: {default_total_strikes} strikes (center ± {config.STRIKES_EACH_SIDE})")

        def save_strike_count():
            n = int(strike_slider.value)
            config.STRIKES_EACH_SIDE = (n - 1) // 2
            strike_count_label.set_text(f"Currently: {n} strikes (center ± {config.STRIKES_EACH_SIDE})")
            log(f"[{datetime.now().strftime('%H:%M:%S')}] Strike count set to {n}.")

        ui.button("Save Strike Count", on_click=save_strike_count).classes("w-full")
        
        # ---------------------------------------------------- NEW RESCAN BUTTON
        def force_rescan():
            # Unfreeze the CE = PE scanners
            for sm in state_mgrs.values():
                sm.state.frozen = False
                sm.state.frozen_until = None
                sm.state.last_recalc_at = None
            log(f"[{datetime.now().strftime('%H:%M:%S')}] Manual rescan: strike window will be re-centred on the next scan.")
            
        ui.button("Rescan", on_click=force_rescan, color="orange") \
            .classes("w-full").props("icon=restart_alt")
        # ----------------------------------------------------

        with ui.column().classes("w-full gap-1"):
            scan_interval_input = ui.number("Scan interval (seconds)", value=config.SCAN_INTERVAL_SECONDS, min=1, step=1).classes("w-full")
            scan_interval_label = ui.label(f"Currently: every {config.SCAN_INTERVAL_SECONDS}s")

        def save_scan_interval():
            try:
                n = int(scan_interval_input.value)
            except (TypeError, ValueError):
                scan_interval_label.set_text("Enter a whole number of seconds.")
                return
            if n < 1:
                scan_interval_label.set_text("Must be at least 1 second — not applied.")
                return
            config.SCAN_INTERVAL_SECONDS = n
            scan_timer.interval = n
            scan_interval_label.set_text(f"Currently: every {n}s")
            log(f"[{datetime.now().strftime('%H:%M:%S')}] Scan interval set to {n}s.")

        ui.button("Save Scan Interval", on_click=save_scan_interval).classes("w-full")

    def apply_alert_state():
        paused = config.ALERTS_PAUSED
        alert_btn.set_text("Resume alerts" if paused else "Pause alerts")
        alert_btn.props("color=green icon=notifications_active" if paused else "color=primary icon=notifications_off")
        paused_badge.set_visibility(paused)

    def toggle_alerts():
        config.ALERTS_PAUSED = not config.ALERTS_PAUSED
        apply_alert_state()
        if config.ALERTS_PAUSED:
            ui.notify("Alerts paused", type="warning")
            log(f"[{datetime.now().strftime('%H:%M:%S')}] Alerts PAUSED")
        else:
            ui.notify("Alerts resumed", type="positive")
            log(f"[{datetime.now().strftime('%H:%M:%S')}] Alerts RESUMED")

    with ui.row().classes("items-center gap-4"):
        ui.button(icon="menu", on_click=drawer.toggle).props("flat round")
        ui.label("LTP Crossing (CE = PE)").classes("text-2xl font-bold")
        alert_btn = ui.button("Pause alerts", icon="notifications_off", on_click=toggle_alerts)
        paused_badge = ui.badge("ALERTS PAUSED").props("color=red")
        apply_alert_state()

        def on_index_toggle(idx_name, enabled):
            config.INDEXES[idx_name]["enabled"] = bool(enabled)
            if idx_name in panels:
                panels[idx_name]["section"].set_visibility(bool(enabled))
            log(f"[{datetime.now().strftime('%H:%M:%S')}] {idx_name} {'ENABLED' if enabled else 'DISABLED'}")

        for _idx, _cfg in config.INDEXES.items():
            ui.checkbox(_idx, value=_cfg["enabled"], on_change=lambda ev, n=_idx: on_index_toggle(n, ev.value))

    with ui.tabs().classes("w-full") as tabs:
        live_tab = ui.tab("Live Scanner")
        backtest_tab = ui.tab("Backtest")

    with ui.tab_panels(tabs, value=live_tab).classes("w-full"):
        with ui.tab_panel(live_tab):
            ui.separator()
            with ui.row().classes("items-center gap-6"):
                status_label = ui.label("Status: waiting for valid token")
            polling = {"busy": False}

            def build_panel(name):
                section = ui.column().classes("w-full gap-2")
                with section:
                    ui.separator()
                    with ui.row().classes("gap-6 items-center"):
                        ui.label(name).classes("text-xl font-bold")
                        ltp_label = ui.label(f"{name} LTP: --")
                        window_label = ui.label("Window: --")

                    pending_tracks = []
                    # CE = PE (LTP crossing), with its own on/off checkbox
                    with ui.row().classes("items-center gap-4"):
                        ce_pe_cb = ui.checkbox(f"{name} CE = PE (LTP Crossing)", value=config.INDEXES[name]["ce_pe"]) \
                            .classes("text-md font-bold")
                        ce_pe_label = ui.label("")
                    matches_col = ui.column().classes("w-full gap-2")
                    with matches_col:
                        matches_table = ui.table(columns=LIVE_MATCH_COLUMNS, rows=[], row_key="id").classes("w-full").props("dense")
                        INVALID_CLASS = "props.row.state === 'invalid' ? 'bg-red-1' : ''"
                        for col in LIVE_MATCH_COLUMNS:
                            matches_table.add_slot(
                                f"body-cell-{col['name']}",
                                f'<q-td key="{col["name"]}" :props="props" :class="{INVALID_CLASS}">{{{{ props.value }}}}</q-td>',
                            )

                        def download_live_csv():
                            ui.download(export_utils.build_csv(matches_table.rows, LIVE_EXPORT_COLUMNS), f"live_matches_{name.lower()}.csv")

                        def download_live_xlsx():
                            ui.download(export_utils.build_xlsx(matches_table.rows, LIVE_EXPORT_COLUMNS), f"live_matches_{name.lower()}.xlsx")

                        with ui.row().classes("gap-3"):
                            ui.button("Download CSV", on_click=download_live_csv)
                            ui.button("Download XLSX", on_click=download_live_xlsx)

                    def on_ce_pe_change(ev):
                        config.INDEXES[name]["ce_pe"] = bool(ev.value)
                        matches_col.set_visibility(bool(ev.value))
                        ce_pe_label.set_text("" if ev.value else "CE = PE: off")
                        log(f"[{datetime.now().strftime('%H:%M:%S')}] {name} CE = PE scanning {'ENABLED' if ev.value else 'DISABLED'}")

                    ce_pe_cb.on_value_change(on_ce_pe_change)
                    matches_col.set_visibility(config.INDEXES[name]["ce_pe"])

                def handle_scan(summary):
                    """CE = PE: show new matches, alert, and queue the 5-minute follow-up."""
                    matches = summary.get("matches") or []
                    if matches:
                        notifier.dispatch_match_alerts(matches, log_fn=log)
                        sm = state_mgrs[name]
                        expiry = sm.state.window.expiry_date if sm.state.window else None
                        new_rows = []
                        for m in matches:
                            bucket_start, bucket_end = tracker.bucket_bounds(m.triggered_at.astimezone(market_utils.IST))
                            row_id = len(matches_table.rows) + len(new_rows)
                            new_rows.append({
                                "id": row_id, "time": m.triggered_at.strftime("%H:%M:%S"),
                                "ce_strike": m.ce_strike, "pe_strike": m.pe_strike, "price": m.price,
                                "check_till": bucket_end.strftime("%H:%M"), "closed_above": "Tracking...", "state": "tracking",
                            })
                            if expiry:
                                pending_tracks.append(tracker.PendingTrack(
                                    row_id=row_id, ce_strike=m.ce_strike, pe_strike=m.pe_strike,
                                    price=m.price, expiry=expiry, bucket_start=bucket_start, bucket_end=bucket_end,
                                    underlying_key=config.INDEXES[name]["underlying_key"],
                                ))
                        matches_table.rows[0:0] = new_rows
                        matches_table.update()

                def process_followups():
                    if not pending_tracks:
                        return
                    updates = tracker.process_pending(client, pending_tracks, datetime.now(market_utils.IST))
                    if updates:
                        for row_id, fields, final in updates:
                            for row in matches_table.rows:
                                if row["id"] == row_id:
                                    row.update(fields)
                                    break
                            if final:
                                log(f"[{datetime.now().strftime('%H:%M:%S')}] {name} match #{row_id} result: {fields.get('result')} ({fields.get('closed_above', '')})")
                        matches_table.update()

                section.set_visibility(config.INDEXES[name]["enabled"])
                return {"section": section, "ltp_label": ltp_label, "window_label": window_label,
                        "ce_pe_label": ce_pe_label,
                        "handle_scan": handle_scan, "process_followups": process_followups}

            for _idx in config.INDEXES:
                panels[_idx] = build_panel(_idx)

            ui.separator()
            log_area = ui.markdown("")

            async def poll():
                if not token_connected:
                    status_label.set_text("Status: waiting for valid token")
                    return
                active = [n for n in config.INDEXES if config.INDEXES[n]["enabled"]]
                if not active:
                    status_label.set_text("Status: no index selected (tick NIFTY and/or SENSEX at the top)")
                    return
                if polling["busy"]:
                    return                       # the previous scan is still running
                polling["busy"] = True
                try:
                    ltps, err = await run.io_bound(fetch_index_ltps, active)
                    if market_utils.is_market_open():
                        status_label.set_text("Status: monitoring " + " + ".join(active))
                    else:
                        status_label.set_text(f"Status: market closed (Mon\u2013Fri {config.MARKET_OPEN}\u2013{config.MARKET_CLOSE} IST)")

                    for name in active:
                        if not config.INDEXES[name]["enabled"]:
                            continue                 # switched off while this scan was running
                        ltp = ltps.get(name)
                        if ltp is None:
                            log(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {name} LTP unavailable ({err})")
                            continue
                        p = panels[name]
                        p["ltp_label"].set_text(f"{name} LTP: {ltp}")

                        # ---- CE = PE (LTP crossing) scan
                        if config.INDEXES[name].get("ce_pe", True):
                            try:
                                market_open = market_utils.is_market_open()
                                scan_logs = []
                                summary = await run.io_bound(lambda n=name, v=ltp, o=market_open: scanner.run_scan_cycle(
                                    client, state_mgrs[n], log_fn=scan_logs.append, detect=o, index=n, index_ltp=v))
                                for msg in scan_logs:
                                    log(msg)
                                for e_msg in summary.get("errors", []):
                                    log(f"[{datetime.now().strftime('%H:%M:%S')}] {name} ERROR: {e_msg}")
                                if not market_open:
                                    p["ce_pe_label"].set_text("CE = PE: paused (market closed)")
                                elif summary.get("frozen"):
                                    p["ce_pe_label"].set_text(f"CE = PE: cooldown ({summary.get('cooldown_remaining')}s left)")
                                else:
                                    p["ce_pe_label"].set_text("CE = PE: scanning")
                                if summary.get("window"):
                                    p["window_label"].set_text(f"Window: {min(summary['window'])} - {max(summary['window'])}")
                                p["handle_scan"](summary)
                                p["process_followups"]()
                            except Exception as ex:
                                log(f"[{datetime.now().strftime('%H:%M:%S')}] {name} CE = PE error: {ex}")
                        else:
                            p["ce_pe_label"].set_text("CE = PE: off")
                finally:
                    polling["busy"] = False

            scan_timer = ui.timer(config.SCAN_INTERVAL_SECONDS, poll)

        # ======================================================== BACKTEST TAB
        with ui.tab_panel(backtest_tab):
            ui.label("Replay today's (or any day in the current expiry cycle's) "
                     "raw rounded CE = PE crossings, checked every 1-minute candle (NIFTY only). "
                     "Cooldown is ignored here \u2014 every crossing is shown.").classes("text-sm text-gray-600")

            bt_expiry_label = ui.label("Expiry: not loaded")
            bt_date_select = ui.select([], label="Select date").classes("w-64")

            def load_dates():
                if not token_connected:
                    bt_expiry_label.set_text("Expiry: connect Upstox token first (Live Scanner tab)")
                    return
                expiry_str, dates = market_utils.current_weekly_expiry_dates(client)
                if not expiry_str:
                    bt_expiry_label.set_text("Expiry: could not fetch expiry")
                    return
                bt_expiry_label.set_text(f"Expiry: {expiry_str} (Tuesday)")
                bt_date_select.set_options(dates)
                if dates:
                    bt_date_select.set_value(dates[-1])

            with ui.row().classes("gap-3"):
                ui.button("Load Expiry Dates", on_click=load_dates)

            bt_summary_label = ui.label("")
            bt_run_button = ui.button("Run CE = PE Backtest")

            ui.label("Matches Found \u2014 type in the Comment column to annotate any row").classes("text-md font-bold")
            bt_results_table = ui.table(columns=MATCH_COLUMNS, rows=[], row_key="id").classes("w-full")
            bt_results_table.add_slot("body-cell-comment", COMMENT_CELL_SLOT % {"event_name": "bt_comment_change"})
            bt_results_table.on("bt_comment_change", make_comment_sync_handler(bt_results_table))

            def download_bt_csv():
                ui.download(export_utils.build_csv(bt_results_table.rows, EXPORT_COLUMNS), "backtest_matches.csv")

            def download_bt_xlsx():
                ui.download(export_utils.build_xlsx(bt_results_table.rows, EXPORT_COLUMNS), "backtest_matches.xlsx")

            with ui.row().classes("gap-3"):
                ui.button("Download CSV", on_click=download_bt_csv)
                ui.button("Download XLSX", on_click=download_bt_xlsx)

            def run_backtest_click():
                if not token_connected:
                    bt_summary_label.set_text("Connect Upstox token first (Live Scanner tab).")
                    return
                date_str = bt_date_select.value
                if not date_str:
                    bt_summary_label.set_text("Pick a date first \u2014 click 'Load Expiry Dates' if the list is empty.")
                    return
                expiry_str, _ = market_utils.current_weekly_expiry_dates(client)
                bt_summary_label.set_text("Running backtest...")
                result = backtest.run_backtest(client, date_str, expiry_str, log_fn=log)
                for e in result["errors"]:
                    log(f"[{datetime.now().strftime('%H:%M:%S')}] BACKTEST ERROR: {e}")
                bt_summary_label.set_text(
                    f"Date: {result['date']} | Open: {result['day_open']} | Close: {result['day_close']} | "
                    f"Strikes checked: {result['strikes']} | Matches found: {len(result['events'])}"
                )
                bt_results_table.rows = [{**event, "id": idx, "comment": ""} for idx, event in enumerate(result["events"])]
                bt_results_table.update()

            bt_run_button.on_click(run_backtest_click)


ui.run(title="LTP Crossing (CE = PE)", port=8082, reload=False)