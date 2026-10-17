import asyncio
import csv
import datetime
import os
import aiohttp
import ccxt.async_support as ccxt

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAME = "1h"
HISTORY_LIMIT = 150
LIVE_POLL_INTERVAL = 3
MIN_REMAINING_MINUTES = 30.0
SCAN_MAX_REMAINING_MIN = 59.75
ENTRY_MAX_REMAINING_MIN = 58.5
MIN_BODY_PCT = 0.005
CONFIRM_CHECKS = 2

# --- НАСТРОЙКИ НАСТОЯЩЕГО ОТКАТА ОТ ОТКРЫТИЯ ЧАСА ---
PULLBACK_RETRACE_RATIO = 0.40
MIN_PULLBACK_PCT = 0.35
DIRECT_PULLBACK_PCT = 0.65

LOG_FILE = "signals_log.csv"
CHAT_ID_FILE = "chat_id.txt"

RAW_TICKERS = [
    "BTWUSDT", "ZECUSDT", "TAOUSDT", "LINKUSDT", "ONDOUSDT", "AKEUSDT", "ADAUSDT",
    "INJUSDT", "OPUSDT", "LTCUSDT", "AAVEUSDT", "FILUSDT", "HBARUSDT", "APTUSDT",
    "ICPUSDT", "XLMUSDT", "XPLUSDT", "JTOUSDT", "RIVERUSDT", "ATOMUSDT", "SHIBUSDT",
    "ASTERUSDT", "PONSUSDT", "龙虾USDT", "LABUSDT", "DASHUSDT", "VVVUSDT", "PENGUUSDT",
    "LDOUSDT", "VIRTUALUSDT", "BRUSDT", "GALAUSDT", "CHIPUSDT", "MEGAUSDT", "TRUMPUSDT",
    "POLUSDT", "FLOKIUSDT", "ONEUSDT", "JASMYUSDT", "1000BONKUSDT", "COAIUSDT", "APEUSDT",
    "LSKUSDT", "VELVETUSDT", "ORDIUSDT", "WLFIUSDT", "ACEUSDT", "LYNUSDT", "COPPER(XCU)USDT",
    "SPXUSDT", "KAITOUSDT", "MYXUSDT", "SYNUSDT", "ESPORTSUSDT", "BILLUSDT", "BLESSUSDT",
    "UAIUSDT", "DEXEUSDT", "PIPPINUSDT", "CYSUSDT", "POWERUSDT", "TUTUSDT", "XAIUSDT",
    "LIGHTUSDT", "GAS(NG)USDT", "ALLOUSDT", "HNTUSDT", "SKYAIUSDT", "PLATINUM(XPT)USDT", "EVAAUSDT",
    "PALLADIUM(XPD)USDT", "BICOUSDT", "BTRUSDT", "HEIUSDT", "REUSDT", "FOLKSUSDT", "STOUSDT",
    "NICKEL(XNI)USDT", "LAYERUSDT", "ALUMINIUM(XAL)USDT", "LEAD(XPB)USDT", "ZINC(XZN)USDT", "ACNUSDT", "AKAMUSDT",
]

active_symbols = {}
current_hour_ts = 0
hourly_candidates = {}
trigger_confirm_counter = {}
sent_signals = set()


# ==================== АВТО-СОХРАНЕНИЕ CHAT_ID ====================
def get_target_chat_id() -> str:
    if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID != "ВАШ_CHAT_ID" and TELEGRAM_CHAT_ID.strip():
        return TELEGRAM_CHAT_ID.strip()
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE, "r", encoding="utf-8") as f:
            saved = f.read().strip()
            if saved:
                return saved
    return ""


def save_target_chat_id(chat_id):
    with open(CHAT_ID_FILE, "w", encoding="utf-8") as f:
        f.write(str(chat_id))


