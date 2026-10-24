import os
import csv
import time
import asyncio
import aiohttp
import ccxt.async_support as ccxt
from datetime import datetime, timezone

# ==================== 1. КЛЮЧИ BYBIT И TELEGRAM ====================
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "ВСТАВЬ_СВОЙ_BYBIT_API_KEY")
BYBIT_SECRET_KEY = os.getenv("BYBIT_SECRET_KEY", "ВСТАВЬ_СВОЙ_BYBIT_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВСТАВЬ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "ВСТАВЬ_СВОЙ_CHAT_ID")

# ==================== 2. РИСК-МЕНЕДЖМЕНТ И СЛОЖНЫЙ ПРОЦЕНТ ====================
# False = БОЕВАЯ ТОРГОВЛЯ НА BYBIT | True = ТЕСТОВЫЙ РЕЖИМ (без реальных ордеров)
DRY_RUN = False

STRATEGY_PROFILE = 1            # 1 -> TP +80% / SL -60% (без БУ) | 2 -> TP +120% / SL -60% (с ПлюсБУ)

# Настройки сложного процента:
MARGIN_PCT_OF_BALANCE = 0.05    # 5% от текущего баланса USDT на 1 сделку (гибридный сложный %)
MIN_MARGIN_USD = 0.50           # Минимальный порог маржи ($0.50) для прохождения лимита Bybit
MAX_MARGIN_USD = 100.0          # Защитный потолок маржи на 1 сделку

LEVERAGE = 15                   # Плечо 15x
MARGIN_MODE = "isolated"        # Изолированная маржа
MAX_OPEN_POSITIONS = 5          # Максимум одновременно открытых сделок
PAIRS_TO_SCAN = 60              # Количество доступных пар Bybit в ротации
BYBIT_ROUND_TRIP_FEE = 0.0011   # Комиссия Bybit Futures (0.11% за круг)

if STRATEGY_PROFILE == 1:
    TP_PCT_OF_MARGIN = 0.80
    SL_PCT_OF_MARGIN = 0.60
    USE_PROFIT_LOCK = False
else:
    TP_PCT_OF_MARGIN = 1.20
    SL_PCT_OF_MARGIN = 0.60
    USE_PROFIT_LOCK = True

LOCK_TRIGGER_MARGIN_PCT = 0.50  # Активация ПлюсБУ при +50% от маржи
LOCK_SL_MARGIN_PCT = 0.10       # Перенос стопа в +10% от маржи

# ==================== 3. ПАРАМЕТРЫ СТРАТЕГИИ (1H SNIPER) ====================
TIMEFRAME = "1h"
EMA_PERIOD = 20
MIN_HA_BODY_PCT = 0.35          # Тело свечи Heikin Ashi >= 0.35%
MAX_OPPOSITE_WICK_PCT = 0.15    # Обратная тень <= 0.15%
MAX_HOLD_HOURS = 48             # Таймаут принудительного закрытия сделки

CSV_LOG_FILE = "bybit_trades_log.csv"
SCAN_INTERVAL_SEC = 30
GUARDIAN_INTERVAL_SEC = 15
# ===========================================================================

active_trades = {}
entered_bar_cache = {}
current_trade_margin_usd = MIN_MARGIN_USD


def init_csv():
    if not os.path.exists(CSV_LOG_FILE):
        with open(CSV_LOG_FILE, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "open_time", "close_time", "symbol", "direction",
                "entry_price", "exit_price", "tp_price", "sl_price",
                "margin_usd", "leverage", "result", "net_pnl_usd"
            ])


def log_closed_trade(trade, exit_price, result_label, net_pnl):
    with open(CSV_LOG_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            trade["open_time"],
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            trade["symbol"],
            trade["direction"],
            f"{trade['entry_price']:.6f}",
            f"{exit_price:.6f}",
            f"{trade['tp_price']:.6f}",
            f"{trade['sl_price']:.6f}",
            trade["margin_usd"],
            trade["leverage"],
            result_label,
            f"{net_pnl:.4f}"
        ])


async def send_tg_message(session: aiohttp.ClientSession, text: str):
    print(f"\n📢 [УВЕДОМЛЕНИЕ]:\n{text}\n", flush=True)
    if not TELEGRAM_BOT_TOKEN or "ВСТАВЬ" in TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            await resp.read()
    except Exception as e:
        print(f"⚠️ Ошибка отправки в Telegram: {e}", flush=True)


