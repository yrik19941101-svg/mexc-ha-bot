import asyncio
import csv
import datetime
import os
import aiohttp
import ccxt.async_support as ccxt

# ==================== ОСНОВНЫЕ НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# API КЛЮЧИ BINGX
BINGX_API_KEY = os.getenv("BINGX_API_KEY", "")
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")

# РИСК-МЕНЕДЖМЕНТ
DEFAULT_TRADE_AMOUNT = 1.0      # Сумма маржи по умолчанию ($1)
SL_PERCENT_OF_MARGIN = 0.90     # Стоп-лосс: -90% от маржи
TP_PERCENT_OF_MARGIN = 1.10     # Тейк-профит: +110% от маржи

TIMEFRAME = "1h"
HISTORY_LIMIT = 120
LIVE_POLL_INTERVAL = 3
MIN_REMAINING_MINUTES = 25.0    
SCAN_MAX_REMAINING_MIN = 59.75  
ENTRY_MAX_REMAINING_MIN = 59.0  
MIN_BODY_PCT = 0.005
CONFIRM_CHECKS = 2              

MIN_24H_VOLUME_USDT = 1_500_000 
SCAN_CONCURRENCY = 10
LIVE_CONCURRENCY = 8

# Сила разворотной свечи (оригинал)
REV_MIN_STRENGTH_RATIO = 0.35   
REV_MIN_ABS_BODY_PCT = 0.08     

# Двухфазный вход (оригинал)
PULLBACK_RETRACE_RATIO = 0.45   
MIN_PULLBACK_PCT = 0.50         
DIRECT_PULLBACK_PCT = 0.85      
MIN_BOUNCE_PCT = 0.18           
MIN_BOUNCE_RATIO = 0.22         

RSI_PERIOD = 14
RSI_SHORT_OVERBOUGHT = 68.0     
RSI_LONG_OVERSOLD = 32.0        

LOG_FILE = "signals_log.csv"
CHAT_ID_FILE = "chat_id.txt"
AMOUNT_FILE = "trade_amount.txt"

active_symbols = {}
current_hour_ts = 0
hourly_candidates = {}
trigger_confirm_counter = {}
sent_signals = set()
is_scanning = False


# ==================== РАБОТА С ПАРАМЕТРАМИ И CHAT_ID ====================
def get_target_chat_id() -> str:
    if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID not in ("ВАШ_ТОКЕН_БОТА", "ВАШ_CHAT_ID") and TELEGRAM_CHAT_ID.strip():
        return TELEGRAM_CHAT_ID.strip()
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE, "r", encoding="utf-8") as f:
            saved = f.read().strip()
            if saved: return saved
    return ""


def save_target_chat_id(chat_id):
    with open(CHAT_ID_FILE, "w", encoding="utf-8") as f:
        f.write(str(chat_id))


def get_trade_amount() -> float:
    if os.path.exists(AMOUNT_FILE):
        try:
            with open(AMOUNT_FILE, "r", encoding="utf-8") as f:
                val = float(f.read().strip())
                if val > 0: return val
        except Exception:
            pass
    return DEFAULT_TRADE_AMOUNT


def save_trade_amount(amount: float):
    with open(AMOUNT_FILE, "w", encoding="utf-8") as f:
        f.write(str(amount))


