"""
app.py — NiceGUI entry point for the CE/PE Match Alert tool.

Layout: a status row (Nifty LTP, current strike window, cooldown state)
plus a live scan log. A ui.timer drives the polling loop.

Both the Live Scanner and Backtest match tables have an inline, editable
"Comment" column (last column) so you can annotate each match as it comes
in — useful for judging whether the strategy is actually working. Comments
are kept in-memory against each row and are included when you download
CSV/XLSX.
"""
from datetime import datetime
from nicegui import ui

import config
import upstox_client
import scanner
import notifier
import telegram_bot
import market_utils
import backtest
import export_utils
import tracker
import bep
from state_manager import StateManager

state_mgr = StateManager()
client = upstox_client.UpstoxClient(config.UPSTOX_ACCESS_TOKEN)

log_lines = []
log_area = None  # assigned inside main_page
token_connected = False  # gates the poll loop from running with a bad/blank token

# Column set shared by both tables: comment is always last, per your request
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

# Live table = the match columns + the 5-minute follow-up result (tracker.py) + BEP info.
LIVE_MATCH_COLUMNS = MATCH_COLUMNS[:-1] + [
    {"name": "check_till", "label": "Checked Till", "field": "check_till"},
    {"name": "touched", "label": "Touched P", "field": "touched"},
    {"name": "closed_above", "label": "Closed Above", "field": "closed_above"},
    {"name": "result", "label": "Result", "field": "result"},
    {"name": "ce_bep", "label": "CE BEP", "field": "ce_bep"},
    {"name": "pe_bep", "label": "PE BEP", "field": "pe_bep"},
    {"name": "bep_alert", "label": "BEP Alert", "field": "bep_alert"},
    MATCH_COLUMNS[-1],  # comment stays last
]
LIVE_EXPORT_COLUMNS = EXPORT_COLUMNS[:-1] + [
    ("check_till", "Checked Till"), ("touched", "Touched P"),
    ("closed_above", "Closed Above"),
    ("result", "Result"), ("ce_bep", "CE BEP"), ("pe_bep", "PE BEP"),
    ("bep_alert", "BEP Alert"), EXPORT_COLUMNS[-1],
]

# The inline-editable comment cell: a q-input bound to the row, which emits
# an event back to Python on blur so the comment is captured for export.
COMMENT_CELL_SLOT = '''
    <q-td key="comment" :props="props">
        <q-input v-model="props.row.comment" dense placeholder="notes..."
            @blur="() => $parent.$emit('%(event_name)s', props.row)" />
    </q-td>
'''


# Same editable comment cell, but the cell is coloured like the rest of its row:
# amber when a BEP alert fired, else green when Valid, light red when Invalid.
LIVE_COMMENT_CELL_SLOT = '''
    <q-td key="comment" :props="props"
          :class="props.row.bep_hit ? 'bg-amber-3' : (props.row.state === 'valid' ? 'bg-green-2' : (props.row.state === 'invalid' ? 'bg-red-1' : ''))">
        <q-input v-model="props.row.comment" dense placeholder="notes..."
            @blur="() => $parent.$emit('%(event_name)s', props.row)" />
    </q-td>
'''


def log(msg: str):
    log_lines.append(msg)
    del log_lines[:-200]  # keep last 200 lines in memory
    if log_area is not None:
        log_area.set_content("\n".join(log_lines[-40:]))


def make_comment_sync_handler(table):
    """Returns a handler that writes an edited comment back into table.rows,
    matched by the row's unique 'id' (not by time — multiple matches in the
    same scan cycle can share the same timestamp)."""
    def handler(e):
        updated_row = e.args
        for row in table.rows:
            if row.get("id") == updated_row.get("id"):
                row["comment"] = updated_row.get("comment", "")
                break
    return handler


