import asyncio
import ccxt.async_support as ccxt

# ==================== НАСТРОЙКИ ОПТИМИЗИРОВАННОГО БЭКТЕСТА ====================
TIMEFRAME = "1h"
DAYS_TO_BACKTEST = 30           # 30 дней (720 часовых свечей)
INITIAL_BALANCE_USD = 50.00     # Стартовый депозит: $50.00
MARGIN_PCT_OF_BALANCE = 0.05    # Безопасные 5% от депозита на сделку (гибридный сложный %)
MIN_MARGIN_USD = 0.50           # Минимальный порог маржи
MAX_MARGIN_USD = 100.0          # Защитный потолок маржи
ASSUMED_LEVERAGE = 15           # Плечо 15x
BYBIT_FEE_RATE = 0.0011         # Комиссия Bybit Futures: 0.11% за круг
MAX_HOLD_HOURS = 48             # Принудительное закрытие через 48 часов
PAIRS_LIMIT = 50                # Топ-50 пар Bybit для теста

# Новые фильтры отсечения флета:
MIN_BODY_PCT = 0.60             # Увеличенное тело Heikin Ashi >= 0.60% (только сильные импульсы)
MAX_WICK_PCT = 0.10             # Строгая обратная тень <= 0.10%
ATR_PERIOD = 14                 # Период расчета волатильности
ATR_MULTIPLIER = 1.2            # Текущая свеча должна быть больше ATR * 1.2 (фильтр всплеска активности)

