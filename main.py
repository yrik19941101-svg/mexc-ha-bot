import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== НАСТРОЙКИ ЧЕСТНОГО БЭКТЕСТА BYBIT (30 ДНЕЙ) ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 30           # 30 дней (720 часовых свечей)
FIXED_TRADE_AMOUNT = 0.50       # Твоя рабочая маржа: $0.50 на сделку
ASSUMED_LEVERAGE = 15           # Плечо 15x (объем позиции = $7.50)
BYBIT_FEE_RATE = 0.0011         # Комиссия Bybit Futures: 0.055% вход + 0.055% выход = 0.11%
MAX_HOLD_HOURS = 48             # Принудительное закрытие через 48 часов, если не сработал TP/SL
PAIRS_LIMIT = 50                # Топ-50 подходящих под маржу $0.50 пар Bybit

# Сетка параметров для проверки на рынке Bybit:
TP_GRID = [0.80, 1.20]                  # Тейк-профит: +80% и +120% от маржи
SL_GRID = [0.60, 0.80]                  # Стоп-лосс: -60% и -80% от маржи
MIN_BODY_GRID = [0.35, 0.50]            # Минимальное тело свечи HA (0.35% и 0.50%)
MAX_WICK_GRID = [0.08, 0.15]            # Максимальная обратная тень HA (0.08% и 0.15%)
PROFIT_LOCK_GRID = [False, True]        # Перенос стопа в +10% при достижении +50% профита


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
            "ha_open": ha_open,
            "ha_high": ha_high,
            "ha_low": ha_low,
            "ha_close": ha_close,
            "real_open": o,
            "real_high": h,
            "real_low": l,
            "real_close": c,
            "body_pct": body_pct,
            "color": color,
            "upper_wick_pct": upper_wick_pct,
            "lower_wick_pct": lower_wick_pct
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


async def get_affordable_bybit_symbols(exchange, all_swaps):
    """Отбирает пары Bybit, где минимальный ордер позволяет торговать с маржой $0.50 (15x)."""
    max_notional = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE * 1.4  # до ~$10.50
    affordable = []
    try:
        tickers = await exchange.fetch_tickers(all_swaps)
        for sym in all_swaps:
            market = exchange.market(sym)
            min_amt = float(market.get("limits", {}).get("amount", {}).get("min") or 0.0)
            last_price = float(tickers.get(sym, {}).get("last") or 0.0)
            if last_price <= 0:
                continue
            if (min_amt * last_price) <= max_notional:
                affordable.append(sym)
            if len(affordable) >= PAIRS_LIMIT:
                break
    except Exception:
        affordable = all_swaps[:PAIRS_LIMIT]
    return affordable