@ui.page("/")
def main_page():
    global log_area, token_connected

    # Ask for browser notification permission once, on load
    ui.run_javascript("""
        if (Notification.permission !== "granted" && Notification.permission !== "denied") {
            Notification.requestPermission();
        }
    """)

    # ---------------------------------------------------------- Sidebar: Telegram settings
    with ui.left_drawer(value=True, fixed=False).classes("bg-gray-100 p-4") as drawer:
        ui.label("Telegram Settings").classes("text-lg font-bold")

        tg_token_input = ui.input(
            "Bot Token",
            value=config.TELEGRAM_BOT_TOKEN,
            password=True,
            password_toggle_button=True,
        ).classes("w-full")

        tg_chat_id_input = ui.input(
            "Chat ID(s) — comma-separated for multiple accounts",
            value=config.TELEGRAM_CHAT_ID,
        ).classes("w-full")

        tg_status_label = ui.label(
            "Status: not configured" if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
            else "Status: unverified"
        )

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
            config.TELEGRAM_ENABLED = len(succeeded) > 0  # keep alerts on if at least one recipient works

            if all_ok:
                tg_status_label.set_text(f"Status: connected — test sent to {len(succeeded)} chat ID(s)")
                log(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram saved and verified "
                    f"({len(succeeded)} chat ID(s)).")
            else:
                fail_txt = "; ".join(f"{cid} ({err})" for cid, err in failed)
                if succeeded:
                    tg_status_label.set_text(f"Status: partial — sent to {len(succeeded)}, failed: {fail_txt}")
                else:
                    tg_status_label.set_text(f"Status: failed for all — {fail_txt}")
                log(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram test — failures: {fail_txt}")

        ui.button("Save & Test Telegram", on_click=save_and_test_telegram).classes("w-full")

    with ui.row().classes("items-center"):
        ui.button(icon="menu", on_click=drawer.toggle).props("flat round")
        ui.label("Nifty CE = PE Match Alert").classes("text-2xl font-bold")

    with ui.tabs().classes("w-full") as tabs:
        live_tab = ui.tab("Live Scanner")
        backtest_tab = ui.tab("Backtest")

    with ui.tab_panels(tabs, value=live_tab).classes("w-full"):

        # ======================================================== LIVE SCANNER TAB
        with ui.tab_panel(live_tab):

            # ---------------------------------------------------- Upstox token settings
            with ui.row().classes("gap-3 items-center"):
                token_input = ui.input(
                    "Upstox Access Token",
                    value=config.UPSTOX_ACCESS_TOKEN,
                    password=True,
                    password_toggle_button=True,
                ).classes("w-96")
                connection_label = ui.label(
                    "Status: not connected" if not config.UPSTOX_ACCESS_TOKEN else "Status: unverified"
                )

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

            with ui.row().classes("gap-3"):
                ui.button("Save & Test Connection", on_click=save_and_test_token)

            # ---------------------------------------------------- Strike count setting
            with ui.row().classes("gap-3 items-center"):
                default_total_strikes = 2 * config.STRIKES_EACH_SIDE + 1
                strike_count_input = ui.number(
                    "Number of strikes to check",
                    value=default_total_strikes,
                    min=1,
                    step=2,
                ).classes("w-56")
                strike_count_label = ui.label(f"Currently: {default_total_strikes} strikes "
                                               f"(center \u00b1 {config.STRIKES_EACH_SIDE})")

            def save_strike_count():
                raw = strike_count_input.value
                try:
                    n = int(raw)
                except (TypeError, ValueError):
                    strike_count_label.set_text("Enter a whole number.")
                    return
                if n < 1 or n % 2 == 0:
                    strike_count_label.set_text(
                        f"Must be an odd number (1, 3, 5, 7...) since it's always "
                        f"center \u00b1 N. Got {n} — not applied."
                    )
                    return
                config.STRIKES_EACH_SIDE = (n - 1) // 2
                strike_count_label.set_text(f"Currently: {n} strikes (center \u00b1 {config.STRIKES_EACH_SIDE})")
                log(f"[{datetime.now().strftime('%H:%M:%S')}] Strike count set to {n} "
                    f"(center \u00b1 {config.STRIKES_EACH_SIDE}). Applies from next window recalculation.")

            with ui.row().classes("gap-3"):
                ui.button("Save Strike Count", on_click=save_strike_count)

            # ---------------------------------------------------- Scan interval setting
            with ui.row().classes("gap-3 items-center"):
                scan_interval_input = ui.number(
                    "Scan interval (seconds)",
                    value=config.SCAN_INTERVAL_SECONDS,
                    min=1,
                    step=1,
                ).classes("w-56")
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
                scan_timer.interval = n  # scan_timer is created further down this page
                scan_interval_label.set_text(f"Currently: every {n}s")
                log(f"[{datetime.now().strftime('%H:%M:%S')}] Scan interval set to {n}s.")

            with ui.row().classes("gap-3"):
                ui.button("Save Scan Interval", on_click=save_scan_interval)

            ui.separator()

            with ui.row().classes("gap-6 items-center"):
                nifty_label = ui.label("Nifty LTP: --")
                window_label = ui.label("Window: --")
                status_label = ui.label("Status: waiting for valid token")

            ui.separator()
            ui.label("Matches Found — type in the Comment column to annotate any row").classes("text-md font-bold")
            live_matches_table = ui.table(
                columns=LIVE_MATCH_COLUMNS,
                rows=[],
                row_key="id",
            ).classes("w-full")
            ROW_STATE_CLASS = ("props.row.bep_hit ? 'bg-amber-3' : (props.row.state === 'valid' ? 'bg-green-2' : "
                               "(props.row.state === 'invalid' ? 'bg-red-1' : ''))")
            for col in LIVE_MATCH_COLUMNS:
                if col["name"] == "comment":
                    continue
                live_matches_table.add_slot(
                    f"body-cell-{col['name']}",
                    f'<q-td key="{col["name"]}" :props="props" :class="{ROW_STATE_CLASS}">'
                    f'{{{{ props.value }}}}</q-td>',
                )
            live_matches_table.add_slot(
                "body-cell-comment", LIVE_COMMENT_CELL_SLOT % {"event_name": "live_comment_change"}
            )
            live_matches_table.on("live_comment_change", make_comment_sync_handler(live_matches_table))

            def download_live_csv():
                data = export_utils.build_csv(live_matches_table.rows, LIVE_EXPORT_COLUMNS)
                ui.download(data, "live_matches.csv")

            def download_live_xlsx():
                data = export_utils.build_xlsx(live_matches_table.rows, LIVE_EXPORT_COLUMNS)
                ui.download(data, "live_matches.xlsx")

            with ui.row().classes("gap-3"):
                ui.button("Download CSV", on_click=download_live_csv)
                ui.button("Download XLSX", on_click=download_live_xlsx)

            ui.separator()
            log_area = ui.markdown("")

            pending_tracks = []  # matches waiting for their 5-min candle to close

            def poll():
                if not token_connected:
                    status_label.set_text("Status: waiting for valid token")
                    return

                summary = scanner.run_scan_cycle(client, state_mgr, log_fn=log)

                if summary.get("nifty_ltp") is not None:
                    nifty_label.set_text(f"Nifty LTP: {summary['nifty_ltp']}")
                if summary.get("window"):
                    window_label.set_text(f"Window: {summary['window']}")

                if summary.get("frozen"):
                    status_label.set_text(f"Status: cooldown ({summary.get('cooldown_remaining')}s left)")
                else:
                    status_label.set_text("Status: scanning")

                for err in summary.get("errors", []):
                    log(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {err}")

                matches = summary.get("matches") or []
                if matches:
                    notifier.dispatch_match_alerts(matches, log_fn=log)
                    expiry = state_mgr.state.window.expiry_date if state_mgr.state.window else None
                    new_rows = []  # collected here, then put at the TOP of the table below
                    for m in matches:
                        # The 5-min chart candle containing the match (09:31 -> 09:30-09:35)
                        bucket_start, bucket_end = tracker.bucket_bounds(
                            m.triggered_at.astimezone(market_utils.IST)
                        )
                        row_id = len(live_matches_table.rows) + len(new_rows)  # unique, never reused
                        ce_bep = bep.get_bep(client, expiry, m.ce_strike) if expiry else None
                        pe_bep = bep.get_bep(client, expiry, m.pe_strike) if expiry else None
                        if ce_bep is None or pe_bep is None:
                            log(f"[{datetime.now().strftime('%H:%M:%S')}] BEP unavailable for "
                                f"{'CE ' + str(m.ce_strike) if ce_bep is None else ''}"
                                f"{' ' if ce_bep is None and pe_bep is None else ''}"
                                f"{'PE ' + str(m.pe_strike) if pe_bep is None else ''} "
                                f"(no previous-day close) — no BEP alert for that leg.")
                        new_rows.append({
                            "id": row_id,
                            "time": m.triggered_at.strftime("%H:%M:%S"),
                            "ce_strike": m.ce_strike,
                            "pe_strike": m.pe_strike,
                            "ce_ltp": round(m.ce_ltp, 2) if m.ce_ltp is not None else None,
                            "pe_ltp": round(m.pe_ltp, 2) if m.pe_ltp is not None else None,
                            "price": m.price,
                            "check_till": bucket_end.strftime("%H:%M"),
                            "touched": "Tracking...",
                            "ce_bep": ce_bep,
                            "pe_bep": pe_bep,
                            "bep_alert": "",
                            "bep_hit": False,
                            "closed_above": "Tracking...",
                            "result": "Tracking...",
                            "state": "tracking",
                            "comment": "",
                        })
                        if expiry:
                            pending_tracks.append(tracker.PendingTrack(
                                row_id=row_id, ce_strike=m.ce_strike, pe_strike=m.pe_strike,
                                price=m.price, expiry=expiry, ce_bep=ce_bep, pe_bep=pe_bep,
                                bucket_start=bucket_start, bucket_end=bucket_end,
                            ))
                    live_matches_table.rows[0:0] = new_rows  # newest match goes to the top
                    live_matches_table.update()

                # Advance the 5-min follow-up for any match whose candle has now closed
                if pending_tracks:
                    updates = tracker.process_pending(client, pending_tracks, datetime.now(market_utils.IST))
                    if updates:
                        for row_id, fields, final in updates:
                            bep_alert = fields.pop("_bep_alert", None)  # alert payload, not a table field
                            for row in live_matches_table.rows:
                                if row["id"] == row_id:
                                    row.update(fields)
                                    break
                            if bep_alert:
                                notifier.dispatch_bep_alert(bep_alert, log_fn=log)
                                log(f"[{datetime.now().strftime('%H:%M:%S')}] BEP alert for match "
                                    f"#{row_id}: {bep_alert['text']}")
                            if final:
                                log(f"[{datetime.now().strftime('%H:%M:%S')}] Match #{row_id} result: "
                                    f"{fields.get('result')} ({fields.get('closed_above', '')})")
                        live_matches_table.update()

            scan_timer = ui.timer(config.SCAN_INTERVAL_SECONDS, poll)

        # ======================================================== BACKTEST TAB
        with ui.tab_panel(backtest_tab):
            ui.label("Replay today's (or any day in the current expiry cycle's) "
                     "raw rounded CE = PE crossings, checked every 1-minute candle. "
                     "Cooldown is ignored here — every crossing is shown.").classes("text-sm text-gray-600")

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
                    bt_date_select.set_value(dates[-1])  # default to most recent date

            with ui.row().classes("gap-3"):
                ui.button("Load Expiry Dates", on_click=load_dates)

            bt_summary_label = ui.label("")
            bt_run_button = ui.button("Run Backtest")
            ui.label("Matches Found — type in the Comment column to annotate any row").classes("text-md font-bold")
            bt_results_table = ui.table(
                columns=MATCH_COLUMNS,
                rows=[],
                row_key="id",
            ).classes("w-full")
            bt_results_table.add_slot(
                "body-cell-comment", COMMENT_CELL_SLOT % {"event_name": "bt_comment_change"}
            )
            bt_results_table.on("bt_comment_change", make_comment_sync_handler(bt_results_table))

            def download_bt_csv():
                data = export_utils.build_csv(bt_results_table.rows, EXPORT_COLUMNS)
                ui.download(data, "backtest_matches.csv")

            def download_bt_xlsx():
                data = export_utils.build_xlsx(bt_results_table.rows, EXPORT_COLUMNS)
                ui.download(data, "backtest_matches.xlsx")

            with ui.row().classes("gap-3"):
                ui.button("Download CSV", on_click=download_bt_csv)
                ui.button("Download XLSX", on_click=download_bt_xlsx)

            def run_backtest_click():
                if not token_connected:
                    bt_summary_label.set_text("Connect Upstox token first (Live Scanner tab).")
                    return
                date_str = bt_date_select.value
                if not date_str:
                    bt_summary_label.set_text("Pick a date first — click 'Load Expiry Dates' if the list is empty.")
                    return

                expiry_str, _ = market_utils.current_weekly_expiry_dates(client)
                bt_summary_label.set_text("Running backtest...")
                result = backtest.run_backtest(client, date_str, expiry_str, log_fn=log)

                if result["errors"]:
                    for e in result["errors"]:
                        log(f"[{datetime.now().strftime('%H:%M:%S')}] BACKTEST ERROR: {e}")

                bt_summary_label.set_text(
                    f"Date: {result['date']} | Open: {result['day_open']} | Close: {result['day_close']} | "
                    f"Strikes checked: {result['strikes']} | Matches found: {len(result['events'])}"
                )
                bt_results_table.rows = [
                    {**event, "id": idx, "comment": ""}
                    for idx, event in enumerate(result["events"])
                ]
                bt_results_table.update()

            bt_run_button.on_click(run_backtest_click)


ui.run(title="CE/PE Match Alert", port=8082, reload=False)
