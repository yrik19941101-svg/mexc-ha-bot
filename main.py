import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== ЧЕСТНЫЙ ПОИСК РЕАЛЬНОГО ПРЕИМУЩЕСТВА ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 45
FIXED_TRADE_AMOUNT = 10.0       # Маржа $10
ASSUMED_LEVERAGE = 15           # Безопасное плечо 15x
BINGX_FEE_RATE = 0.001          # Комиссия BingX 0.10% (вход + выход)
MAX_HOLD_HOURS = 48             # Таймаут удержания сделки

# Сетка параметров (исправлен масштаб фильтра флета!)
ENTRY_MODE_GRID = ["CONTINUATION", "PULLBACK"]  # Продолжение после отката vs Вход на самом откате
TP_GRID = [0.8, 1.2, 1.6]                       # Тейк-профит (+80%, +120%, +160% от маржи)
SL_GRID = [0.6, 0.8]                            # Стоп-лосс (-60%, -80% от маржи)
REAL_MIN_BODY_PCT = [0.25, 0.50]                # РЕАЛЬНЫЙ размер тела свечи в % (0.25% и 0.50% цены)
EMA_PERIOD_GRID = [20, 50]


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
        
        # Размер тела в реальных ПРОЦЕНТАХ (например, 0.45 означает 0.45%)
        body_pct = abs(ha_close - ha_open) / ha_open * 100.0 if ha_open > 0 else 0.0
        color = "GREEN" if ha_close > ha_open else "RED"
        
        ha_candles.append({
            "timestamp": ts, "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low,
            "ha_close": ha_close, "real_open": o, "real_high": h, "real_low": l,
            "real_close": c, "body_pct": body_pct, "color": color, "volume": v
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


async def run_true_edge_finder():
    print("🚀 Запуск ЧЕСТНОГО оптимизатора v2.0 (исправлен фильтр флета + тест двух логик входа)...", flush=True)
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
                    "ema20": calculate_ema(closes, 20),
                    "ema50": calculate_ema(closes, 50)
                }
        except Exception as e:
            print(f"⚠️ Ошибка загрузки {sym}: {e}", flush=True)

    await exchange.close()
    print(f"💾 Закешировано пар: {len(cached_market_data)}. Перебираем комбинации...", flush=True)

    if not cached_market_data:
        print("❌ Нет данных для анализа.", flush=True)
        return

    combinations = list(product(ENTRY_MODE_GRID, TP_GRID, SL_GRID, REAL_MIN_BODY_PCT, EMA_PERIOD_GRID))
    results = []

    pos_size_usd = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE
    fee_per_trade_usd = pos_size_usd * BINGX_FEE_RATE

    for mode, tp_pct, sl_pct, min_body_pct, ema_p in combinations:
        wins = 0
        losses = 0
        total_pnl = 0.0
        total_fees = 0.0

        for sym, data in cached_market_data.items():
            ha_candles = data["ha_candles"]
            ema_values = data["ema20"] if ema_p == 20 else data["ema50"]

            busy_until_idx = -1

            for idx in range(55, len(ha_candles) - MAX_HOLD_HOURS):
                if idx <= busy_until_idx:
                    continue

                prev_c = ha_candles[idx - 1]
                curr_c = ha_candles[idx]

                # НАСТОЯЩИЙ фильтр флета: отсекаем мелкие свечи меньше 0.25% / 0.50%
                if curr_c["body_pct"] < min_body_pct:
                    continue

                current_price = curr_c["real_close"]
                ema_now = ema_values[idx]
                ema_prev = ema_values[idx - 3]  # Проверяем наклон EMA за 3 часа, чтобы не входить в боковике

                target_dir = None

                if mode == "CONTINUATION":
                    # Вход ПОСЛЕ завершения отката в сторону тренда и наклона EMA
                    if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                        if current_price > ema_now and ema_now > ema_prev:
                            target_dir = "LONG"
                    elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                        if current_price < ema_now and ema_now < ema_prev:
                            target_dir = "SHORT"
                else:
                    # Вход НА САМОМ откате против локальной свечи, но по глобальной EMA
                    if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                        if current_price < ema_now and ema_now < ema_prev:
                            target_dir = "SHORT"
                    elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                        if current_price > ema_now and ema_now > ema_prev:
                            target_dir = "LONG"

                if not target_dir:
                    continue

                entry_p = current_price
                sl_delta = (FIXED_TRADE_AMOUNT * sl_pct / pos_size_usd) * entry_p
                tp_delta = (FIXED_TRADE_AMOUNT * tp_pct / pos_size_usd) * entry_p

                if target_dir == "LONG":
                    sl_price = entry_p - sl_delta
                    tp_price = entry_p + tp_delta
                else:
                    sl_price = entry_p + sl_delta
                    tp_price = entry_p - tp_delta

                trade_pnl_gross = None
                exit_idx = idx + MAX_HOLD_HOURS

                for offset in range(1, MAX_HOLD_HOURS + 1):
                    future_c = ha_candles[idx + offset]
                    f_high = future_c["real_high"]
                    f_low = future_c["real_low"]

                    if target_dir == "LONG":
                        hit_tp = f_high >= tp_price
                        hit_sl = f_low <= sl_price
                    else:
                        hit_tp = f_low <= tp_price
                        hit_sl = f_high >= sl_price

                    if hit_tp and hit_sl:
                        trade_pnl_gross = -FIXED_TRADE_AMOUNT * sl_pct
                        exit_idx = idx + offset
                        break
                    elif hit_tp:
                        trade_pnl_gross = FIXED_TRADE_AMOUNT * tp_pct
                        exit_idx = idx + offset
                        break
                    elif hit_sl:
                        trade_pnl_gross = -FIXED_TRADE_AMOUNT * sl_pct
                        exit_idx = idx + offset
                        break

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
        if total_trades < 20:
            continue

        winrate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
        random_baseline_wr = (sl_pct / (tp_pct + sl_pct)) * 100.0
        edge_over_random = winrate - random_baseline_wr

        results.append({
            "mode": mode, "tp": tp_pct, "sl": sl_pct,
            "body": min_body_pct, "ema": ema_p,
            "trades": total_trades, "winrate": winrate, "edge": edge_over_random,
            "pnl": total_pnl, "fees": total_fees
        })

    results.sort(key=lambda x: x["pnl"], reverse=True)

    print("\n" + "="*105, flush=True)
    print(f"📊 РЕЗУЛЬТАТЫ ЧЕСТНОГО ТЕСТА v2.0 (С работающим фильтром флета и учетом всех комиссий):", flush=True)
    print("="*105, flush=True)
    for i, r in enumerate(results[:10], 1):
        print(
            f"#{i:<2} | Чистый PnL: ${r['pnl']:>7.2f} (Комиссии: ${r['fees']:>4.0f}) | "
            f"Винрейт: {r['winrate']:.1f}% (Перевес: {r['edge']:+.1f}%) | "
            f"Сделок: {r['trades']:>4} | Режим: {r['mode']:<12} | "
            f"TP:+{r['tp']*100:.0f}%/SL:-{r['sl']*100:.0f}% | Тело>{r['body']}% | EMA:{r['ema']}",
            flush=True
        )
    print("="*105, flush=True)

if __name__ == "__main__":
    asyncio.run(run_true_edge_finder())