# ==================== РАБОТА С БАЗОЙ СТАТИСТИКИ (CSV) ====================
CSV_HEADERS = [
    "signal_id", "signal_time_utc", "ticker", "direction", "entry_price",
    "remaining_minutes", "streak_candles", "reversal_body_pct", "live_body_pct",
    "trigger_reason", "status", "updated_time_utc"
]


def init_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()


def log_new_signal(data: dict):
    init_csv()
    with open(LOG_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writerow(data)


def update_signal_status(signal_id: str, new_status: str):
    init_csv()
    rows = []
    updated_row = None
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with open(LOG_FILE, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "trigger_reason" not in row or not row["trigger_reason"]:
                row["trigger_reason"] = "Смена цвета HA"
            if row["signal_id"] == signal_id:
                row["status"] = new_status
                row["updated_time_utc"] = now_str
                updated_row = row
            rows.append(row)

    if updated_row:
        with open(LOG_FILE, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    return updated_row


def get_stats_summary() -> str:
    init_csv()
    total = 0
    win = 0
    loss = 0
    pending = 0
    long_win, long_loss = 0, 0
    short_win, short_loss = 0, 0

    with open(LOG_FILE, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            st = row.get("status", "")
            d = row.get("direction", "")
            if st == "ЗАШЕЛ":
                win += 1
                if d == "LONG":
                    long_win += 1
                else:
                    short_win += 1
            elif st == "НЕ ЗАШЕЛ":
                loss += 1
                if d == "LONG":
                    long_loss += 1
                else:
                    short_loss += 1
            else:
                pending += 1

    marked = win + loss
    winrate = (win / marked * 100.0) if marked > 0 else 0.0
    lines = [
        "📊 <b>Статистика сигналов:</b>",
        "",
        f"Всего сигналов: <b>{total}</b>",
        f"✅ Зашло: <b>{win}</b>",
        f"❌ Не зашло: <b>{loss}</b>",
        f"⏳ Не отмечено: <b>{pending}</b>",
        f"🎯 <b>Винрейт (по отмеченным): {winrate:.1f}%</b>",
        "",
        f"🟢 LONG: {long_win} зашло / {long_loss} не зашло",
        f"🔴 SHORT: {short_win} зашло / {short_loss} не зашло",
        "",
        "📥 Для выгрузки файла анализа отправьте /export"
    ]
    return "\n".join(lines)


# ==================== РАСЧЕТ HEIKIN ASHI ====================
def calculate_heikin_ashi(ohlcv):
    ha_candles = []
    for i, candle in enumerate(ohlcv):
        ts, o, h, l, c, v = candle
        ha_close = (o + h + l + c) / 4.0

        if i == 0:
            ha_open = (o + c) / 2.0
        else:
            ha_open = (ha_candles[i - 1]["ha_open"] + ha_candles[i - 1]["ha_close"]) / 2.0

        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)

        body_pct = abs(ha_close - ha_open) / ha_open * 100.0 if ha_open > 0 else 0.0

        if body_pct < MIN_BODY_PCT:
            color = "DOJI"
        elif ha_close > ha_open:
            color = "GREEN"
        else:
            color = "RED"

        ha_candles.append({
            "timestamp": ts,
            "ha_open": ha_open,
            "ha_high": ha_high,
            "ha_low": ha_low,
            "ha_close": ha_close,
            "real_open": o,
            "real_high": h,
            "real_low": l,
            "real_close": c,
            "body_pct": body_pct,
            "color": color
        })
    return ha_candles


def count_consecutive_color(ha_candles, start_index, target_color):
    count = 0
    for i in range(start_index, -1, -1):
        if ha_candles[i]["color"] == target_color:
            count += 1
        else:
            break
    return count


# ==================== TELEGRAM: УВЕДОМЛЕНИЯ И КНОПКИ ====================
def build_signal_message(data: dict) -> str:
    ticker = data["ticker"]
    direction = data["direction"]
    price = data["entry_price"]
    dt_str = data["signal_time_utc"]
    rem_min = float(data["remaining_minutes"])
    streak = int(data["streak_candles"])
    rev_pct = float(data["reversal_body_pct"])
    live_pct = float(data["live_body_pct"])
    trigger_reason = data.get("trigger_reason", "Откат + HA")
    status = data["status"]

    mexc_pair = ticker[:-4] + "_USDT"
    chart_url = f"https://futures.mexc.com/exchange/{mexc_pair}"

    if direction == "SHORT":
        header = "🔴 <b>СИГНАЛ: SHORT (ВНИЗ)</b>"
        chain = f"{'🟢' * min(streak, 6)} ({streak} закр.) ➔ 🔴 (1 закр.) ➔ ⚡ {trigger_reason}"
    else:
        header = "🟢 <b>СИГНАЛ: LONG (ВВЕРХ)</b>"
        chain = f"{'🔴' * min(streak, 6)} ({streak} закр.) ➔ 🟢 (1 закр.) ➔ ⚡ {trigger_reason}"

    if status == "ЗАШЕЛ":
        status_line = "✅ <b>Результат: СИГНАЛ ЗАШЕЛ</b>"
    elif status == "НЕ ЗАШЕЛ":
        status_line = "❌ <b>Результат: НЕ ЗАШЕЛ</b>"
    else:
        status_line = "⏳ <b>Результат: Ожидает отметки (нажми кнопку ниже)</b>"

    lines = [
        header,
        "",
        f"📌 <b>Пара:</b> <code>{ticker}</code> (<a href='{chart_url}'>Открыть график</a>)",
        f"💰 <b>Цена входа:</b> <code>{price}</code>",
        f"🎯 <b>Триггер входа:</b> {trigger_reason}",
        f"🕒 <b>Время (MEXC):</b> {dt_str} UTC",
        f"⏱ <b>До закрытия 1H свечи:</b> {rem_min:.1f} мин.",
        "",
        "📊 <b>Цепочка Heikin Ashi (1H):</b>",
        chain,
        f"▪️ Тело разворотной свечи: {rev_pct:.3f}%",
        f"▪️ Тело живой свечи HA: {live_pct:.3f}%",
        "",
        status_line
    ]
    return "\n".join(lines)


def build_inline_keyboard(signal_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Зашел", "callback_data": f"WIN|{signal_id}"},
                {"text": "❌ Не зашел", "callback_data": f"LOSS|{signal_id}"}
            ]
        ]
    }


async def send_telegram_signal(session: aiohttp.ClientSession, data: dict) -> bool:
    target_chat = get_target_chat_id()
    if not target_chat:
        print("[ОШИБКА TG] Chat ID не задан! Напишите боту /start в Telegram.", flush=True)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": build_signal_message(data),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": build_inline_keyboard(data["signal_id"])
    }
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            if resp.status != 200:
                print(f"[Ошибка TG sendMessage]: {await resp.text()}", flush=True)
                return False
            return True
    except Exception as e:
        print(f"[Ошибка связи с Telegram]: {e}", flush=True)
        return False


