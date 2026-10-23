import asyncio
import ccxt.async_support as ccxt
from itertools import product

# ==================== НАСТРОЙКИ ПОИСКА 70%+ ВИНРЕЙТА ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 45
FIXED_TRADE_AMOUNT = 10.0       # Рабочая маржа $10 на сделку
ASSUMED_LEVERAGE = 20           # Плечо 20x

# Сетка параметров для достижения высокого винрейта:
TP_GRID = [0.7, 0.9, 1.1]           # Тейк-профит (+70%, +90%, +110% от маржи)
SL_GRID = [1.0, 1.2]                # Стоп-лосс (-100%, -120% от маржи)
MIN_SERIES_GRID = [1, 2]            # Сколько свечей одного цвета должно быть перед откатом
EMA_FAN_FILTER = [True, False]      # Требовать выстраивание Цена -> EMA 20 -> EMA 50
USE_BREAKEVEN = [True, False]       # Перевод в безубыток при прохождении +45% профита


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


async def run_high_winrate_optimizer():
    print("🚀 Запуск оптимизатора под высокий винрейт (70%+)...", flush=True)
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
                ema20 = calculate_ema(closes, 20)
                ema50 = calculate_ema(closes, 50)
                
                cached_market_data[sym] = {
                    "ha_candles": ha_candles,
                    "ema20": ema20,
                    "ema50": ema50
                }
        except Exception as e:
            print(f"⚠️ Ошибка загрузки {sym}: {e}", flush=True)

    await exchange.close()
    print(f"💾 Закешировано пар: {len(cached_market_data)}. Перебираем комбинации...", flush=True)

    if len(cached_market_data) == 0:
        print("❌ Нет данных для анализа.", flush=True)
        return

    combinations = list(product(TP_GRID, SL_GRID, MIN_SERIES_GRID, EMA_FAN_FILTER, USE_BREAKEVEN))
    print(f"🔬 Всего комбинаций для проверки: {len(combinations)}\n", flush=True)

    best_results = []

    for tp_pct, sl_pct, min_series, use_fan, use_be in combinations:
        wins = 0
        losses = 0
        breakevens = 0
        total_pnl = 0.0

        for sym, data in cached_market_data.items():
            ha_candles = data["ha_candles"]
            ema20 = data["ema20"]
            ema50 = data["ema50"]

            for idx in range(55, len(ha_candles) - 24):
                curr_c = ha_candles[idx]
                if curr_c["body_pct"] < 0.001:
                    continue

                # Проверяем серию предыдущих свечей одного цвета перед откатом
                prev_colors = [ha_candles[idx - k]["color"] for k in range(1, min_series + 1)]
                
                target_dir = None
                current_price = curr_c["real_close"]
                e20 = ema20[idx]
                e50 = ema50[idx]

                # Условие для SHORT: серия красных свечей + текущая откатила и стала зеленой
                if all(c == "RED" for c in prev_colors) and curr_c["color"] == "GREEN":
                    if use_fan:
                        if current_price < e20 and e20 < e50:
                            target_dir = "SHORT"
                    else:
                        if current_price < e20:
                            target_dir = "SHORT"

                # Условие для LONG: серия зеленых свечей + текущая откатила и стала красной
                elif all(c == "GREEN" for c in prev_colors) and curr_c["color"] == "RED":
                    if use_fan:
                        if current_price > e20 and e20 > e50:
                            target_dir = "LONG"
                    else:
                        if current_price > e20:
                            target_dir = "LONG"

                if not target_dir:
                    continue

                entry_p = current_price
                pos_size = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE
                sl_delta = (FIXED_TRADE_AMOUNT * sl_pct / pos_size) * entry_p
                tp_delta = (FIXED_TRADE_AMOUNT * tp_pct / pos_size) * entry_p
                be_delta = (FIXED_TRADE_AMOUNT * 0.45 / pos_size) * entry_p  # Триггер БУ (+45% от маржи)

                if target_dir == "LONG":
                    sl_price = entry_p - sl_delta
                    tp_price = entry_p + tp_delta
                    be_trigger_price = entry_p + be_delta
                else:
                    sl_price = entry_p + sl_delta
                    tp_price = entry_p - tp_delta
                    be_trigger_price = entry_p - be_delta

                trade_result = None
                be_activated = False

                for future_c in ha_candles[idx + 1 : idx + 25]:
                    f_high = future_c["real_high"]
                    f_low = future_c["real_low"]

                    if target_dir == "LONG":
                        hit_tp = f_high >= tp_price
                        hit_sl = f_low <= sl_price
                        if hit_tp and hit_sl:
                            trade_result = "BE" if be_activated else "LOSS"
                            break
                        elif hit_tp:
                            trade_result = "WIN"
                            break
                        elif hit_sl:
                            trade_result = "BE" if be_activated else "LOSS"
                            break
                        # Активация безубытка для следующих свечей
                        if use_be and not be_activated and f_high >= be_trigger_price:
                            be_activated = True
                            sl_price = entry_p
                    else:
                        hit_tp = f_low <= tp_price
                        hit_sl = f_high >= sl_price
                        if hit_tp and hit_sl:
                            trade_result = "BE" if be_activated else "LOSS"
                            break
                        elif hit_tp:
                            trade_result = "WIN"
                            break
                        elif hit_sl:
                            trade_result = "BE" if be_activated else "LOSS"
                            break
                        if use_be and not be_activated and f_low <= be_trigger_price:
                            be_activated = True
                            sl_price = entry_p

                if trade_result == "WIN":
                    wins += 1
                    total_pnl += FIXED_TRADE_AMOUNT * tp_pct
                elif trade_result == "LOSS":
                    losses += 1
                    total_pnl -= FIXED_TRADE_AMOUNT * sl_pct
                elif trade_result == "BE":
                    breakevens += 1

        decided_trades = wins + losses
        winrate = (wins / decided_trades * 100.0) if decided_trades > 0 else 0.0

        if decided_trades >= 30 and total_pnl > 0:
            best_results.append({
                "tp": tp_pct, "sl": sl_pct, "series": min_series,
                "fan": use_fan, "be": use_be,
                "wins": wins, "losses": losses, "be_count": breakevens,
                "winrate": winrate, "pnl": total_pnl
            })

    # Сортируем по винрейту среди прибыльных комбинаций
    best_results.sort(key=lambda x: (x["winrate"], x["pnl"]), reverse=True)

    print("\n" + "="*80, flush=True)
    print("🏆 ТОП-7 КОНФИГУРАЦИЙ С МАКСИМАЛЬНЫМ ВИНРЕЙТОМ (Маржа $10 на сделку):", flush=True)
    print("="*80, flush=True)
    for i, res in enumerate(best_results[:7], 1):
        print(
            f"#{i} | 🎯 Винрейт: {res['winrate']:.1f}% (+{res['wins']} / -{res['losses']} / БУ:{res['be_count']}) | "
            f"💰 PnL: ${res['pnl']:.2f} | TP: +{res['tp']*100:.0f}% | SL: -{res['sl']*100:.0f}% | "
            f"Серия: {res['series']} | Веер EMA: {res['fan']} | БУ: {res['be']}",
            flush=True
        )
    print("="*80, flush=True)

if __name__ == "__main__":
    asyncio.run(run_high_winrate_optimizer())
