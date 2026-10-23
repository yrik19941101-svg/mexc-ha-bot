import asyncio
import datetime
import ccxt.async_support as ccxt

# ==================== НАСТРОЙКИ БЭКТЕСТА ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 60           # Глубина истории для теста (60 дней)
MIN_24H_VOLUME_USDT = 1_500_000

# Параметры риск-менеджмента для теста
SL_PERCENT_OF_MARGIN = 0.90     # -90% от маржи
TP_PERCENT_OF_MARGIN = 1.10     # +110% от маржи
ASSUMED_LEVERAGE = 20           # Плечо для расчета ценового расстояния до SL/TP

# Фильтры стратегии (как в боте)
MIN_BODY_PCT = 0.005
REV_MIN_STRENGTH_RATIO = 0.35
REV_MIN_ABS_BODY_PCT = 0.08
PULLBACK_RETRACE_RATIO = 0.45
MIN_PULLBACK_PCT = 0.50
DIRECT_PULLBACK_PCT = 0.85
MIN_BOUNCE_PCT = 0.18
MIN_BOUNCE_RATIO = 0.22
RSI_PERIOD = 14
RSI_SHORT_OVERBOUGHT = 68.0
RSI_LONG_OVERSOLD = 32.0


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


async def run_backtest():
    print("🚀 Инициализация бэктеста на истории BingX...", flush=True)
    exchange = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    
    markets = await exchange.load_markets(True)
    active_swaps = []
    for sym, m in markets.items():
        if m.get("swap") and m.get("quote") == "USDT" and m.get("active", True):
            active_swaps.append(sym)

    print(f"📊 Найдено активных пар на BingX: {len(active_swaps)}. Начинаем скачивание истории за {DAYS_TO_BACKTEST} дней...", flush=True)

    total_signals = 0
    wins = 0
    losses = 0

    since_timestamp = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)

    for i, sym in enumerate(active_swaps[:30]): # Тестируем на топ-30 парах для скорости
        try:
            ohlcv_1h = await exchange.fetch_ohlcv(sym, timeframe="1h", since=since_timestamp, limit=1500)
            if not ohlcv_1h or len(ohlcv_1h) < 100:
                continue

            print(f"[{i+1}/{min(30, len(active_swaps))}] Анализ истории пары {sym} (свечей: {len(ohlcv_1h)})", flush=True)

            # Проходим по истории свеча за свечой (симулируем каждый час)
            for idx in range(60, len(ohlcv_1h) - 25):
                historical_slice = ohlcv_1h[:idx]
                current_candle = ohlcv_1h[idx] # Живая свеча часа
                
                closed_1h = historical_slice[:-1]
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

                if not target_dir: continue

                # Проверка силы разворота
                streak_slice = ha_1h[idx_before_rev - streak_1h + 1 : idx_before_rev + 1]
                avg_streak_body = sum(c["body_pct"] for c in streak_slice) / len(streak_slice)
                if reversal_1h["body_pct"] < max(REV_MIN_ABS_BODY_PCT, avg_streak_body * REV_MIN_STRENGTH_RATIO):
                    continue

                # Проверка 6H
                ohlcv_6h = build_6h_ohlcv_from_1h(closed_1h)
                color_6h = get_htf_color(calculate_heikin_ashi(ohlcv_6h))
                if target_dir == "SHORT" and color_6h != "RED": continue
                if target_dir == "LONG" and color_6h != "GREEN": continue

                # Проверка живой свечи (момент входа)
                c_open = current_candle[1]
                c_high = current_candle[2]
                c_low = current_candle[3]
                c_close = current_candle[4]

                recent_ranges = [abs(c["real_high"] - c["real_low"]) for c in ha_1h[-5:]]
                rev_real_range = max(abs(reversal_1h["real_open"] - reversal_1h["real_close"]), sum(recent_ranges)/len(recent_ranges)*0.55)

                trigger_matched = False
                if target_dir == "SHORT":
                    max_pb = (c_high - c_open) / c_open * 100.0
                    retrace_rat = (c_high - c_open) / rev_real_range if rev_real_range > 0 else 0
                    if max_pb >= MIN_PULLBACK_PCT and (retrace_rat >= PULLBACK_RETRACE_RATIO or max_pb >= DIRECT_PULLBACK_PCT):
                        bounce = (c_high - c_close) / c_high * 100.0
                        if bounce >= MIN_BOUNCE_PCT: trigger_matched = True
                else:
                    max_pb = (c_open - c_low) / c_open * 100.0
                    retrace_rat = (c_open - c_low) / rev_real_range if rev_real_range > 0 else 0
                    if max_pb >= MIN_PULLBACK_PCT and (retrace_rat >= PULLBACK_RETRACE_RATIO or max_pb >= DIRECT_PULLBACK_PCT):
                        bounce = (c_close - c_low) / c_low * 100.0
                        if bounce >= MIN_BOUNCE_PCT: trigger_matched = True

                if trigger_matched:
                    total_signals += 1
                    entry_p = c_close
                    
                    # Расчет SL и TP цен
                    pos_size = 1.0 * ASSUMED_LEVERAGE
                    sl_delta = (1.0 * SL_PERCENT_OF_MARGIN / pos_size) * entry_p
                    tp_delta = (1.0 * TP_PERCENT_OF_MARGIN / pos_size) * entry_p

                    if target_dir == "LONG":
                        sl_price = entry_p - sl_delta
                        tp_price = entry_p + tp_delta
                    else:
                        sl_price = entry_p + sl_delta
                        tp_price = entry_p - tp_delta

                    # Проверяем будущие свечи на отработку TP или SL
                    trade_result = None
                    for future_c in ohlcv_1h[idx+1 : idx+24]: # Смотрим вперед до 24 часов
                    f_high = future_c[2]
                    f_low = future_c[3]

                        if target_dir == "LONG":
                            hit_tp = f_high >= tp_price
                            hit_sl = f_low <= sl_price
                            if hit_tp and hit_sl:
                                trade_result = "LOSS" # Консервативно считаем лосс при одновременном пробое
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
                    elif trade_result == "LOSS":
                        losses += 1

        except Exception as e:
            print(f"Ошибка по паре {sym}: {e}", flush=True)

    await exchange.close()

    print("\n" + "="*40, flush=True)
    print("📈 РЕЗУЛЬТАТЫ БЭКТЕСТА СТРАТЕГИИ:", flush=True)
    print(f"Всего найдено сделок: {total_signals}", flush=True)
    print(f"✅ Успешных (TP +110%): {wins}", flush=True)
    print(f"❌ Убыточных (SL -90%): {losses}", flush=True)
    marked = wins + losses
    winrate = (wins / marked * 100.0) if marked > 0 else 0.0
    print(f"🎯 Итоговый Винрейт: {winrate:.1f}%", flush=True)
    print("="*40, flush=True)

if __name__ == "__main__":
    asyncio.run(run_backtest())