async def send_telegram_file(session: aiohttp.ClientSession, chat_id: str):
    init_csv()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    with open(LOG_FILE, "rb") as f:
        file_bytes = f.read()

    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("caption", "📁 Файл статистики сигналов.")
    form.add_field("document", file_bytes, filename="signals_log.csv", content_type="text/csv")
    try:
        async with session.post(url, data=form, timeout=20) as resp:
            if resp.status != 200:
                print(f"[Ошибка отправки файла TG]: {await resp.text()}", flush=True)
    except Exception as e:
        print(f"[Ошибка отправки файла]: {e}", flush=True)


async def telegram_polling_worker(session: aiohttp.ClientSession):
    offset = 0
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    while True:
        try:
            async with session.get(f"{base_url}/getUpdates", params={"offset": offset, "timeout": 20}, timeout=25) as resp:
                if resp.status != 200:
                    await asyncio.sleep(3)
                    continue
                result = await resp.json()

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    msg = cb.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    message_id = msg.get("message_id")

                    if chat_id:
                        save_target_chat_id(chat_id)

                    if "|" in cb_data:
                        action, sig_id = cb_data.split("|", 1)
                        new_status = "ЗАШЕЛ" if action == "WIN" else "НЕ ЗАШЕЛ"
                        updated_row = update_signal_status(sig_id, new_status)

                        if updated_row and chat_id and message_id:
                            edit_payload = {
                                "chat_id": chat_id,
                                "message_id": message_id,
                                "text": build_signal_message(updated_row),
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True,
                                "reply_markup": build_inline_keyboard(sig_id)
                            }
                            await session.post(f"{base_url}/editMessageText", json=edit_payload)

                        await session.post(f"{base_url}/answerCallbackQuery", json={
                            "callback_query_id": cb_id,
                            "text": f"Отмечено: {new_status}"
                        })

                elif "message" in update:
                    msg = update["message"]
                    text = msg.get("text", "").strip()
                    chat_id = msg.get("chat", {}).get("id")

                    if chat_id:
                        save_target_chat_id(chat_id)

                    if "document" in msg:
                        doc = msg["document"]
                        if doc.get("file_name", "").endswith(".csv"):
                            file_id = doc["file_id"]
                            async with session.get(f"{base_url}/getFile", params={"file_id": file_id}) as f_resp:
                                f_info = await f_resp.json()
                            file_path = f_info.get("result", {}).get("file_path")
                            if file_path:
                                dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
                                async with session.get(dl_url) as dl_resp:
                                    content = await dl_resp.read()
                                with open(LOG_FILE, "wb") as f:
                                    f.write(content)
                                await session.post(f"{base_url}/sendMessage", json={
                                    "chat_id": chat_id,
                                    "text": "✅ База статистики успешно восстановлена!",
                                    "parse_mode": "HTML"
                                })
                        continue

                    if text.startswith("/stats"):
                        summary = get_stats_summary()
                        await session.post(f"{base_url}/sendMessage", json={
                            "chat_id": chat_id, "text": summary, "parse_mode": "HTML"
                        })
                    elif text.startswith("/export"):
                        await send_telegram_file(session, chat_id)
                    elif text.startswith("/status") or text.startswith("/start"):
                        cand_list = ", ".join(
                            [f"{k} ({v['target_dir']})" for k, v in hourly_candidates.items()]
                        ) or "нет кандидатов в этом часе"
                        saved_cid = get_target_chat_id()
                        status_lines = [
                            "🤖 <b>Бот Heikin Ashi + True Pullback активен</b>",
                            f"📩 <b>Chat ID:</b> <code>{saved_cid}</code> (подключен ✅)",
                            f"📊 <b>Пар:</b> {len(active_symbols)} из {len(RAW_TICKERS)}",
                            f"🎯 <b>Кандидаты ({len(hourly_candidates)}):</b>",
                            f"<code>{cand_list}</code>"
                        ]
                        await session.post(f"{base_url}/sendMessage", json={
                            "chat_id": chat_id,
                            "text": "\n".join(status_lines),
                            "parse_mode": "HTML"
                        })

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Ошибка поллинга TG]: {e}", flush=True)
            await asyncio.sleep(5)


