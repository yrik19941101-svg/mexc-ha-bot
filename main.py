import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== ЧЕСТНЫЙ ОПТИМИЗАТОР v3.0 (СНАЙПЕР) ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 45
FIXED_TRADE_AMOUNT = 10.0       # Маржа $10
ASSUMED_LEVERAGE = 15           # Плечо 15x
BINGX_FEE_RATE = 0.001          # Комиссия BingX 0.10% за круг
MAX_HOLD_HOURS = 48             # Таймаут удержания сделки

# Сетка параметров вокруг найденной прибыльной зоны:
TP_GRID = [0.8, 1.2, 1.5]               # Тейк-профит (+80%, +120%, +150% от маржи)
SL_GRID = [0.6, 0.8]                    # Стоп-лосс (-60%, -80% от маржи)
REAL_MIN_BODY_PCT = [0.50, 0.75, 1.00]  # Усиленный фильтр тела свечи (в % цены)
WICK_FILTER_GRID = [False, True]        # Требовать отсутствие/минимальную обратную тень Heikin Ashi
PROFIT_LOCK_GRID = [False, True]        # Перенос стопа в +10% (плюсовой БУ) при достижении +50%


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

        # Расчет обратных теней Heikin Ashi (в % от цены открытия)
        upper_wick_pct = (ha_high - max(ha_open, ha_close)) / ha_open * 100.0 if ha_open > 0 else 0.0
        lower_wick_pct = (min(ha_open, ha_close) - ha_low) / ha_open * 100.0 if ha_open > 0 else 0.0
        
        ha_candles.append({
            "timestamp": ts, "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low,
            "ha_close": ha_close, "real_open": o, "real_high": h, "real_low": l,
            "real_close": c, "body_pct": body_pct, "color": color,
            "upper_wick_pct": upper_wick_pct, "lower_wick_pct": lower_wick_pct
        })
    return ha_candles


def calculate_ema(closes, period):
    if len(closes) < period:
        return [sum(closes) / len(closes)] * len(closes)
    ema = [sum(closes[:period]) / period]
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return [ema[0]] * (period - 1) + ema


async def run_sniper_optimizer():
    print("🚀 Запуск ЧЕСТНОГО оптимизатора v3.0 (Фильтр импульса + Тени HA + Защита прибыли)...", flush=True)
    exchange = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    
    markets = await exchange.load_markets(True)
    active_swaps = [sym for sym, m in markets.items() if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)]
    active_swaps = active_swaps[:40]

    print(f"📊 Скачиваем историю для {len(active_swaps)} пар за {DAYS_TO_BACKTEST} дней...", flush=True)
    since_timestamp = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)

    cached_market_data = {}
    for sym in active_swaps:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_timestamp, limit=1200)
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
    print(f"💾 Закешировано пар: {len(cached_market_data)}. Перебираем комбинации...", flush=True)

    if not cached_market_data:
        print("❌ Нет данных для анализа.", flush=True)
        return

    combinations = list(product(TP_GRID, SL_GRID, REAL_MIN_BODY_PCT, WICK_FILTER_GRID, PROFIT_LOCK_GRID))
    results = []

    pos_size_usd = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE
    fee_per_trade_usd = pos_size_usd * BINGX_FEE_RATE

    for tp_pct, sl_pct, min_body_pct, no_wick, use_lock in combinations:
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

                # Фильтр размера тела свечи
                if curr_c["body_pct"] < min_body_pct:
                    continue

                current_price = curr_c["real_close"]
                ema_now = ema_values[idx]
                ema_prev = ema_values[idx - 3]

                target_dir = None

                # Вход по продолжению тренда (CONTINUATION)
                if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                    if current_price > ema_now and ema_now > ema_prev:
                        # Если включен фильтр теней: у зеленой свечи нижняя тень должна быть почти нулевой (<= 0.08%)
                        if not no_wick or curr_c["lower_wick_pct"] <= 0.08:
                            target_dir = "LONG"
                elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                    if current_price < ema_now and ema_now < ema_prev:
                        # У красной свечи верхняя тень должна быть почти нулевой (<= 0.08%)
                        if not no_wick or curr_c["upper_wick_pct"] <= 0.08:
                            target_dir = "SHORT"

                if not target_dir:
                    continue

                entry_p = current_price
                sl_delta = (FIXED_TRADE_AMOUNT * sl_pct / pos_size_usd) * entry_p
                tp_delta = (FIXED_TRADE_AMOUNT * tp_pct / pos_size_usd) * entry_p
                
                # Параметры защиты прибыли: при достижении +50% маржи переносим стоп в +10% маржи
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

                    # Честная проверка внутри свечи: при одновременном касании считаем худший исход (SL)
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

                    # Активируем защиту прибыли ТОЛЬКО для следующих свечей (без заглядывания внутрь текущего бара)
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
        results.append({
            "tp": tp_pct, "sl": sl_pct, "body": min_body_pct,
            "no_wick": no_wick, "lock": use_lock,
            "trades": total_trades, "winrate": winrate,
            "pnl": total_pnl, "fees": total_fees
        })

    results.sort(key=lambda x: x["pnl"], reverse=True)

    print("\n" + "="*108, flush=True)
    print(f"🏆 ТОП-10 КОНФИГУРАЦИЙ ЧЕСТНОГО ТЕСТА v3.0 (Маржа ${FIXED_TRADE_AMOUNT}, Плечо {ASSUMED_LEVERAGE}x, Все комиссии учтены):", flush=True)
    print("="*108, flush=True)
    for i, r in enumerate(results[:10], 1):
        print(
            f"#{i:<2} | Чистый PnL: ${r['pnl']:>7.2f} (Комиссии: ${r['fees']:>4.0f}) | "
            f"🎯 Винрейт: {r['winrate']:>5.1f}% | Сделок: {r['trades']:>4} | "
            f"TP:+{r['tp']*100:.0f}%/SL:-{r['sl']*100:.0f}% | Тело>{r['body']:.2f}% | "
            f"БезТени:{str(r['no_wick']):<5} | ПлюсБУ:{str(r['lock']):<5}",
            flush=True
        )
    print("="*108, flush=True)
    print("⏸ Расчет завершен. Переход в режим ожидания (чтобы сервер не перезапускал контейнер)...", flush=True)
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(run_sniper_optimizer())
