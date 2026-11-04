"""
app.py — NiceGUI entry point for the CE/PE Match Alert tool.
"""
from datetime import datetime
from nicegui import ui, run

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
from bep_monitor import BepMonitor
from state_manager import StateManager

state_mgr = StateManager()
bep_monitor = BepMonitor()
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

BOARD_COLUMNS = [
    {"name": "strike", "label": "Strike", "field": "strike", "align": "left"},
    {"name": "ltp", "label": "LTP", "field": "ltp", "align": "right"},
    {"name": "bep", "label": "BEP", "field": "bep", "align": "right"},
    {"name": "time", "label": "Time", "field": "time", "align": "right"},
]

SUPPORT_GREEN = ("rgb(200,230,201)", "#1b3d1e")
SUPPORT_RED = ("rgb(255,205,210)", "#7f1d1d")

COMMENT_CELL_SLOT = '''
    <q-td key="comment" :props="props">
        <q-input v-model="props.row.comment" dense placeholder="notes..."
            @blur="() => $parent.$emit('%(event_name)s', props.row)" />
    </q-td>
'''

def log(msg: str):
    log_lines.append(msg)
    del log_lines[:-200]
    if log_area is not None:
        log_area.set_content("\n".join(log_lines[-40:]))

def make_comment_sync_handler(table):
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
        ui.label("BEP file").classes("text-lg font-bold")

        async def handle_bep_upload(e):
            if hasattr(e, "file"):
                data, name = await e.file.read(), getattr(e.file, "name", "")
            else:
                data, name = e.content.read(), getattr(e, "name", "")
            ok, msg, warns = bep.load_csv(data, name)
            bep_upload_label.set_text(("✅ " if ok else "❌ ") + msg)
            log(f"[{datetime.now().strftime('%H:%M:%S')}] BEP upload ({name}): {msg}")
            bep_status_label.set_text(bep.status_text())
            if ok:
                bep_monitor.reset_backfill()
                render_board()
                render_first_cards()
                render_latest_cards()
                log(f"[{datetime.now().strftime('%H:%M:%S')}] BEP board reset; re-checking all candles since 09:15.")
            try:
                bep_upload.reset()
            except Exception:
                pass

        bep_upload = ui.upload(on_upload=handle_bep_upload, auto_upload=True, max_files=1).props("accept=.csv").classes("hidden")
        ui.button("Upload BEP CSV", icon="upload_file", on_click=lambda: bep_upload.run_method('pickFiles')).classes("w-full")
            
        bep_upload_label = ui.label("")
        bep_status_label = ui.label(bep.status_text()).classes("text-sm text-gray-600")

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
            # 1. Unfreeze the live scanner
            state_mgr.state.frozen = False
            state_mgr.state.frozen_until = None
            state_mgr.state.last_recalc_at = None
            
            # 2. Wipe the BEP monitor's morning memory
            bep_monitor.reset_backfill()
            
            log(f"[{datetime.now().strftime('%H:%M:%S')}] 🚨 Manual rescan triggered: The app is re-scanning from 09:15 with the current window.")
            
        ui.button("Rescan from 09:15", on_click=force_rescan, color="orange") \
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
        ui.label("Nifty CE = PE Match Alert").classes("text-2xl font-bold")
        alert_btn = ui.button("Pause alerts", icon="notifications_off", on_click=toggle_alerts)
        paused_badge = ui.badge("ALERTS PAUSED").props("color=red")
        apply_alert_state()

    with ui.tabs().classes("w-full") as tabs:
        live_tab = ui.tab("Live Scanner")
        backtest_tab = ui.tab("Backtest")

    with ui.tab_panels(tabs, value=live_tab).classes("w-full"):
        with ui.tab_panel(live_tab):
            ui.separator()

            with ui.row().classes("gap-6 items-center"):
                nifty_label = ui.label("Nifty LTP: --")
                window_label = ui.label("Window: --")
                status_label = ui.label("Status: waiting for valid token")

            ui.separator()

            first_cards = {}
            latest_cards = {}
            with ui.row().classes("gap-4"):
                # FIRST CARDS
                for kind in ("CE", "PE"):
                    card = ui.card().classes("w-80 rounded-xl shadow-md")
                    with card:
                        with ui.row().classes("items-center justify-between w-full no-wrap"):
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.icon("circle", color="green" if kind == "CE" else "red").classes("text-3xl")
                                ui.label(f"First {kind}").classes("text-gray-600")
                            badge = ui.badge("--").props("color=grey")
                        name_label = ui.label("--").classes("text-xl font-bold")
                        detail_label = ui.label("Waiting for first candle").classes("text-sm text-gray-500")
                    first_cards[kind] = {"card": card, "badge": badge, "name": name_label, "detail": detail_label}

                # LATEST SUPPORT CARDS
                for kind in ("CE", "PE"):
                    card = ui.card().classes("w-80 rounded-xl shadow-md")
                    with card:
                        with ui.row().classes("items-center justify-between w-full no-wrap"):
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.icon("circle", color="green" if kind == "CE" else "red").classes("text-3xl")
                                ui.label(f"Latest Support {kind}").classes("text-gray-600")
                            badge = ui.badge("--").props("color=grey")
                        name_label = ui.label(f"NO {kind}").classes("text-xl font-bold")
                        detail_label = ui.label("").classes("text-sm text-gray-500")
                    latest_cards[kind] = {"card": card, "badge": badge, "name": name_label, "detail": detail_label}

            def render_board():
                view = bep_monitor.board_view()
                for opt, table in board_tables.items():
                    rows = []
                    for r in view.get(opt, []):
                        row = dict(r)
                        row["bg"], row["fg"] = SUPPORT_GREEN if r["kind"] == "green" else SUPPORT_RED
                        rows.append(row)
                    table.rows = rows
                    table.update()

            def render_first_cards():
                view = bep_monitor.first_view or {}
                for kind, w in first_cards.items():
                    w["card"].classes(remove="bg-green-2")
                    leg = view.get(kind)
                    if not leg:
                        w["name"].set_text(f"NO {kind}")
                        w["detail"].set_text("Waiting for first candle")
                        w["badge"].set_text("--")
                        w["badge"].props("color=grey")
                        continue
                    w["name"].set_text(f"NIFTY {leg['strike']}{kind}")
                    ltp, level = leg.get("ltp"), leg.get("bep")
                    w["detail"].set_text(f"BEP {level:.2f} · LTP {ltp:.2f}" if ltp is not None else f"BEP {level:.2f} · LTP --")
                    if ltp is not None:
                        gap = ltp - level
                        w["badge"].set_text(f"{gap:+.2f}")
                        w["badge"].props(f"color={'green' if gap >= 0 else 'red'}")
                    else:
                        w["badge"].set_text("--")
                        w["badge"].props("color=grey")
                    w["card"].classes(add="bg-green-2")

            def render_latest_cards():
                view = bep_monitor.latest_view or {}
                for kind, w in latest_cards.items():
                    w["card"].classes(remove="bg-green-2 bg-red-2")
                    leg = view.get(kind)
                    if not leg:
                        w["name"].set_text(f"NO {kind}")
                        w["detail"].set_text("")
                        w["badge"].set_text("--")
                        w["badge"].props("color=grey")
                        continue
                    w["name"].set_text(f"NIFTY {leg['strike']}{kind}")
                    ltp, level = leg.get("ltp"), leg.get("bep")
                    w["detail"].set_text(f"BEP {level:.2f} · LTP {ltp:.2f}" if ltp is not None else f"BEP {level:.2f} · LTP --")
                    if ltp is not None:
                        gap = ltp - level
                        w["badge"].set_text(f"{gap:+.2f}")
                        w["badge"].props(f"color={'green' if gap >= 0 else 'red'}")
                    else:
                        w["badge"].set_text("--")
                        w["badge"].props("color=grey")
                    w["card"].classes(add="bg-green-2" if leg.get("state") == "green" else "bg-red-2")

            ui.separator()
            ui.label("BEP Support Boards: Green = a green candle touched BEP (low <= BEP) and closed above it. "
                     "Red = a later candle closed below the fail level (the flush candle's low if a failed-breakdown "
                     "setup stood behind it, otherwise the trigger candle's own low). New supports raise a notification.").classes("text-xs text-gray-500")
            
            with ui.row().classes("w-full no-wrap items-start gap-4"):
                with ui.column().classes("gap-2").style("flex: 2 1 0; min-width: 0"):
                    ui.label("Matches Found (CE = PE)").classes("text-md font-bold")
                    live_matches_table = ui.table(columns=LIVE_MATCH_COLUMNS, rows=[], row_key="id").classes("w-full").props("dense")
                    INVALID_CLASS = "props.row.state === 'invalid' ? 'bg-red-1' : ''"
                    for col in LIVE_MATCH_COLUMNS:
                        live_matches_table.add_slot(
                            f"body-cell-{col['name']}",
                            f'<q-td key="{col["name"]}" :props="props" :class="{INVALID_CLASS}">{{{{ props.value }}}}</q-td>',
                        )

                    def download_live_csv():
                        ui.download(export_utils.build_csv(live_matches_table.rows, LIVE_EXPORT_COLUMNS), "live_matches.csv")
                    def download_live_xlsx():
                        ui.download(export_utils.build_xlsx(live_matches_table.rows, LIVE_EXPORT_COLUMNS), "live_matches.xlsx")

                    with ui.row().classes("gap-3"):
                        ui.button("Download CSV", on_click=download_live_csv)
                        ui.button("Download XLSX", on_click=download_live_xlsx)

                board_tables = {}
                for opt in ("CE", "PE"):
                    with ui.column().classes("gap-2").style("flex: 1 1 0; min-width: 0"):
                        ui.label(f"BEP Support — {opt}").classes("text-md font-bold")
                        table = ui.table(columns=BOARD_COLUMNS, rows=[], row_key="id").classes("w-full").props("dense flat bordered").style("max-height: 640px; overflow-y: auto")
                        table.add_slot(
                            "body",
                            """
                            <q-tr :props="props" :style="props.row.bg ? {backgroundColor: props.row.bg, color: props.row.fg} : {}">
                              <q-td key="strike" :props="props"><span class="text-weight-bold">{{ props.row.strike }}</span></q-td>
                              <q-td key="ltp" :props="props">{{ props.row.ltp }}</q-td>
                              <q-td key="bep" :props="props">{{ props.row.bep }}</q-td>
                              <q-td key="time" :props="props">{{ props.row.time }}</q-td>
                            </q-tr>
                            """,
                        )
                        board_tables[opt] = table
            ui.separator()
            log_area = ui.markdown("")

            pending_tracks = []

            async def poll():
                if not token_connected:
                    status_label.set_text("Status: waiting for valid token")
                    return

                market_open = market_utils.is_market_open()
                summary = scanner.run_scan_cycle(client, state_mgr, log_fn=log, detect=market_open)

                if summary.get("nifty_ltp") is not None:
                    nifty_label.set_text(f"Nifty LTP: {summary['nifty_ltp']}")
                if summary.get("window"):
                    window_label.set_text(f"Window: {summary['window']}")

                if not market_open:
                    status_label.set_text(f"Status: market closed — match scanning paused (Mon–Fri {config.MARKET_OPEN}–{config.MARKET_CLOSE} IST)")
                elif summary.get("frozen"):
                    status_label.set_text(f"Status: cooldown ({summary.get('cooldown_remaining')}s left)")
                else:
                    status_label.set_text("Status: scanning")

                for err in summary.get("errors", []):
                    log(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {err}")

                matches = summary.get("matches") or []
                if matches:
                    notifier.dispatch_match_alerts(matches, log_fn=log)
                    expiry = state_mgr.state.window.expiry_date if state_mgr.state.window else None
                    new_rows = []
                    for m in matches:
                        bucket_start, bucket_end = tracker.bucket_bounds(m.triggered_at.astimezone(market_utils.IST))
                        row_id = len(live_matches_table.rows) + len(new_rows)
                        new_rows.append({
                            "id": row_id, "time": m.triggered_at.strftime("%H:%M:%S"),
                            "ce_strike": m.ce_strike, "pe_strike": m.pe_strike, "price": m.price,
                            "check_till": bucket_end.strftime("%H:%M"), "closed_above": "Tracking...", "state": "tracking",
                        })
                        if expiry:
                            pending_tracks.append(tracker.PendingTrack(
                                row_id=row_id, ce_strike=m.ce_strike, pe_strike=m.pe_strike,
                                price=m.price, expiry=expiry, bucket_start=bucket_start, bucket_end=bucket_end,
                            ))
                    live_matches_table.rows[0:0] = new_rows
                    live_matches_table.update()

                if pending_tracks:
                    updates = tracker.process_pending(client, pending_tracks, datetime.now(market_utils.IST))
                    if updates:
                        for row_id, fields, final in updates:
                            for row in live_matches_table.rows:
                                if row["id"] == row_id:
                                    row.update(fields)
                                    break
                            if final:
                                log(f"[{datetime.now().strftime('%H:%M:%S')}] Match #{row_id} result: {fields.get('result')} ({fields.get('closed_above', '')})")
                        live_matches_table.update()

                if summary.get("nifty_ltp") is not None:
                    try:
                        hint = state_mgr.state.window.expiry_date if state_mgr.state.window else None
                        res = await run.io_bound(bep_monitor.step, client, summary["nifty_ltp"], hint, datetime.now(market_utils.IST))
                        for msg in res["logs"]:
                            log(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
                        if res["board_changed"] or res["recalculated"]:
                            render_board()
                        if res.get("first_updated"):
                            render_first_cards()
                        if res.get("latest_updated"):
                            render_latest_cards()
                        support_alerts = bep_monitor.pop_support_alerts()
                        if support_alerts:
                            notifier.dispatch_support_alerts(support_alerts, log_fn=log)
                    except Exception as ex:
                        log(f"[{datetime.now().strftime('%H:%M:%S')}] BEP monitor error: {ex}")

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
                    bt_date_select.set_value(dates[-1])

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