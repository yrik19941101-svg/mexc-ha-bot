import asyncio
import ccxt.async_support as ccxt

# ==================== НАСТРОЙКИ СТРАТЕГИИ ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 60           # Глубина истории (60 дней)
FIXED_TRADE_AMOUNT = 1.0        # Фиксированная маржа ($1)
SL_PERCENT_OF_MARGIN = 0.90     # Стоп-лосс: -90% от маржи
TP_PERCENT_OF_MARGIN = 1.50     # Тейк-профит: +150% от маржи
ASSUMED_LEVERAGE = 20           # Плечо

EMA_PERIOD = 50                 # Трендовый фильтр
MIN_BODY_PCT = 0.002            # Минимальный размер тела свечи (защита от флета)


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
            "real_close": c, "body_pct": body_pct, "color": color
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


async def run_backtest():
    print("🚀 Запуск оптимизированного бэктеста (1H + Откат + EMA 50 + Фильтр флета)...", flush=True)
    exchange = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    
    markets = await exchange.load_markets(True)
    active_swaps = [sym for sym, m in markets.items() if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)]

    print(f"📊 Найдено пар на BingX: {len(active_swaps)}. Скачиваем историю за {DAYS_TO_BACKTEST} дней...", flush=True)
    since_timestamp = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)

    total_signals = 0
    wins = 0
    losses = 0
    total_pnl_usd = 0.0

    for sym in active_swaps:
        try:
            ohlcv_1h = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_timestamp, limit=1500)
            if not ohlcv_1h or len(ohlcv_1h) < 100: 
                continue

            ha_candles = calculate_heikin_ashi(ohlcv_1h)
            closes = [c["real_close"] for c in ha_candles]
            ema_values = calculate_ema(closes, EMA_PERIOD)

            for idx in range(EMA_PERIOD, len(ha_candles) - 24):
                prev_c = ha_candles[idx - 1]
                curr_c = ha_candles[idx]
                
                # Фильтр флета по размеру тела
                if curr_c["body_pct"] < MIN_BODY_PCT:
                    continue

                target_dir = None
                current_price = curr_c["real_close"]
                ema_val = ema_values[idx]

                # ЛОГИКА ОТКАТА С ТРЕНДОВЫМ ФИЛЬТРОМ:
                # 1. Если предыдущая RED, а текущая стала GREEN -> Ждем отката вверх, входим в SHORT (если ниже EMA)
                if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                    if current_price < ema_val:
                        target_dir = "SHORT"

                # 2. Если предыдущая GREEN, а текущая стала RED -> Ждем отката вниз, входим в LONG (если выше EMA)
                elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                    if current_price > ema_val:
                        target_dir = "LONG"

                if not target_dir:
                    continue

                total_signals += 1
                entry_p = current_price
                
                pos_size = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE
                sl_delta = (FIXED_TRADE_AMOUNT * SL_PERCENT_OF_MARGIN / pos_size) * entry_p
                tp_delta = (FIXED_TRADE_AMOUNT * TP_PERCENT_OF_MARGIN / pos_size) * entry_p

                if target_dir == "LONG":
                    sl_price = entry_p - sl_delta
                    tp_price = entry_p + tp_delta
                else:
                    sl_price = entry_p + sl_delta
                    tp_price = entry_p - tp_delta

                # Проверка отработки в течение следующих 24 свечей
                trade_result = None
                for future_c in ha_candles[idx + 1 : idx + 25]:
                    f_high = future_c["real_high"]
                    f_low = future_c["real_low"]

                    if target_dir == "LONG":
                        hit_tp = f_high >= tp_price
                        hit_sl = f_low <= sl_price
                        if hit_tp and hit_sl:
                            trade_result = "LOSS"
                            break
                        elif hit_tp:
                            trade_result = "WIN"
                            break
                        elif hit_sl:
                            trade_result = "LOSS"
                            break
                    else:
                        hit_tp = f_low <= tp_price
                        hit_sl = f_high >= sl_price
                        if hit_tp and hit_sl:
                            trade_result = "LOSS"
                            break
                        elif hit_tp:
                            trade_result = "WIN"
                            break
                        elif hit_sl:
                            trade_result = "LOSS"
                            break

                if trade_result == "WIN":
                    wins += 1
                    total_pnl_usd += FIXED_TRADE_AMOUNT * TP_PERCENT_OF_MARGIN
                elif trade_result == "LOSS":
                    losses += 1
                    total_pnl_usd -= FIXED_TRADE_AMOUNT * SL_PERCENT_OF_MARGIN

        except Exception as e:
            pass

    await exchange.close()

    print("\n" + "="*50, flush=True)
    print("📈 РЕЗУЛЬТАТЫ ОПТИМИЗИРОВАННОГО БЭКТЕСТА:", flush=True)
    print(f"Всего сделок: {total_signals}", flush=True)
    print(f"✅ Успешных (TP +150%): {wins}", flush=True)
    print(f"❌ Убыточных (SL -90%): {losses}", flush=True)
    marked = wins + losses
    winrate = (wins / marked * 100.0) if marked > 0 else 0.0
    print(f"🎯 Итоговый Винрейт: {winrate:.1f}%", flush=True)
    print(f"💰 Итоговый PnL: ${total_pnl_usd:.2f}", flush=True)
    print("="*50, flush=True)

if __name__ == "__main__":
    asyncio.run(run_backtest())