async def send_tg_document(session: aiohttp.ClientSession, filepath: str):
    if not os.path.exists(filepath):
        await send_tg_message(session, "📂 Лог сделок пока пуст.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", str(TELEGRAM_CHAT_ID))
        with open(filepath, "rb") as file_obj:
            data.add_field("document", file_obj, filename=os.path.basename(filepath))
            async with session.post(url, data=data, timeout=20) as resp:
                await resp.read()
    except Exception as e:
        print(f"⚠️ Ошибка отправки CSV в Telegram: {e}", flush=True)


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
        color = "GREEN" if ha_close > ha_open else "RED"

        upper_wick_pct = (ha_high - max(ha_open, ha_close)) / ha_open * 100.0 if ha_open > 0 else 0.0
        lower_wick_pct = (min(ha_open, ha_close) - ha_low) / ha_open * 100.0 if ha_open > 0 else 0.0

        ha_candles.append({
            "timestamp": ts,
            "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low, "ha_close": ha_close,
            "real_open": o, "real_high": h, "real_low": l, "real_close": c,
            "body_pct": body_pct, "color": color,
            "upper_wick_pct": upper_wick_pct, "lower_wick_pct": lower_wick_pct
        })
    return ha_candles


def calculate_ema(closes, period):
    if len(closes) < period:
        return [sum(closes) / len(closes)] * len(closes)
    ema = [sum(closes[:period]) / period]
    multiplier = 2.0 / (period + 1.0)
    for price in closes[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return [ema[0]] * (period - 1) + ema


async def update_dynamic_margin(exchange):
    """Рассчитывает рабочую маржу по сложному проценту от баланса Bybit."""
    global current_trade_margin_usd

    if DRY_RUN or "ВСТАВЬ" in BYBIT_API_KEY:
        current_trade_margin_usd = MIN_MARGIN_USD
        return 0.0, current_trade_margin_usd, "ТЕСТОВЫЙ РЕЖИМ"

    try:
        bal = await exchange.fetch_balance()
        usdt_free = float(bal.get("USDT", {}).get("free", 0.0) or 0.0)
        usdt_total = float(bal.get("USDT", {}).get("total", 0.0) or 0.0)
        effective_balance = max(usdt_total, usdt_free)

        if effective_balance > 0:
            raw_margin = effective_balance * MARGIN_PCT_OF_BALANCE
            calculated_margin = max(MIN_MARGIN_USD, min(raw_margin, MAX_MARGIN_USD))
            current_trade_margin_usd = round(calculated_margin, 2)
        else:
            current_trade_margin_usd = MIN_MARGIN_USD

        status_str = f"Баланс: ${effective_balance:.2f} | Маржа ({int(MARGIN_PCT_OF_BALANCE*100)}%): ${current_trade_margin_usd:.2f}"
        return usdt_free, current_trade_margin_usd, status_str
    except Exception as e:
        return 0.0, current_trade_margin_usd, f"Ошибка баланса: {e}"


async def execute_bybit_entry(exchange, symbol, direction, entry_price, sl_price, tp_price, margin_to_use):
    """Открывает рыночный ордер на Bybit V5 с динамической маржой."""
    target_notional_usd = margin_to_use * LEVERAGE
    raw_amount = target_notional_usd / entry_price

    market = exchange.market(symbol)
    min_amount = float(market.get("limits", {}).get("amount", {}).get("min") or 0.0)

    if min_amount > 0 and raw_amount < min_amount:
        if (min_amount * entry_price) <= (target_notional_usd * 1.4):
            raw_amount = min_amount
        else:
            return False, 0.0, f"Мин. лот ({min_amount}) дороже бюджета ${target_notional_usd:.2f}"

    amount = float(exchange.amount_to_precision(symbol, raw_amount))
    if amount <= 0:
        return False, 0.0, "Объем равен 0"

    sl_str = str(exchange.price_to_precision(symbol, sl_price))
    tp_str = str(exchange.price_to_precision(symbol, tp_price))

    if DRY_RUN:
        return True, amount, 0

    try:
        await exchange.set_margin_mode(MARGIN_MODE, symbol, params={"leverage": str(LEVERAGE)})
    except Exception:
        pass

    try:
        await exchange.set_leverage(LEVERAGE, symbol)
    except Exception:
        pass

    order_side = "buy" if direction == "LONG" else "sell"
    hedge_idx = 1 if direction == "LONG" else 2
    last_err = "Unknown error"

    for pos_idx in [0, hedge_idx]:
        try:
            params = {
                "positionIdx": pos_idx,
                "takeProfit": tp_str,
                "stopLoss": sl_str,
                "tpslMode": "Full",
                "tpOrderType": "Market",
                "slOrderType": "Market"
            }
            await exchange.create_order(symbol, "market", order_side, amount, None, params=params)
            return True, amount, pos_idx
        except Exception:
            try:
                await exchange.create_order(
                    symbol, "market", order_side, amount, None, params={"positionIdx": pos_idx}
                )
                return True, amount, pos_idx
            except Exception as inner_e:
                last_err = str(inner_e)
                continue

    return False, 0.0, last_err


async def close_bybit_position_market(exchange, trade):
    if DRY_RUN:
        return True
    symbol = trade["symbol"]
    close_side = "sell" if trade["direction"] == "LONG" else "buy"
    pos_idx = trade.get("pos_idx", 0)
    amount = trade["amount"]

    try:
        params = {"positionIdx": pos_idx, "reduceOnly": True}
        await exchange.create_order(symbol, "market", close_side, amount, None, params=params)
        return True
    except Exception as e:
        print(f"⚠️ Ошибка закрытия позиции {symbol}: {e}", flush=True)
        return False


async def update_bybit_trading_stop(exchange, trade, new_sl_price):
    if DRY_RUN:
        return
    try:
        symbol = trade["symbol"]
        market = exchange.market(symbol)
        sl_str = str(exchange.price_to_precision(symbol, new_sl_price))
        pos_idx = trade.get("pos_idx", 0)
        await exchange.private_post_v5_position_trading_stop({
            "category": "linear",
            "symbol": market["id"],
            "stopLoss": sl_str,
            "tpslMode": "Full",
            "positionIdx": pos_idx
        })
    except Exception as e:
        print(f"ℹ️ Биржевой SL контролируется Стражем: {e}", flush=True)


async def sync_exchange_positions(exchange):
    if DRY_RUN:
        return
    try:
        positions = await exchange.fetch_positions()
        live_symbols = set()
        for p in positions:
            contracts = float(p.get("contracts") or 0.0)
            if contracts > 0:
                live_symbols.add(p["symbol"])
        for sym in list(active_trades.keys()):
            if sym not in live_symbols:
                active_trades.pop(sym, None)
                print(f"ℹ️ Позиция {sym} закрыта биржевым триггером Bybit.", flush=True)
    except Exception as e:
        print(f"⚠️ Ошибка синхронизации позиций: {e}", flush=True)


async def guardian_loop(exchange, session: aiohttp.ClientSession):
    while True:
        try:
            if active_trades:
                await sync_exchange_positions(exchange)

            for sym, trade in list(active_trades.items()):
                try:
                    ticker = await exchange.fetch_ticker(sym)
                    curr_price = float(ticker["last"])
                except Exception:
                    continue

                direction = trade["direction"]
                entry_p = trade["entry_price"]
                tp_p = trade["tp_price"]
                sl_p = trade["sl_price"]

                if USE_PROFIT_LOCK and not trade["lock_active"]:
                    reached_lock = (
                        (direction == "LONG" and curr_price >= trade["lock_trigger_price"]) or
                        (direction == "SHORT" and curr_price <= trade["lock_trigger_price"])
                    )
                    if reached_lock:
                        trade["lock_active"] = True
                        trade["sl_price"] = trade["locked_sl_price"]
                        sl_p = trade["sl_price"]
                        await update_bybit_trading_stop(exchange, trade, sl_p)
                        await send_tg_message(
                            session,
                            f"🛡 <b>ЗАЩИТА ПРИБЫЛИ BYBIT (ПлюсБУ +10%)</b>\n"
                            f"Монета: <b>{sym}</b> ({direction})\n"
                            f"Стоп перенесен в плюс: <code>{sl_p:.6f}</code>"
                        )

                hit_tp = (curr_price >= tp_p) if direction == "LONG" else (curr_price <= tp_p)
                hit_sl = (curr_price <= sl_p) if direction == "LONG" else (curr_price >= sl_p)
                hold_hours = (time.time() - trade["open_ts"]) / 3600.0
                hit_timeout = hold_hours >= MAX_HOLD_HOURS

                if hit_tp or hit_sl or hit_timeout:
                    closed = await close_bybit_position_market(exchange, trade)
                    if closed:
                        active_trades.pop(sym, None)
                        pos_usd = trade["amount"] * entry_p
                        fee_usd = pos_usd * BYBIT_ROUND_TRIP_FEE

                        if direction == "LONG":
                            gross_pnl = ((curr_price - entry_p) / entry_p) * pos_usd
                        else:
                            gross_pnl = ((entry_p - curr_price) / entry_p) * pos_usd

                        net_pnl = gross_pnl - fee_usd
                        reason = "TAKE PROFIT ✅" if hit_tp else ("PLUS-BU 🛡" if trade["lock_active"] and hit_sl else ("STOP LOSS ❌" if hit_sl else "TIMEOUT ⏱"))

                        log_closed_trade(trade, curr_price, reason, net_pnl)
                        _, new_m, _ = await update_dynamic_margin(exchange)
                        await send_tg_message(
                            session,
                            f"🏁 <b>СДЕЛКА ЗАКРЫТА: {reason}</b>\n"
                            f"Монета: <b>{sym}</b> ({direction})\n"
                            f"Чистый PnL: <b>${net_pnl:+.3f}</b>\n"
                            f"📈 Сложный %: след. маржа <b>${new_m:.2f}</b>"
                        )
        except Exception as e:
            print(f"⚠️ Ошибка в Guardian Loop: {e}", flush=True)

        await asyncio.sleep(GUARDIAN_INTERVAL_SEC)


def check_candle_setup(prev_c, curr_c, ema_now, ema_prev, current_price):
    if prev_c["color"] == curr_c["color"]:
        return None, "нет смены цвета"

    if curr_c["body_pct"] < MIN_HA_BODY_PCT:
        return None, f"тело {curr_c['body_pct']:.2f}%"

    if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
        if current_price > ema_now and ema_now > ema_prev:
            if curr_c["lower_wick_pct"] <= MAX_OPPOSITE_WICK_PCT:
                return "LONG", "OK"
            return None, f"нижняя тень {curr_c['lower_wick_pct']:.2f}%"
        return None, "против EMA20"

    if prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
        if current_price < ema_now and ema_now < ema_prev:
            if curr_c["upper_wick_pct"] <= MAX_OPPOSITE_WICK_PCT:
                return "SHORT", "OK"
            return None, f"верхняя тень {curr_c['upper_wick_pct']:.2f}%"
        return None, "против EMA20"

    return None, "нет условий"


async def filter_affordable_bybit_pairs(exchange, all_swap_symbols, margin_usd):
    max_allowed_notional = margin_usd * LEVERAGE * 1.4
    affordable = []
    try:
        tickers = await exchange.fetch_tickers(all_swap_symbols)
        for sym in all_swap_symbols:
            market = exchange.market(sym)
            min_amt = float(market.get("limits", {}).get("amount", {}).get("min") or 0.0)
            last_price = float(tickers.get(sym, {}).get("last") or 0.0)
            if last_price <= 0:
                continue
            if (min_amt * last_price) <= max_allowed_notional:
                affordable.append(sym)
            if len(affordable) >= PAIRS_TO_SCAN:
                break
    except Exception:
        affordable = all_swap_symbols[:PAIRS_TO_SCAN]
    return affordable


async def scanner_loop(exchange, session: aiohttp.ClientSession, all_swaps: list, initial_symbols: list):
    valid_symbols = initial_symbols
    last_refresh_ts = time.time()

    print(f"🔍 Сканер Bybit запущен (Сложный %: {int(MARGIN_PCT_OF_BALANCE*100)}% от депо)...", flush=True)
    while True:
        try:
            if len(active_trades) >= MAX_OPEN_POSITIONS:
                await asyncio.sleep(SCAN_INTERVAL_SEC)
                continue

            if time.time() - last_refresh_ts >= 3600:
                _, updated_m, _ = await update_dynamic_margin(exchange)
                valid_symbols = await filter_affordable_bybit_pairs(exchange, all_swaps, updated_m)
                last_refresh_ts = time.time()

            scanned_count = 0
            color_flips = 0
            near_misses = []

            for sym in valid_symbols:
                if len(active_trades) >= MAX_OPEN_POSITIONS:
                    break
                if sym in active_trades:
                    continue

                try:
                    ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=55)
                    if not ohlcv or len(ohlcv) < 35:
                        continue

                    scanned_count += 1
                    ha_candles = calculate_heikin_ashi(ohlcv)
                    closes = [c["real_close"] for c in ha_candles]
                    ema20 = calculate_ema(closes, EMA_PERIOD)

                    current_price = ha_candles[-1]["real_close"]

                    candidates = [
                        ("ЗАКРЫТАЯ 1H", ha_candles[-3], ha_candles[-2], ema20[-2], ema20[-5]),
                        ("ТЕКУЩАЯ 1H", ha_candles[-2], ha_candles[-1], ema20[-1], ema20[-4])
                    ]

                    triggered_dir = None
                    triggered_candle = None
                    triggered_label = ""

                    for label, p_c, c_c, e_now, e_prev in candidates:
                        bar_ts = c_c["timestamp"]
                        if entered_bar_cache.get(sym) == bar_ts:
                            continue

                        if p_c["color"] != c_c["color"]:
                            color_flips += 1

                        direction, status_note = check_candle_setup(p_c, c_c, e_now, e_prev, current_price)
                        if direction:
                            triggered_dir = direction
                            triggered_candle = c_c
                            triggered_label = label
                            break
                        elif p_c["color"] != c_c["color"] and c_c["body_pct"] >= 0.20:
                            near_misses.append(f"{sym} ({status_note})")

                    if not triggered_dir:
                        continue

                    free_usdt, trade_margin, _ = await update_dynamic_margin(exchange)
                    if not DRY_RUN and free_usdt > 0 and free_usdt < trade_margin:
                        continue

                    pos_size_usd = trade_margin * LEVERAGE
                    sl_delta = (trade_margin * SL_PCT_OF_MARGIN / pos_size_usd) * current_price
                    tp_delta = (trade_margin * TP_PCT_OF_MARGIN / pos_size_usd) * current_price
                    lock_trig_delta = (trade_margin * LOCK_TRIGGER_MARGIN_PCT / pos_size_usd) * current_price
                    lock_sl_delta = (trade_margin * LOCK_SL_MARGIN_PCT / pos_size_usd) * current_price

                    if triggered_dir == "LONG":
                        sl_price = current_price - sl_delta
                        tp_price = current_price + tp_delta
                        lock_trigger_p = current_price + lock_trig_delta
                        locked_sl_p = current_price + lock_sl_delta
                    else:
                        sl_price = current_price + sl_delta
                        tp_price = current_price - tp_delta
                        lock_trigger_p = current_price - lock_trig_delta
                        locked_sl_p = current_price - lock_sl_delta

                    ok, amount, idx_or_err = await execute_bybit_entry(
                        exchange, sym, triggered_dir, current_price, sl_price, tp_price, trade_margin
                    )

                    entered_bar_cache[sym] = triggered_candle["timestamp"]

                    if ok:
                        actual_notional = amount * current_price
                        actual_margin = actual_notional / LEVERAGE
                        active_trades[sym] = {
                            "symbol": sym,
                            "direction": triggered_dir,
                            "entry_price": current_price,
                            "tp_price": tp_price,
                            "sl_price": sl_price,
                            "lock_trigger_price": lock_trigger_p,
                            "locked_sl_price": locked_sl_p,
                            "lock_active": False,
                            "amount": amount,
                            "pos_idx": idx_or_err,
                            "margin_usd": round(actual_margin, 3),
                            "leverage": LEVERAGE,
                            "open_ts": time.time(),
                            "open_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        }

                        badge = "🧪 [ТЕСТОВЫЙ ОРДЕР]" if DRY_RUN else "🔥 [РЕАЛЬНАЯ СДЕЛКА BYBIT]"
                        emoji = "🟢" if triggered_dir == "LONG" else "🔴"
                        await send_tg_message(
                            session,
                            f"{badge}\n"
                            f"{emoji} <b>ОТКРЫТА СДЕЛКА: {triggered_dir}</b> ({triggered_label})\n"
                            f"🪙 Монета: <b>{sym}</b>\n"
                            f"💵 Вход: <code>{current_price:.6f}</code>\n"
                            f"📈 Сложный % маржа: <b>${actual_margin:.2f}</b> ({LEVERAGE}x)\n"
                            f"🎯 Тейк: <code>{tp_price:.6f}</code> | Стоп: <code>{sl_price:.6f}</code>"
                        )
                    else:
                        print(f"⚠️ Пропуск ордера {sym}: {idx_or_err}", flush=True)

                except Exception:
                    continue

            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            near_str = ", ".join(near_misses[:3]) if near_misses else "нет близких"
            print(
                f"[{now_str} UTC] Пар: {scanned_count} | Маржа: ${current_trade_margin_usd:.2f} | "
                f"Открыто: {len(active_trades)}/{MAX_OPEN_POSITIONS} | Ближайшие: {near_str}",
                flush=True
            )

        except Exception as e:
            print(f"⚠️ Ошибка в сканере: {e}", flush=True)

        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def telegram_commands_loop(exchange, session: aiohttp.ClientSession):
    if not TELEGRAM_BOT_TOKEN or "ВСТАВЬ" in TELEGRAM_BOT_TOKEN:
        return
    offset = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    while True:
        try:
            async with session.get(url, params={"offset": offset, "timeout": 20}, timeout=25) as resp:
                data = await resp.json()
                for item in data.get("result", []):
                    offset = item["update_id"] + 1
                    msg = item.get("message", {})
                    text = msg.get("text", "").strip()

                    if text == "/status":
                        if not active_trades:
                            await send_tg_message(session, f"📊 Открытых позиций нет. Маржа (сложный %): <b>${current_trade_margin_usd:.2f}</b>")
                        else:
                            lines = [f"📊 <b>Активные сделки ({len(active_trades)}/{MAX_OPEN_POSITIONS}):</b>"]
                            for s, t in active_trades.items():
                                lines.append(f"• <b>{s}</b> ({t['direction']}) | Маржа: ${t['margin_usd']:.2f}")
                            await send_tg_message(session, "\n".join(lines))

                    elif text == "/balance":
                        _, _, bal_text = await update_dynamic_margin(exchange)
                        await send_tg_message(session, f"💰 <b>Баланс Bybit:</b>\n{bal_text}")

                    elif text == "/export":
                        await send_tg_document(session, CSV_LOG_FILE)
        except Exception:
            await asyncio.sleep(5)


async def main():
    init_csv()
    print(f"🚀 Запуск боевого бота Bybit со сложным процентом...", flush=True)

    exchange = ccxt.bybit({
        "apiKey": BYBIT_API_KEY,
        "secret": BYBIT_SECRET_KEY,
        "enableRateLimit": True,
        "options": {"defaultType": "linear"}
    })

    markets = await exchange.load_markets(True)
    all_swaps = [
        sym for sym, m in markets.items()
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active", True)
    ]

    _, start_m, balance_info = await update_dynamic_margin(exchange)
    valid_symbols = await filter_affordable_bybit_pairs(exchange, all_swaps, start_m)
    print(f"💰 {balance_info} | Пар доступно: {len(valid_symbols)}", flush=True)

    async with aiohttp.ClientSession() as session:
        await send_tg_message(
            session,
            f"🤖 <b>Авто-бот Bybit (со сложным %) запущен!</b>\n"
            f"Режим: <b>{'ТЕСТОВЫЙ' if DRY_RUN else 'БОЕВОЙ'}</b>\n"
            f"Баланс: <b>{balance_info}</b>\n"
            f"Сложный процент: <b>Включен ({int(MARGIN_PCT_OF_BALANCE*100)}% от депо, мин. ${MIN_MARGIN_USD})</b>\n"
            f"Цели (Профиль #{STRATEGY_PROFILE}): <b>TP +{TP_PCT_OF_MARGIN*100:.0f}% / SL -{SL_PCT_OF_MARGIN*100:.0f}%</b>"
        )

        await asyncio.gather(
            scanner_loop(exchange, session, all_swaps, valid_symbols),
            guardian_loop(exchange, session),
            telegram_commands_loop(exchange, session)
        )


if __name__ == "__main__":
    asyncio.run(main())
