import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== НАСТРОЙКИ ЧЕСТНОГО КРАШ-ТЕСТА ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 45
FIXED_TRADE_AMOUNT = 10.0       # Маржа $10
ASSUMED_LEVERAGE = 15           # Безопасное плечо 15x (без риска ранней ликвидации)
BINGX_FEE_RATE = 0.001          # Комиссия BingX 0.10% за круг (0.05% вход + 0.05% выход)
MAX_HOLD_HOURS = 48             # Таймаут удержания сделки

# Сетка честных параметров
TP_GRID = [0.6, 0.8, 1.0, 1.2]      # Тейк-профит от маржи (+60% .. +120%)
SL_GRID = [0.6, 0.8]                # Безопасный Стоп-лосс (-60%, -80% от маржи)
EMA_GRID = [20, 50]                 # Фильтр тренда
MIN_SERIES_GRID = [1, 2]            # Серия свечей перед откатом


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


async def run_honest_backtest():
    print("🚀 Запуск ЧЕСТНОГО краш-теста (с комиссиями BingX, без наслоения и без пропавших сделок)...", flush=True)
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

    combinations = list(product(TP_GRID, SL_GRID, EMA_GRID, MIN_SERIES_GRID))
    results = []

    pos_size_usd = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE
    fee_per_trade_usd = pos_size_usd * BINGX_FEE_RATE  # Реальная комиссия за вход+выход

    for tp_pct, sl_pct, ema_p, min_series in combinations:
        wins = 0
        losses = 0
        total_pnl = 0.0
        total_fees = 0.0

        for sym, data in cached_market_data.items():
            ha_candles = data["ha_candles"]
            ema_values = data["ema20"] if ema_p == 20 else data["ema50"]

            busy_until_idx = -1  # Блокировка наслоения сделок на одной паре

            for idx in range(55, len(ha_candles) - MAX_HOLD_HOURS):
                if idx <= busy_until_idx:
                    continue

                curr_c = ha_candles[idx]
                if curr_c["body_pct"] < 0.002:
                    continue

                prev_colors = [ha_candles[idx - k]["color"] for k in range(1, min_series + 1)]
                current_price = curr_c["real_close"]
                ema_val = ema_values[idx]

                target_dir = None
                if all(c == "RED" for c in prev_colors) and curr_c["color"] == "GREEN":
                    if current_price < ema_val:
                        target_dir = "SHORT"
                elif all(c == "GREEN" for c in prev_colors) and curr_c["color"] == "RED":
                    if current_price > ema_val:
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
                        # В спорной свече считаем худший сценарий (SL)
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

                # Если за 48 часов не выбило ни TP, ни SL — закрываем по рынку (без утайки!)
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
        winrate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0

        # Доля случайного винрейта для сравнения (насколько стратегия лучше монетки)
        random_baseline_wr = (sl_pct / (tp_pct + sl_pct)) * 100.0
        edge_over_random = winrate - random_baseline_wr

        results.append({
            "tp": tp_pct, "sl": sl_pct, "ema": ema_p, "series": min_series,
            "trades": total_trades, "winrate": winrate, "edge": edge_over_random,
            "pnl": total_pnl, "fees": total_fees
        })

    results.sort(key=lambda x: x["pnl"], reverse=True)

    print("\n" + "="*95, flush=True)
    print(f"📊 РЕЗУЛЬТАТЫ ЧЕСТНОГО ТЕСТА (Маржа ${FIXED_TRADE_AMOUNT}, Плечо {ASSUMED_LEVERAGE}x, Комиссия учтена):", flush=True)
    print("="*95, flush=True)
    for i, r in enumerate(results[:8], 1):
        print(
            f"#{i} | Чистый PnL: ${r['pnl']:>7.2f} (Комиссии: ${r['fees']:.0f}) | "
            f"Винрейт: {r['winrate']:.1f}% (Преимущество над монеткой: {r['edge']:+.1f}%) | "
            f"Сделок: {r['trades']} | TP: +{r['tp']*100:.0f}% / SL: -{r['sl']*100:.0f}% | EMA:{r['ema']} | Серия:{r['series']}",
            flush=True
        )
    print("="*95, flush=True)

if __name__ == "__main__":
    asyncio.run(run_honest_backtest())