PROFILES_TO_TEST = [
    {"id": 1, "tp": 0.80, "sl": 0.60, "use_lock": False, "name": "Оптимизированный Профиль #1 (TP +80% / без БУ)"},
    {"id": 2, "tp": 1.20, "sl": 0.60, "use_lock": True,  "name": "Оптимизированный Профиль #2 (TP +120% / с ПлюсБУ)"}
]
# ==============================================================================


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
        true_range = h - l

        ha_candles.append({
            "timestamp": ts,
            "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low, "ha_close": ha_close,
            "real_open": o, "real_high": h, "real_low": l, "real_close": c,
            "body_pct": body_pct, "color": color,
            "upper_wick_pct": upper_wick_pct, "lower_wick_pct": lower_wick_pct,
            "true_range": true_range
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


def calculate_atr(ha_candles, period):
    tr_list = [c["true_range"] for c in ha_candles]
    if len(tr_list) < period:
        return [sum(tr_list) / len(tr_list)] * len(tr_list)
    atr = [sum(tr_list[:period]) / period]
    for tr in tr_list[period:]:
        next_atr = (atr[-1] * (period - 1) + tr) / period
        atr.append(next_atr)
    return [atr[0]] * (period - 1) + atr


async def get_affordable_bybit_symbols(exchange, all_swaps):
    max_notional = MAX_MARGIN_USD * ASSUMED_LEVERAGE * 1.4
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


async def run_optimized_backtest():
    print(f"🚀 Запуск ОПТИМИЗИРОВАННОГО бэктеста с фильтром флета за {DAYS_TO_BACKTEST} дней (Старт: ${INITIAL_BALANCE_USD:.2f})...", flush=True)
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
    print(f"📊 Отобрано {len(active_swaps)} пар. Скачиваем историю за {DAYS_TO_BACKTEST} дней...", flush=True)

    since_timestamp = exchange.milliseconds() - (DAYS_TO_BACKTEST * 24 * 3600 * 1000)
    cached_market_data = {}

    for sym in active_swaps:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, since=since_timestamp, limit=1000)
            if ohlcv and len(ohlcv) > 100:
                ha_candles = calculate_heikin_ashi(ohlcv)
                closes = [c["real_close"] for c in ha_candles]
                ema20 = calculate_ema(closes, 20)
                atr14 = calculate_atr(ha_candles, ATR_PERIOD)
                cached_market_data[sym] = {
                    "ha_candles": ha_candles,
                    "ema20": ema20,
                    "atr14": atr14
                }
        except Exception:
            pass

    await exchange.close()
    print(f"💾 Закешировано пар: {len(cached_market_data)}. Запускаем симуляцию...\n", flush=True)

    if not cached_market_data:
        print("❌ Нет данных для анализа.", flush=True)
        return

    all_potential_trades = []

    for sym, data in cached_market_data.items():
        ha_candles = data["ha_candles"]
        ema_values = data["ema20"]
        atr_values = data["atr14"]

        for idx in range(30, len(ha_candles) - MAX_HOLD_HOURS):
            prev_c = ha_candles[idx - 1]
            curr_c = ha_candles[idx]

            if curr_c["body_pct"] < MIN_BODY_PCT:
                continue

            # Фильтр волатильности ATR: свеча должна быть активной
            if curr_c["true_range"] < (atr_values[idx] * ATR_MULTIPLIER):
                continue

            current_price = curr_c["real_close"]
            ema_now = ema_values[idx]
            ema_prev = ema_values[idx - 5]  жжесткий фильтр наклона EMA за 5 часов

            target_dir = None
            if prev_c["color"] == "RED" and curr_c["color"] == "GREEN":
                if current_price > ema_now and ema_now > ema_prev:
                    if curr_c["lower_wick_pct"] <= MAX_WICK_PCT:
                        target_dir = "LONG"
            elif prev_c["color"] == "GREEN" and curr_c["color"] == "RED":
                if current_price < ema_now and ema_now < ema_prev:
                    if curr_c["upper_wick_pct"] <= MAX_WICK_PCT:
                        target_dir = "SHORT"

            if target_dir:
                all_potential_trades.append({
                    "timestamp": curr_c["timestamp"],
                    "symbol": sym,
                    "direction": target_dir,
                    "idx": idx,
                    "data": data
                })

    all_potential_trades.sort(key=lambda x: x["timestamp"])

    print("="*95, flush=True)
    print(f"📈 РЕЗУЛЬТАТЫ ОПТИМИЗИРОВАННОЙ ТОРГОВЛИ (Фильтр флета ATR + Слот 5% от депо):", flush=True)
    print("="*95, flush=True)

    for profile in PROFILES_TO_TEST:
        sim_balance = INITIAL_BALANCE_USD
        wins = 0
        losses = 0
        total_fees = 0.0
        peak_balance = INITIAL_BALANCE_USD
        max_drawdown_pct = 0.0
        pair_busy_until = {}

        tp_pct = profile["tp"]
        sl_pct = profile["sl"]
        use_lock = profile["use_lock"]

        for signal in all_potential_trades:
            sym = signal["symbol"]
            idx = signal["idx"]

            if idx <= pair_busy_until.get(sym, -1):
                continue

            raw_margin = sim_balance * MARGIN_PCT_OF_BALANCE
            trade_margin = max(MIN_MARGIN_USD, min(raw_margin, MAX_MARGIN_USD))

            if sim_balance < trade_margin:
                continue

            data = signal["data"]
            ha_candles = data["ha_candles"]
            target_dir = signal["direction"]
            entry_p = ha_candles[idx]["real_close"]

            pos_size_usd = trade_margin * ASSUMED_LEVERAGE
            fee_usd = pos_size_usd * BYBIT_FEE_RATE
            total_fees += fee_usd

            sl_delta = (trade_margin * sl_pct / pos_size_usd) * entry_p
            tp_delta = (trade_margin * tp_pct / pos_size_usd) * entry_p
            lock_trigger_delta = (trade_margin * 0.50 / pos_size_usd) * entry_p
            lock_sl_delta = (trade_margin * 0.10 / pos_size_usd) * entry_p

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
            exit_offset = MAX_HOLD_HOURS
            lock_active = False

            for offset in range(1, MAX_HOLD_HOURS + 1):
                if idx + offset >= len(ha_candles):
                    break
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
                    trade_pnl_gross = trade_margin * current_sl_pct_val
                    exit_offset = offset
                    break
                elif hit_sl:
                    trade_pnl_gross = trade_margin * current_sl_pct_val
                    exit_offset = offset
                    break
                elif hit_tp:
                    trade_pnl_gross = trade_margin * tp_pct
                    exit_offset = offset
                    break

                if use_lock and not lock_active:
                    if target_dir == "LONG" and f_high >= lock_trigger_p:
                        lock_active = True
                        sl_price = locked_sl_p
                    elif target_dir == "SHORT" and f_low <= lock_trigger_p:
                        lock_active = True
                        sl_price = locked_sl_p

            if trade_pnl_gross is None:
                last_close = ha_candles[idx + MAX_HOLD_HOURS]["real_close"]
                if target_dir == "LONG":
                    price_change_pct = (last_close - entry_p) / entry_p
                else:
                    price_change_pct = (entry_p - last_close) / entry_p
                trade_pnl_gross = price_change_pct * pos_size_usd

            pair_busy_until[sym] = idx + exit_offset
            net_trade_pnl = trade_pnl_gross - fee_usd
            sim_balance += net_trade_pnl

            if sim_balance > peak_balance:
                peak_balance = sim_balance
            
            drawdown = (peak_balance - sim_balance) / peak_balance * 100.0
            if drawdown > max_drawdown_pct:
                max_drawdown_pct = drawdown

            if net_trade_pnl > 0:
                wins += 1
            else:
                losses += 1

        total_trades = wins + losses
        winrate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
        net_profit_usd = sim_balance - INITIAL_BALANCE_USD
        roi_pct = (net_profit_usd / INITIAL_BALANCE_USD) * 100.0

        print(
            f"🎯 {profile['name']}\n"
            f"   • Финальный баланс: <b>${sim_balance:.2f}</b> (Чистая прибыль: ${net_profit_usd:+.2f} | <b>{roi_pct:+.1f}%</b>)\n"
            f"   • Винрейт: {winrate:.1f}% (Побед: {wins} / Поражений: {losses} из {total_trades} сделок)\n"
            f"   • Максимальная просадка: <b>{max_drawdown_pct:.1f}%</b> | Уплачено комиссий: ${total_fees:.2f}\n"
            f"{'-'*95}",
            flush=True
        )

    print("="*95, flush=True)
    print("⏸ Оптимизированный бэктест завершен.", flush=True)
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(run_optimized_backtest())
