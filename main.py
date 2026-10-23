import asyncio
import ccxt.async_support as ccxt

# ==================== НАСТРОЙКИ БЭКТЕСТА ====================
DAYS_TO_BACKTEST = 45           # Глубина истории (45 дней)
FIXED_TRADE_AMOUNT = 1.0        # Фиксированная маржа ($1)
SL_PERCENT_OF_MARGIN = 0.90     # Стоп-лосс: -90%
TP_PERCENT_OF_MARGIN = 1.50     # Тейк-профит: +150%
ASSUMED_LEVERAGE = 20           # Плечо

# Фильтры тренда и флета
MIN_BODY_PCT = 0.003            # Минимальный размер тела свечи (фильтр флета/доджи)
EMA_PERIOD = 50                 # Период EMA для фильтрации тренда на 4H


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
    # Дополняем начало до длины closes
    return [ema[0]] * (period - 1) + ema


def aggregate_candles(candles_30m, target_minutes):
    """Агрегация 30м свечей в старший таймфрейм (например, 4H = 8 свечей по 30м)"""
    factor = target_minutes // 30
    buckets = {}
    for c in candles_30m:
        ts, o, h, l, cl, v = c
        target_ts = (ts // (target_minutes * 60 * 1000)) * (target_minutes * 60 * 1000)
        if target_ts not in buckets:
            buckets[target_ts] = [target_ts, o, h, l, cl, v]
        else:
            b = buckets[target_ts]
            if h > b[2]: b[2] = h
            if l < b[3]: b[3] = l
            b[4] = cl
            b[5] += v
    return [buckets[k] for k in sorted(buckets.keys())]


async def run_backtest():
    print("🚀 Запуск бэктеста двухуровневой стратегии (4H -> 30m) с фильтрами тренда и флета...", flush=True)
    exchange = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    
    markets = await exchange.load_markets(True)
    active_swaps = [sym for sym, m in markets.items() if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)]

    print(f"📊 Найдено пар на BingX: {len(active_swaps)}. Скачиваем 30m историю за {DAYS_TO_BACKTEST} дней...", flush=True)
    since_timestamp = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)

    total_signals = 0
    wins = 0
    losses = 0
    total_pnl_usd = 0.0

    for i, sym in enumerate(active_swaps[:50]): # Ограничим для теста первыми 50 парами для скорости
        try:
            ohlcv_30m = await exchange.fetch_ohlcv(sym, timeframe="30m", since=since_timestamp, limit=1500)
            if not ohlcv_30m or len(ohlcv_30m) < 200: 
                continue

            ohlcv_4h = aggregate_candles(ohlcv_30m, 240)
            if len(ohlcv_4h) < EMA_PERIOD + 10: 
                continue

            ha_4h_all = calculate_heikin_ashi(ohlcv_4h)
            closes_4h = [c["real_close"] for c in ha_4h_all]
            ema_4h = calculate_ema(closes_4h, EMA_PERIOD)

            # Идем по 30м свечам и сопоставляем с 4H контекстом
            for idx_30m in range(50, len(ohlcv_30m) - 48):
                c_30m = ohlcv_30m[idx_30m]
                t_30m = c_30m[0]

                # Находим соответствующий 4H бар
                current_4h_ts = (t_30m // (240 * 60 * 1000)) * (240 * 60 * 1000)
                h4_idx = -1
                for j, h4_c in enumerate(ohlcv_4h):
                    if h4_c[0] == current_4h_ts:
                        h4_idx = j
                        break
                
                if h4_idx < 5 or h4_idx >= len(ha_4h_all) - 1:
                    continue

                # Проверяем состояние 4H (предыдущая закрытая + текущая сменила цвет)
                prev_4h = ha_4h_all[h4_idx - 1]
                curr_4h = ha_4h_all[h4_idx]
                
                # Фильтр флета по размеру тела на 4H
                if curr_4h["body_pct"] < MIN_BODY_PCT:
                    continue

                h4_trend = None
                # Если предыдущая красная, текущая стала зеленой -> 4H бычий контекст (LONG)
                if prev_4h["color"] == "RED" and curr_4h["color"] == "GREEN":
                    if curr_4h["real_close"] > ema_4h[h4_idx]: # Фильтр тренда по EMA
                        h4_trend = "LONG"
                # Если предыдущая зеленая, текущая стала красной -> 4H медвежий контекст (SHORT)
                elif prev_4h["color"] == "GREEN" and curr_4h["color"] == "RED":
                    if curr_4h["real_close"] < ema_4h[h4_idx]: # Фильтр тренда по EMA
                        h4_trend = "SHORT"

                if not h4_trend:
                    continue

                # Теперь проверяем 30m триггер в рамках этого 4H контекста
                ha_30m_slice = calculate_heikin_ashi(ohlcv_30m[:idx_30m + 1])
                if len(ha_30m_slice) < 3:
                    continue

                prev_30 = ha_30m_slice[-2]
                curr_30 = ha_30m_slice[-1]

                # Фильтр флета по размеру тела на 30m
                if curr_30["body_pct"] < MIN_BODY_PCT:
                    continue

                target_dir = None
                # Если 4H LONG и на 30m произошло подтверждение (предыдущая красная, текущая стала зеленой)
                if h4_trend == "LONG" and prev_30["color"] == "RED" and curr_30["color"] == "GREEN":
                    target_dir = "LONG"
                # Если 4H SHORT и на 30m произошло подтверждение (предыдущая зеленая, текущая стала красной)
                elif h4_trend == "SHORT" and prev_30["color"] == "GREEN" and curr_30["color"] == "RED":
                    target_dir = "SHORT"

                if not target_dir:
                    continue

                total_signals += 1
                entry_p = curr_30["real_close"]
                
                pos_size = FIXED_TRADE_AMOUNT * ASSUMED_LEVERAGE
                sl_delta = (FIXED_TRADE_AMOUNT * SL_PERCENT_OF_MARGIN / pos_size) * entry_p
                tp_delta = (FIXED_TRADE_AMOUNT * TP_PERCENT_OF_MARGIN / pos_size) * entry_p

                if target_dir == "LONG":
                    sl_price = entry_p - sl_delta
                    tp_price = entry_p + tp_delta
                else:
                    sl_price = entry_p + sl_delta
                    tp_price = entry_p - tp_delta

                # Проверка отработки сделки по будущим 30м свечам (проверяем в течение 24 часов / 48 баров)
                trade_result = None
                for future_c in ohlcv_30m[idx_30m + 1 : idx_30m + 48]:
                    f_high = future_c[2]
                    f_low = future_c[3]

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
    print("📈 РЕЗУЛЬТАТЫ БЭКТЕСТА (4H -> 30m + Тренд EMA + Фильтр флета):", flush=True)
    print(f"Всего отфильтрованных сигналов: {total_signals}", flush=True)
    print(f"✅ Успешных (TP +150%): {wins}", flush=True)
    print(f"❌ Убыточных (SL -90%): {losses}", flush=True)
    marked = wins + losses
    winrate = (wins / marked * 100.0) if marked > 0 else 0.0
    print(f"🎯 Итоговый Винрейт: {winrate:.1f}%", flush=True)
    print(f"💰 Итоговый PnL: ${total_pnl_usd:.2f}", flush=True)
    print("="*50, flush=True)

if __name__ == "__main__":
    asyncio.run(run_backtest())