async def run_bybit_backtest():
    print(f"🚀 Запуск ЧЕСТНОГО бэктеста на BYBIT за {DAYS_TO_BACKTEST} дней (Маржа ${FIXED_TRADE_AMOUNT}, Плечо {ASSUMED_LEVERAGE}x)...", flush=True)
    exchange = ccxt.bybit({
        "enableRateLimit": True,
        "options": {"defaultType": "linear"}
    })

    markets = await exchange.load_markets(True)
    all_swaps = [
        sym for sym, m in markets.items()
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active", True)
    ]

    active_swaps = await get_affordable_bybit_symbols(exchange, all_swaps)
    print(f"📊 Отобрано {len(active_swaps)} доступных под маржу ${FIXED_TRADE_AMOUNT} пар. Скачиваем историю за {DAYS_TO_BACKTEST} дней...", flush=True)

    since_timestamp = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)
    cached_market_data = {}

    for sym in active_swaps:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_timestamp, limit=1000)
            if ohlcv and len(ohlcv) > 100:
                ha_candles = calculate_heikin_ashi(ohlcv)
                closes = [c["real_close"] for c in ha_candles]
                cached_market_data[sym] = {
                    "ha_candles": ha_candles,
                    "ema20": calculate_ema(closes, 20)
                }
        except Exception as e:
            print(f"⚠️ Пропуск {sym}: {e}", flush=True)

    await exchange.close()
    print(f"💾 Успешно закешировано пар Bybit: {len(cached_market_data)}. Перебираем комбинации...", flush=True)

    if not cached_market_data:
        print("❌ Не удалось загрузить данные с Bybit.", flush=True)
        return

    combinations = list(product(TP_GRID, SL_GRID, MIN_BODY_GRID, MAX_WICK_GRID, PROFIT_LOCK_GRID))
    results = []

    pos_size_usd = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE
    fee_per_trade_usd = pos_size_usd * BYBIT_FEE_RATE

    for tp_pct, sl_pct, min_body_pct, max_wick_pct, use_lock in combinations:
        wins = 0
        losses = 0
        total_pnl = 0.0
        total_fees = 0.0

        for sym, data in cached_market_data.items():
            ha_candles = data["ha_candles"]
            ema_values = data["ema20"]
            busy_until_idx = -1

            for idx in range(30, len(ha_candles) - MAX_HOLD_HOURS):
                if idx <= busy_until_idx:
                    continue

                prev_c = ha_candles[idx - 1]
                curr_c = ha_candles[idx]

                if curr_c["body_pct"] < min_body_pct:
                    continue

                current_price = curr_c["real_close"]
                ema_now = ema_values[idx]
                ema_prev = ema_values[idx - 3]

                target_dir = None

                # Вход CONTINUATION по тренду EMA 20 с проверкой обратной тени
                if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                    if current_price > ema_now and ema_now > ema_prev:
                        if curr_c["lower_wick_pct"] <= max_wick_pct:
                            target_dir = "LONG"
                elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                    if current_price < ema_now and ema_now < ema_prev:
                        if curr_c["upper_wick_pct"] <= max_wick_pct:
                            target_dir = "SHORT"

                if not target_dir:
                    continue

                entry_p = current_price
                sl_delta = (FIXED_TRADE_AMOUNT * sl_pct / pos_size_usd) * entry_p
                tp_delta = (FIXED_TRADE_AMOUNT * tp_pct / pos_size_usd) * entry_p
                lock_trigger_delta = (FIXED_TRADE_AMOUNT * 0.50 / pos_size_usd) * entry_p
                lock_sl_delta = (FIXED_TRADE_AMOUNT * 0.10 / pos_size_usd) * entry_p

                if target_dir == "LONG":
                    sl_price = entry_p - sl_delta
                    tp_price = entry_p + tp_delta
                    lock_trigger_p = entry_p + lock_trigger_delta
                    locked_sl_p = entry_p + lock_sl_delta
                else:
                    sl_price = entry_p + sl_delta
                    tp_price = entry_p - tp_delta
                    lock_trigger_p = entry_p - lock_trigger_delta
                    locked_sl_p = entry_p - lock_sl_delta

                trade_pnl_gross = None
                exit_idx = idx + MAX_HOLD_HOURS
                lock_active = False

                for offset in range(1, MAX_HOLD_HOURS + 1):
                    future_c = ha_candles[idx + offset]
                    f_high = future_c["real_high"]
                    f_low = future_c["real_low"]

                    current_sl_pct_val = 0.10 if lock_active else -sl_pct

                    if target_dir == "LONG":
                        hit_tp = f_high >= tp_price
                        hit_sl = f_low <= sl_price
                    else:
                        hit_tp = f_low <= tp_price
                        hit_sl = f_high >= sl_price

                    if hit_tp and hit_sl:
                        trade_pnl_gross = FIXED_TRADE_AMOUNT * current_sl_pct_val
                        exit_idx = idx + offset
                        break
                    elif hit_sl:
                        trade_pnl_gross = FIXED_TRADE_AMOUNT * current_sl_pct_val
                        exit_idx = idx + offset
                        break
                    elif hit_tp:
                        trade_pnl_gross = FIXED_TRADE_AMOUNT * tp_pct
                        exit_idx = idx + offset
                        break

                    if use_lock and not lock_active:
                        if target_dir == "LONG" and f_high >= lock_trigger_p:
                            lock_active = True
                            sl_price = locked_sl_p
                        elif target_dir == "SHORT" and f_low <= lock_trigger_p:
                            lock_active = True
                            sl_price = locked_sl_p

                if trade_pnl_gross is None:
                    last_close = ha_candles[exit_idx]["real_close"]
                    if target_dir == "LONG":
                        price_change_pct = (last_close - entry_p) / entry_p
                    else:
                        price_change_pct = (entry_p - last_close) / entry_p
                    trade_pnl_gross = price_change_pct * pos_size_usd

                busy_until_idx = exit_idx
                net_trade_pnl = trade_pnl_gross - fee_per_trade_usd
                total_pnl += net_trade_pnl
                total_fees += fee_per_trade_usd

                if net_trade_pnl > 0:
                    wins += 1
                else:
                    losses += 1

        total_trades = wins + losses
        if total_trades < 15:
            continue

        winrate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
        roi_on_margin_pct = (total_pnl / FIXED_TRADE_AMOUNT) * 100.0

        results.append({
            "tp": tp_pct, "sl": sl_pct, "body": min_body_pct,
            "wick": max_wick_pct, "lock": use_lock,
            "trades": total_trades, "winrate": winrate,
            "pnl": total_pnl, "fees": total_fees, "roi": roi_on_margin_pct
        })

    results.sort(key=lambda x: x["pnl"], reverse=True)

    print("\n" + "="*118, flush=True)
    print(f"🏆 ТОП-10 КОНФИГУРАЦИЙ НА BYBIT ЗА {DAYS_TO_BACKTEST} ДНЕЙ (Маржа ${FIXED_TRADE_AMOUNT:.2f}, Плечо {ASSUMED_LEVERAGE}x, Комиссия 0.11% учтена):", flush=True)
    print("="*118, flush=True)
    for i, r in enumerate(results[:10], 1):
        print(
            f"#{i:<2} | Чистый PnL: ${r['pnl']:>6.2f} (К марже: {r['roi']:>+6.0f}% | Комиссии: ${r['fees']:>5.2f}) | "
            f"🎯 Винрейт: {r['winrate']:>5.1f}% | Сделок: {r['trades']:>4} | "
            f"TP:+{r['tp']*100:.0f}%/SL:-{r['sl']*100:.0f}% | Тело>={r['body']:.2f}% | Тень<={r['wick']:.2f}% | ПлюсБУ:{str(r['lock']):<5}",
            flush=True
        )
    print("="*118, flush=True)
    print("⏸ Расчет завершен. Ожидание (чтобы контейнер не перезапускался)...", flush=True)
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(run_bybit_backtest())