# ==================== СКАНИРОВАНИЕ MEXC FUTURES ====================
async def resolve_mexc_symbols(exchange):
    markets = await exchange.load_markets()
    resolved = {}

    id_map = {}
    for sym, m in markets.items():
        if m.get("swap") and m.get("quote") == "USDT":
            raw_id = m.get("id", "").replace("_", "")
            id_map[raw_id] = sym

    for ticker in RAW_TICKERS:
        if ticker in id_map:
            resolved[ticker] = id_map[ticker]

    print(f"[Инициализация] Найдено пар на MEXC Futures: {len(resolved)} из {len(RAW_TICKERS)}", flush=True)
    return resolved


async def scan_hourly_candidates(exchange, hour_open_ts):
    candidates = {}
    hour_ms = 3600 * 1000
    prev_closed_ts = hour_open_ts - hour_ms
    sem = asyncio.Semaphore(8)

    async def check_one(ticker, ccxt_sym):
        async with sem:
            try:
                ohlcv = await exchange.fetch_ohlcv(ccxt_sym, timeframe=TIMEFRAME, limit=HISTORY_LIMIT)
            except Exception:
                return

            if not ohlcv or len(ohlcv) < 10:
                return

            closed_candles = [c for c in ohlcv if c[0] < hour_open_ts]

            if len(closed_candles) < 10 or closed_candles[-1][0] != prev_closed_ts:
                return

            ha_closed = calculate_heikin_ashi(closed_candles)
            reversal_candle = ha_closed[-1]
            idx_before_rev = len(ha_closed) - 2

            rev_real_range = max(
                abs(reversal_candle["real_open"] - reversal_candle["real_close"]),
                abs(reversal_candle["ha_open"] - reversal_candle["ha_close"])
            )

            if reversal_candle["color"] == "RED":
                streak = count_consecutive_color(ha_closed, idx_before_rev, "GREEN")
                if streak >= 3:
                    candidates[ticker] = {
                        "target_dir": "SHORT",
                        "target_live_color": "GREEN",
                        "streak": streak,
                        "rev_body_pct": reversal_candle["body_pct"],
                        "rev_close_price": reversal_candle["real_close"],
                        "rev_range": rev_real_range
                    }

            elif reversal_candle["color"] == "GREEN":
                streak = count_consecutive_color(ha_closed, idx_before_rev, "RED")
                if streak >= 3:
                    candidates[ticker] = {
                        "target_dir": "LONG",
                        "target_live_color": "RED",
                        "streak": streak,
                        "rev_body_pct": reversal_candle["body_pct"],
                        "rev_close_price": reversal_candle["real_close"],
                        "rev_range": rev_real_range
                    }

    await asyncio.gather(*(check_one(t, s) for t, s in active_symbols.items()))
    return candidates