# ==================== РАБОТА СО СТАТИСТИКОЙ (CSV) ====================
CSV_HEADERS = [
    "signal_id", "signal_time_utc", "ticker", "direction", "entry_price",
    "remaining_minutes", "streak_candles", "rsi_value", "mtf_mode", "trigger_reason", "order_status", "updated_time_utc"
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


def update_signal_order_status(signal_id: str, new_status: str):
    init_csv()
    rows = []
    updated_row = None
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with open(LOG_FILE, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["signal_id"] == signal_id:
                row["order_status"] = new_status
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
    executed = 0
    with open(LOG_FILE, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            if row.get("order_status") == "В СДЕЛКЕ":
                executed += 1
    return f"📊 <b>Статистика бота:</b>\nВсего сигналов: <b>{total}</b>\nОткрыто сделок: <b>{executed}</b>"


# ==================== РАСЧЕТ ИНДИКАТОРОВ ====================
def calculate_rsi_series(closes: list, period: int = 14) -> list:
    n = len(closes)
    if n < period + 1: return [50.0] * n
    rsi = [50.0] * n
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0: rsi[period] = 100.0
    else: rsi[period] = 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    for i in range(period + 1, n):
        diff = closes[i] - closes[i - 1]
        gain, loss = max(diff, 0.0), max(-diff, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0: rsi[i] = 100.0
        else: rsi[i] = 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    return rsi


def build_6h_ohlcv_from_1h(ohlcv_1h: list) -> list:
    six_h_ms = 6 * 3600 * 1000
    buckets = {}
    for c in ohlcv_1h:
        ts, o, h, l, cl, v = c
        b_ts = (ts // six_h_ms) * six_h_ms
        if b_ts not in buckets: buckets[b_ts] = [b_ts, o, h, l, cl, v]
        else:
            b = buckets[b_ts]
            if h > b[2]: b[2] = h
            if l < b[3]: b[3] = l
            b[4] = cl
            b[5] += v
    return [buckets[k] for k in sorted(buckets.keys())]


def calculate_heikin_ashi(ohlcv):
    closes = [c[4] for c in ohlcv]
    rsi_series = calculate_rsi_series(closes, RSI_PERIOD)
    ha_candles = []
    for i, candle in enumerate(ohlcv):
        ts, o, h, l, c, v = candle
        ha_close = (o + h + l + c) / 4.0
        if i == 0: ha_open = (o + c) / 2.0
        else: ha_open = (ha_candles[i - 1]["ha_open"] + ha_candles[i - 1]["ha_close"]) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        body_pct = abs(ha_close - ha_open) / ha_open * 100.0 if ha_open > 0 else 0.0
        color = "DOJI" if body_pct < MIN_BODY_PCT else ("GREEN" if ha_close > ha_open else "RED")
        ha_candles.append({
            "timestamp": ts, "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low,
            "ha_close": ha_close, "real_open": o, "real_high": h, "real_low": l,
            "real_close": c, "body_pct": body_pct, "color": color, "rsi": rsi_series[i]
        })
    return ha_candles


def count_consecutive_color(ha_candles, start_index, target_color):
    count = 0
    for i in range(start_index, -1, -1):
        if ha_candles[i]["color"] == target_color: count += 1
        else: break
    return count


def get_htf_color(ha_candles: list) -> str:
    if not ha_candles: return "DOJI"
    last_c = ha_candles[-1]
    if last_c["color"] != "DOJI" and last_c["body_pct"] >= 0.03: return last_c["color"]
    if len(ha_candles) >= 2: return ha_candles[-2]["color"]
    return last_c["color"]


# ==================== ТОРГОВЛЯ НА BINGX ====================
async def execute_bingx_trade(ticker: str, direction: str, entry_price: float) -> str:
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        return "❌ Ошибка: API ключи BingX не настроены на Bothost!"

    trade_amount = get_trade_amount()
    exchange = ccxt.bingx({
        "apiKey": BINGX_API_KEY,
        "secret": BINGX_SECRET_KEY,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"}
    })

    try:
        await exchange.load_markets()
        ccxt_sym = active_symbols.get(ticker)
        if not ccxt_sym:
            for s, r in active_symbols.items():
                if r == ticker or s == ticker:
                    ccxt_sym = s
                    break
        if not ccxt_sym:
            ccxt_sym = ticker[:-4] + "/USDT:USDT" if ticker.endswith("USDT") else ticker

        market = exchange.market(ccxt_sym)
        contract_size = market.get("contractSize", 1.0)
        
        balance = await exchange.fetch_balance({"type": "swap"})
        usdt_free = float(balance.get("USDT", {}).get("free", 0))
        if usdt_free < trade_amount:
            await exchange.close()
            return f"❌ Недостаточно средств на балансе BingX (доступно: ${usdt_free:.2f}, нужно: ${trade_amount})"

        ticker_info = await exchange.fetch_ticker(ccxt_sym)
        last_price = float(ticker_info.get("last", entry_price))

        leverage_info = await exchange.fapiPrivate_get_trade_leverage({"symbol": market["id"]})
        lev = 20
        try:
            lev = float(leverage_info.get("data", {}).get("leverage", 20))
        except Exception:
            pass

        position_usdt_size = trade_amount * lev
        amount_contracts = (position_usdt_size / last_price) / contract_size

        side = "buy" if direction == "LONG" else "sell"
        params = {"positionSide": "LONG" if direction == "LONG" else "SHORT"}

        order = await exchange.create_order(ccxt_sym, "market", side, amount_contracts, None, params)
        filled_price = float(order.get("average") or order.get("price") or last_price)

        sl_usd = trade_amount * SL_PERCENT_OF_MARGIN
        tp_usd = trade_amount * TP_PERCENT_OF_MARGIN

        price_delta_sl = (sl_usd / position_usdt_size) * filled_price
        price_delta_tp = (tp_usd / position_usdt_size) * filled_price

        if direction == "LONG":
            sl_price = filled_price - price_delta_sl
            tp_price = filled_price + price_delta_tp
            sl_side, tp_side = "sell", "sell"
        else:
            sl_price = filled_price + price_delta_sl
            tp_price = filled_price - price_delta_tp
            sl_side, tp_side = "buy", "buy"

        try:
            await exchange.create_order(ccxt_sym, "stop_market", sl_side, amount_contracts, None, {"stopPrice": sl_price, "reduceOnly": True})
            await exchange.create_order(ccxt_sym, "take_profit_market", tp_side, amount_contracts, None, {"stopPrice": tp_price, "reduceOnly": True})
        except Exception as e:
            print(f"[Предупреждение SL/TP]: {e}", flush=True)

        await exchange.close()
        return f"✅ Сделка открыта (Маржа: ${trade_amount})!\nВход: <code>{filled_price}</code>\nSL: <code>{sl_price:.4f}</code> | TP: <code>{tp_price:.4f}</code>"

    except Exception as e:
        await exchange.close()
        return f"❌ Ошибка API BingX: {str(e)}"


# ==================== TELEGRAM ====================
def build_signal_message(data: dict) -> str:
    ticker = data["ticker"]
    direction = data["direction"]
    price = data["entry_price"]
    rem_min = float(data["remaining_minutes"])
    streak = int(data["streak_candles"])
    rsi_val = float(data.get("rsi_value", 50.0))
    mtf_mode = data.get("mtf_mode", "1D + 6H + 1H")
    trigger_reason = data.get("trigger_reason", "Откат + Отбой")
    order_status = data.get("order_status", "Ожидает")

    bingx_pair = ticker[:-4] + "-USDT" if ticker.endswith("USDT") and "-" not in ticker else ticker
    chart_url = f"https://bingx.com/ru-ru/perpetual/{bingx_pair}"

    if direction == "SHORT":
        header = "🔴 <b>СИГНАЛ BINGX: SHORT</b>"
        chain = f"{'🟢' * min(streak, 6)} ({streak} закр.) ➔ 🔴 (1 закр.) ➔ 🎯 {trigger_reason}"
    else:
        header = "🟢 <b>СИГНАЛ BINGX: LONG</b>"
        chain = f"{'🔴' * min(streak, 6)} ({streak} закр.) ➔ 🟢 (1 закр.) ➔ 🎯 {trigger_reason}"

    lines = [
        header,
        "",
        f"📌 <b>Пара:</b> <code>{bingx_pair}</code> (<a href='{chart_url}'>График BingX</a>)",
        f"💰 <b>Цена входа:</b> <code>{price}</code>",
        f"🧭 <b>Старшие ТФ:</b> {mtf_mode}",
        f"📈 <b>RSI (1H):</b> {rsi_val:.1f}",
        f"🎯 <b>Точка входа:</b> {trigger_reason}",
        f"⏱ <b>До закрытия 1H:</b> {rem_min:.1f} мин.",
        "",
        "📊 <b>Цепочка Heikin Ashi:</b>",
        chain,
        f"⚙️ <b>Статус ордера:</b> {order_status}"
    ]
    return "\n".join(lines)


def build_inline_keyboard(signal_id: str) -> dict:
    current_amount = get_trade_amount()
    return {
        "inline_keyboard": [
            [
                {"text": f"🚀 Открыть сделку (${current_amount})", "callback_data": f"TRADE|{signal_id}"}
            ]
        ]
    }


async def send_telegram_signal(session: aiohttp.ClientSession, data: dict) -> bool:
    target_chat = get_target_chat_id()
    if not target_chat: return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": build_signal_message(data),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": build_inline_keyboard(data["signal_id"])
    }
    async with session.post(url, json=payload, timeout=10) as resp:
        return resp.status == 200


async def send_telegram_file(session: aiohttp.ClientSession, chat_id: str):
    init_csv()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    with open(LOG_FILE, "rb") as f:
        file_bytes = f.read()

    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("caption", "📁 Файл статистики сигналов и сделок.")
    form.add_field("document", file_bytes, filename="signals_log.csv", content_type="text/csv")
    await session.post(url, data=form, timeout=20)


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

                    if chat_id: save_target_chat_id(chat_id)

                    if cb_data.startswith("TRADE|"):
                        _, sig_id = cb_data.split("|", 1)
                        target_signal = None
                        init_csv()
                        with open(LOG_FILE, mode="r", newline="", encoding="utf-8") as f:
                            reader = csv.DictReader(f)
                            for row in reader:
                                if row["signal_id"] == sig_id:
                                    target_signal = row
                                    break

                        if target_signal:
                            await session.post(f"{base_url}/answerCallbackQuery", json={
                                "callback_query_id": cb_id,
                                "text": f"⏳ Отправка ордера на маржу ${get_trade_amount()}..."
                            })

                            trade_result_msg = await execute_bingx_trade(
                                target_signal["ticker"],
                                target_signal["direction"],
                                float(target_signal["entry_price"])
                            )

                            update_signal_order_status(sig_id, "В СДЕЛКЕ" if "✅" in trade_result_msg else "ОШИБКА")
                            target_signal["order_status"] = "В СДЕЛКЕ" if "✅" in trade_result_msg else "ОШИБКА"

                            edit_payload = {
                                "chat_id": chat_id,
                                "message_id": message_id,
                                "text": build_signal_message(target_signal) + f"\n\n<code>{trade_result_msg}</code>",
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True
                            }
                            await session.post(f"{base_url}/editMessageText", json=edit_payload)
                        else:
                            await session.post(f"{base_url}/answerCallbackQuery", json={
                                "callback_query_id": cb_id,
                                "text": "❌ Сигнал не найден в базе."
                            })

                elif "message" in update:
                    msg = update["message"]
                    text = msg.get("text", "").strip()
                    chat_id = msg.get("chat", {}).get("id")

                    if chat_id: save_target_chat_id(chat_id)

                    try:
                        new_val = float(text.replace(",", "."))
                        if 0.1 <= new_val <= 10000:
                            save_trade_amount(new_val)
                            await session.post(f"{base_url}/sendMessage", json={
                                "chat_id": chat_id,
                                "text": f"✅ Сумма сделки успешно изменена на <b>${new_val}</b>!\nТеперь кнопка будет открывать позиции на эту сумму.",
                                "parse_mode": "HTML"
                            })
                            continue
                    except ValueError:
                        pass

                    if text.startswith("/amount"):
                        parts = text.split()
                        if len(parts) > 1:
                            try:
                                new_val = float(parts[1])
                                save_trade_amount(new_val)
                                await session.post(f"{base_url}/sendMessage", json={
                                    "chat_id": chat_id,
                                    "text": f"✅ Сумма сделки установлена: <b>${new_val}</b>",
                                    "parse_mode": "HTML"
                                })
                                continue
                            except ValueError:
                                pass
                        await session.post(f"{base_url}/sendMessage", json={
                            "chat_id": chat_id,
                            "text": f"Текущая сумма сделки: <b>${get_trade_amount()}</b>.\nОтправь число в чат или команду /amount [сумма], чтобы изменить её.",
                            "parse_mode": "HTML"
                        })

                    elif text.startswith("/stats"):
                        summary = get_stats_summary()
                        await session.post(f"{base_url}/sendMessage", json={"chat_id": chat_id, "text": summary, "parse_mode": "HTML"})
                    elif text.startswith("/export"):
                        await send_telegram_file(session, chat_id)
                    elif text.startswith("/status") or text.startswith("/start"):
                        saved_cid = get_target_chat_id()
                        cur_amt = get_trade_amount()
                        status_lines = [
                            "🤖 <b>Бот BingX Trading Terminal активен</b>",
                            f"📩 <b>Chat ID:</b> <code>{saved_cid}</code> (подключен ✅)",
                            f"💧 <b>Ликвидных пар в работе:</b> {len(active_symbols)}",
                            f"💵 <b>Сумма сделки:</b> <b>${cur_amt}</b> | <b>SL:</b> -90% | <b>TP:</b> +110%",
                            "",
                            "💡 <i>Чтобы изменить сумму сделки, просто отправь в чат число (например: <code>10</code> или <code>5.5</code>) или команду /amount [сумма].</i>"
                        ]
                        await session.post(f"{base_url}/sendMessage", json={"chat_id": chat_id, "text": "\n".join(status_lines), "parse_mode": "HTML"})

        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)


# ==================== ЗАГРУЗКА И СКАНИРОВАНИЕ BINGX ====================
async def load_liquid_bingx_swaps(exchange):
    markets = await exchange.load_markets(True)
    all_swaps = {}
    for sym, m in markets.items():
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True):
            raw_id = m.get("id", "").replace("-", "").replace("_", "")
            if not raw_id: raw_id = sym.split(":")[0].replace("/", "")
            all_swaps[sym] = raw_id

    liquid_resolved = {}
    try:
        tickers = await exchange.fetch_tickers(list(all_swaps.keys()))
        for sym, t in tickers.items():
            if sym not in all_swaps: continue
            qv = t.get("quoteVolume") or t.get("info", {}).get("turnover", 0.0)
            if qv and float(qv) >= MIN_24H_VOLUME_USDT:
                liquid_resolved[all_swaps[sym]] = sym
    except Exception:
        pass

    if len(liquid_resolved) < 20:
        for sym, raw_id in all_swaps.items():
            liquid_resolved[raw_id] = sym

    return liquid_resolved


async def scan_hourly_candidates(exchange, hour_open_ts):
    global is_scanning
    is_scanning = True
    candidates = {}
    hour_ms = 3600 * 1000
    prev_closed_ts = hour_open_ts - hour_ms
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def check_one(ticker, ccxt_sym):
        async with sem:
            await asyncio.sleep(0.04)
            try:
                ohlcv_1h = await exchange.fetch_ohlcv(ccxt_sym, timeframe="1h", limit=HISTORY_LIMIT)
            except Exception:
                return

            if not ohlcv_1h or len(ohlcv_1h) < 55: return
            closed_1h = [c for c in ohlcv_1h if c[0] < hour_open_ts]
            if len(closed_1h) < 40 or closed_1h[-1][0] != prev_closed_ts: return

            ha_1h = calculate_heikin_ashi(closed_1h)
            reversal_1h = ha_1h[-1]
            idx_before_rev = len(ha_1h) - 2

            target_dir = None
            streak_1h = 0
            if reversal_1h["color"] == "RED":
                streak_1h = count_consecutive_color(ha_1h, idx_before_rev, "GREEN")
                if streak_1h >= 3: target_dir = "SHORT"
            elif reversal_1h["color"] == "GREEN":
                streak_1h = count_consecutive_color(ha_1h, idx_before_rev, "RED")
                if streak_1h >= 3: target_dir = "LONG"

            if not target_dir: return

            streak_slice = ha_1h[idx_before_rev - streak_1h + 1 : idx_before_rev + 1]
            avg_streak_body = sum(c["body_pct"] for c in streak_slice) / len(streak_slice)
            if reversal_1h["body_pct"] < max(REV_MIN_ABS_BODY_PCT, avg_streak_body * REV_MIN_STRENGTH_RATIO):
                return

            ohlcv_6h = build_6h_ohlcv_from_1h(closed_1h)
            color_6h = get_htf_color(calculate_heikin_ashi(ohlcv_6h))
            if target_dir == "SHORT" and color_6h != "RED": return
            if target_dir == "LONG" and color_6h != "GREEN": return

            try:
                ohlcv_1d = await exchange.fetch_ohlcv(ccxt_sym, timeframe="1d", limit=40)
            except Exception:
                return

            if not ohlcv_1d or len(ohlcv_1d) < 5: return
            color_1d = get_htf_color(calculate_heikin_ashi(ohlcv_1d))
            recent_rsi = [c["rsi"] for c in ha_1h[-3:]]

            mtf_mode = None
            peak_rsi = reversal_1h["rsi"]
            if target_dir == "SHORT":
                peak_rsi = max(recent_rsi)
                if color_1d == "RED" and peak_rsi >= 48.0: mtf_mode = "1D (🔴) + 6H (🔴)"
                elif peak_rsi >= RSI_SHORT_OVERBOUGHT: mtf_mode = f"Перекупленность RSI {peak_rsi:.0f}"
            else:
                peak_rsi = min(recent_rsi)
                if color_1d == "GREEN" and peak_rsi <= 52.0: mtf_mode = "1D (🟢) + 6H (🟢)"
                elif peak_rsi <= RSI_LONG_OVERSOLD: mtf_mode = f"Перепроданность RSI {peak_rsi:.0f}"

            if not mtf_mode: return

            recent_ranges = [abs(c["real_high"] - c["real_low"]) for c in ha_1h[-5:]]
            avg_recent_range = sum(recent_ranges) / len(recent_ranges)
            rev_real_range = max(
                abs(reversal_1h["real_open"] - reversal_1h["real_close"]),
                abs(reversal_1h["ha_open"] - reversal_1h["ha_close"]),
                avg_recent_range * 0.55
            )

            candidates[ticker] = {
                "target_dir": target_dir, "streak": streak_1h,
                "rev_body_pct": reversal_1h["body_pct"], "rev_range": rev_real_range,
                "peak_rsi": peak_rsi, "mtf_mode": mtf_mode
            }

    try:
        await asyncio.gather(*(check_one(t, s) for t, s in active_symbols.items()))
    finally:
        is_scanning = False
    return candidates


async def monitor_candidate_live(exchange, session, sem, ticker, info, server_time_ms, hour_open_ts, rem_min):
    if (ticker, hour_open_ts) in sent_signals or ticker not in active_symbols: return
    ccxt_sym = active_symbols[ticker]

    async with sem:
        try: ohlcv = await exchange.fetch_ohlcv(ccxt_sym, timeframe="1h", limit=50)
        except Exception: return

    if not ohlcv or len(ohlcv) < 20: return
    ha_candles = calculate_heikin_ashi(ohlcv)
    live_candle = ha_candles[-1]
    if live_candle["timestamp"] != hour_open_ts: return

    open_price = live_candle["real_open"]
    current_price = live_candle["real_close"]
    high_price = live_candle["real_high"]
    low_price = live_candle["real_low"]
    rev_range = info["rev_range"]
    target_dir = info["target_dir"]

    trigger_reason = None
    if target_dir == "SHORT":
        max_pullback_abs = high_price - open_price
        max_pullback_pct = (max_pullback_abs / open_price) * 100.0 if open_price > 0 else 0.0
        retrace_ratio = (max_pullback_abs / rev_range) if rev_range > 0 else 0.0
        pullback_ok = max_pullback_pct >= MIN_PULLBACK_PCT and (retrace_ratio >= PULLBACK_RETRACE_RATIO or max_pullback_pct >= DIRECT_PULLBACK_PCT)

        if pullback_ok and current_price > open_price:
            bounce_abs = high_price - current_price
            bounce_pct = (bounce_abs / high_price) * 100.0 if high_price > 0 else 0.0
            bounce_ratio = (bounce_abs / max_pullback_abs) if max_pullback_abs > 0 else 0.0
            if bounce_pct >= MIN_BOUNCE_PCT and 0.22 <= bounce_ratio <= 0.85:
                trigger_reason = f"Пик +{max_pullback_pct:.2f}% ➔ Отбой вниз -{bounce_pct:.2f}%"

    elif target_dir == "LONG":
        max_pullback_abs = open_price - low_price
        max_pullback_pct = (max_pullback_abs / open_price) * 100.0 if open_price > 0 else 0.0
        retrace_ratio = (max_pullback_abs / rev_range) if rev_range > 0 else 0.0
        pullback_ok = max_pullback_pct >= MIN_PULLBACK_PCT and (retrace_ratio >= PULLBACK_RETRACE_RATIO or max_pullback_pct >= DIRECT_PULLBACK_PCT)

        if pullback_ok and current_price < open_price:
            bounce_abs = current_price - low_price
            bounce_pct = (bounce_abs / low_price) * 100.0 if low_price > 0 else 0.0
            bounce_ratio = (bounce_abs / max_pullback_abs) if max_pullback_abs > 0 else 0.0
            if bounce_pct >= MIN_BOUNCE_PCT and 0.22 <= bounce_ratio <= 0.85:
                trigger_reason = f"Дно -{max_pullback_pct:.2f}% ➔ Отбой вверх +{bounce_pct:.2f}%"

    if trigger_reason:
        trigger_confirm_counter[ticker] = trigger_confirm_counter.get(ticker, 0) + 1
    else:
        trigger_confirm_counter[ticker] = 0
        return

    if trigger_confirm_counter[ticker] >= CONFIRM_CHECKS:
        dt_str = datetime.datetime.fromtimestamp(server_time_ms / 1000.0, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        signal_id = f"{ticker}_{hour_open_ts // 1000}"
        signal_data = {
            "signal_id": signal_id, "signal_time_utc": dt_str, "ticker": ticker,
            "direction": target_dir, "entry_price": current_price, "remaining_minutes": f"{rem_min:.1f}",
            "streak_candles": info["streak"], "rsi_value": f"{info['peak_rsi']:.1f}",
            "mtf_mode": info["mtf_mode"], "trigger_reason": trigger_reason, "order_status": "Ожидает",
            "updated_time_utc": dt_str
        }

        sent_ok = await send_telegram_signal(session, signal_data)
        if sent_ok:
            sent_signals.add((ticker, hour_open_ts))
            log_new_signal(signal_data)


async def main():
    global active_symbols, current_hour_ts, hourly_candidates, trigger_confirm_counter
    init_csv()
    exchange = ccxt.bingx({"enableRateLimit": False, "options": {"defaultType": "swap"}})
    live_sem = asyncio.Semaphore(LIVE_CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        active_symbols = await load_liquid_bingx_swaps(exchange)
        tg_task = asyncio.create_task(telegram_polling_worker(session))

        try:
            while True:
                try:
                    t_resp = await exchange.fetch_time()
                    server_time_ms = int(t_resp) if t_resp else int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
                    hour_ms = 3600 * 1000
                    hour_open_ts = (server_time_ms // hour_ms) * hour_ms
                    remaining_minutes = ((hour_open_ts + hour_ms) - server_time_ms) / 60000.0

                    if hour_open_ts != current_hour_ts and remaining_minutes <= SCAN_MAX_REMAINING_MIN:
                        current_hour_ts = hour_open_ts
                        trigger_confirm_counter.clear()
                        try: active_symbols = await load_liquid_bingx_swaps(exchange)
                        except Exception: pass
                        hourly_candidates = await scan_hourly_candidates(exchange, hour_open_ts)

                    if MIN_REMAINING_MINUTES <= remaining_minutes <= ENTRY_MAX_REMAINING_MIN and hourly_candidates:
                        tasks = [
                            monitor_candidate_live(exchange, session, live_sem, ticker, info, server_time_ms, hour_open_ts, remaining_minutes)
                            for ticker, info in hourly_candidates.items() if (ticker, hour_open_ts) not in sent_signals
                        ]
                        if tasks: await asyncio.gather(*tasks)

                except Exception:
                    pass
                await asyncio.sleep(LIVE_POLL_INTERVAL)
        finally:
            tg_task.cancel()
            await exchange.close()

if __name__ == "__main__":
    asyncio.run(main())