async def monitor_candidate_live(exchange, session, ticker, info, server_time_ms, hour_open_ts, rem_min):
    if (ticker, hour_open_ts) in sent_signals:
        return

    ccxt_sym = active_symbols[ticker]
    try:
        ohlcv = await exchange.fetch_ohlcv(ccxt_sym, timeframe=TIMEFRAME, limit=HISTORY_LIMIT)
    except Exception:
        return

    if not ohlcv:
        return

    ha_candles = calculate_heikin_ashi(ohlcv)
    live_candle = ha_candles[-1]

    if live_candle["timestamp"] != hour_open_ts:
        return

    current_price = live_candle["real_close"]
    open_price = live_candle["real_open"]
    rev_range = info["rev_range"]
    target_dir = info["target_dir"]

    trigger_reason = None

    if target_dir == "SHORT" and current_price > open_price:
        pullback_abs = current_price - open_price
        pullback_pct = (pullback_abs / open_price) * 100.0 if open_price > 0 else 0.0
        retrace_ratio = (pullback_abs / rev_range) if rev_range > 0 else 0.0

        if pullback_pct >= MIN_PULLBACK_PCT:
            if live_candle["color"] == "GREEN":
                trigger_reason = f"Зеленая HA + откат вверх +{pullback_pct:.2f}%"
            elif retrace_ratio >= PULLBACK_RETRACE_RATIO or pullback_pct >= DIRECT_PULLBACK_PCT:
                trigger_reason = f"Откат вверх +{pullback_pct:.2f}% ({retrace_ratio*100:.0f}% от пред. свечи)"

    elif target_dir == "LONG" and current_price < open_price:
        pullback_abs = open_price - current_price
        pullback_pct = (pullback_abs / open_price) * 100.0 if open_price > 0 else 0.0
        retrace_ratio = (pullback_abs / rev_range) if rev_range > 0 else 0.0

        if pullback_pct >= MIN_PULLBACK_PCT:
            if live_candle["color"] == "RED":
                trigger_reason = f"Красная HA + откат вниз -{pullback_pct:.2f}%"
            elif retrace_ratio >= PULLBACK_RETRACE_RATIO or pullback_pct >= DIRECT_PULLBACK_PCT:
                trigger_reason = f"Откат вниз -{pullback_pct:.2f}% ({retrace_ratio*100:.0f}% от пред. свечи)"

    if trigger_reason:
        trigger_confirm_counter[ticker] = trigger_confirm_counter.get(ticker, 0) + 1
    else:
        trigger_confirm_counter[ticker] = 0
        return

    if trigger_confirm_counter[ticker] >= CONFIRM_CHECKS:
        dt_str = datetime.datetime.fromtimestamp(
            server_time_ms / 1000.0, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")

        signal_id = f"{ticker}_{hour_open_ts // 1000}"
        signal_data = {
            "signal_id": signal_id,
            "signal_time_utc": dt_str,
            "ticker": ticker,
            "direction": target_dir,
            "entry_price": current_price,
            "remaining_minutes": f"{rem_min:.1f}",
            "streak_candles": info["streak"],
            "reversal_body_pct": f"{info['rev_body_pct']:.4f}",
            "live_body_pct": f"{live_candle['body_pct']:.4f}",
            "trigger_reason": trigger_reason,
            "status": "ОЖИДАЕТ",
            "updated_time_utc": dt_str
        }

        sent_ok = await send_telegram_signal(session, signal_data)
        if sent_ok:
            sent_signals.add((ticker, hour_open_ts))
            log_new_signal(signal_data)
            print(f"[СИГНАЛ ОТПРАВЛЕН] {ticker} -> {target_dir} | {trigger_reason} | Цена: {current_price}", flush=True)


# ==================== ТОЧКА ВХОДА ====================
async def main():
    global active_symbols, current_hour_ts, hourly_candidates, trigger_confirm_counter

    init_csv()
    exchange = ccxt.mexc({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"}
    })

    async with aiohttp.ClientSession() as session:
        active_symbols = await resolve_mexc_symbols(exchange)
        tg_task = asyncio.create_task(telegram_polling_worker(session))

        print("Бот успешно запущен на хостинге и отслеживает рынок...", flush=True)
        try:
            while True:
                try:
                    server_time_ms = await exchange.fetch_time()
                    hour_ms = 3600 * 1000
                    hour_open_ts = (server_time_ms // hour_ms) * hour_ms
                    hour_close_ts = hour_open_ts + hour_ms
                    remaining_minutes = (hour_close_ts - server_time_ms) / 60000.0

                    if hour_open_ts != current_hour_ts and remaining_minutes <= SCAN_MAX_REMAINING_MIN:
                        current_hour_ts = hour_open_ts
                        trigger_confirm_counter.clear()
                        print(f"\n[Новый час] Сканирование всех {len(active_symbols)} пар...", flush=True)
                        hourly_candidates = await scan_hourly_candidates(exchange, hour_open_ts)
                        print(f"[Кандидаты часа] Найдено сетапов: {len(hourly_candidates)} -> {list(hourly_candidates.keys())}", flush=True)

                    if MIN_REMAINING_MINUTES <= remaining_minutes <= ENTRY_MAX_REMAINING_MIN and hourly_candidates:
                        tasks = [
                            monitor_candidate_live(
                                exchange, session, ticker, info,
                                server_time_ms, hour_open_ts, remaining_minutes
                            )
                            for ticker, info in hourly_candidates.items()
                            if (ticker, hour_open_ts) not in sent_signals
                        ]
                        if tasks:
                            await asyncio.gather(*tasks)

                except Exception as loop_err:
                    print(f"[Ошибка основного цикла]: {loop_err}", flush=True)

                await asyncio.sleep(LIVE_POLL_INTERVAL)
        finally:
            tg_task.cancel()
            await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
