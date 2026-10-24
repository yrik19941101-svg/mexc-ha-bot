import ccxt
import pandas as pd
import numpy as np
import time

SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'ADA/USDT',
    'AVAX/USDT', 'DOGE/USDT', 'LINK/USDT', 'DOT/USDT', 'NEAR/USDT',
    'APT/USDT', 'ARB/USDT', 'OP/USDT', 'SUI/USDT', 'INJ/USDT',
    'FIL/USDT', 'RENDER/USDT', 'ATOM/USDT', 'LTC/USDT', 'TIA/USDT'
]

def fetch_historical_data(exchange, symbol, timeframe='1h', days=60):
    """Загрузка данных за 60 дней (1440 часовых свечей)"""
    try:
        now = exchange.milliseconds()
        since = now - days * 24 * 60 * 60 * 1000
        all_ohlcv = []
        
        while since < now:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
            if not ohlcv:
                break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            if len(ohlcv) < 1000:
                break
                
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = df.drop_duplicates(subset=['timestamp']).reset_index(drop=True)
        return df
    except Exception as e:
        print(f"Ошибка загрузки {symbol}: {e}")
        return None

def prepare_indicators(df):
    """Расчет EMA, ATR и RSI(14)"""
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema100'] = df['close'].ewm(span=100, adjust=False).mean()
    
    # ATR (14)
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    
    # RSI (14)
    delta = df['close'].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    df['rsi'] = df['rsi'].fillna(50)
    
    df['body'] = (df['close'] - df['open']).abs()
    return df

def backtest_strategy(df, symbol):
    initial_capital = 150.0
    capital = initial_capital
    
    # Параметры Мартингейла v8.0: 8 шагов, коэффициент 1.5x, старт $0.50
    base_stake = 0.50
    max_steps = 8
    current_step = 1
    max_step_reached = 1
    rr_ratio = 1.25
    
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    ema20 = df['ema20'].values
    ema50 = df['ema50'].values
    ema100 = df['ema100'].values
    atrs = df['atr'].values
    rsis = df['rsi'].values
    bodies = df['body'].values
    
    demand_zones = []
    supply_zones = []
    
    in_position = False
    pos_type = None
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    current_stake = 0.0
    next_allowed_bar = 0
    
    trades = []
    
    for i in range(105, len(df)):
        if capital <= 0:
            break
            
        curr_open = opens[i]
        curr_high = highs[i]
        curr_low = lows[i]
        curr_close = closes[i]
        curr_atr = atrs[i]
        
        if np.isnan(curr_atr) or curr_atr == 0:
            continue

        # -------------------------------------------------------------------------
        # 1. РЕГИСТРАЦИЯ ЗОН СПРОСА И ПРЕДЛОЖЕНИЯ (С ПРОБОЕМ СТРУКТУРЫ BOS)
        # -------------------------------------------------------------------------
        p = i - 3
        if p >= 12:
            is_swing_low = (lows[p] <= np.min(lows[p-5:p])) and (lows[p] <= np.min(lows[p+1:i+1]))
            impulse_up = (np.max(highs[p+1:i+1]) - lows[p]) >= (atrs[p] * 1.25)
            bos_up = np.max(closes[p+1:i+1]) > np.max(highs[p-4:p])
            
            if is_swing_low and impulse_up and bos_up:
                demand_zones.append({
                    'bottom': lows[p],
                    'top': lows[p] + atrs[p] * 0.70,
                    'bar': p
                })
                
            is_swing_high = (highs[p] >= np.max(highs[p-5:p])) and (highs[p] >= np.max(highs[p+1:i+1]))
            impulse_down = (highs[p] - np.min(lows[p+1:i+1])) >= (atrs[p] * 1.25)
            bos_down = np.min(closes[p+1:i+1]) < np.min(lows[p-4:p])
            
            if is_swing_high and impulse_down and bos_down:
                supply_zones.append({
                    'bottom': highs[p] - atrs[p] * 0.70,
                    'top': highs[p],
                    'bar': p
                })

        demand_zones = [
            z for z in demand_zones 
            if (curr_close >= z['bottom']) and (i - z['bar'] <= 120)
        ]
        supply_zones = [
            z for z in supply_zones 
            if (curr_close <= z['top']) and (i - z['bar'] <= 120)
        ]

        # -------------------------------------------------------------------------
        # 2. ПРОВЕРКА ЗАКРЫТИЯ СДЕЛКИ И УПРАВЛЕНИЕ ШАГАМИ МАРТИНГЕЙЛА
        # -------------------------------------------------------------------------
        if in_position:
            if pos_type == 'LONG':
                if curr_low <= stop_loss:
                    capital -= current_stake
                    trades.append('LOSS')
                    in_position = False
                    current_step += 1
                    if current_step > max_steps:
                        current_step = 1
                    next_allowed_bar = i + 8
                    continue
                    
                elif curr_high >= take_profit:
                    capital += current_stake * rr_ratio
                    trades.append('WIN')
                    in_position = False
                    current_step = 1  # Сброс на 1 шаг при победе
                    next_allowed_bar = i + 2
                    continue
                    
            elif pos_type == 'SHORT':
                if curr_high >= stop_loss:
                    capital -= current_stake
                    trades.append('LOSS')
                    in_position = False
                    current_step += 1
                    if current_step > max_steps:
                        current_step = 1
                    next_allowed_bar = i + 8
                    continue
                    
                elif curr_low <= take_profit:
                    capital += current_stake * rr_ratio
                    trades.append('WIN')
                    in_position = False
                    current_step = 1
                    next_allowed_bar = i + 2
                    continue
                    
            continue

        if i < next_allowed_bar:
            continue

        # -------------------------------------------------------------------------
        # 3. ПОИСК ТОЧКИ ВХОДА (ТРЕНД + RSI + ЗОНА)
        # -------------------------------------------------------------------------
        uptrend = (curr_close > ema100[i]) and (ema20[i] >= ema50[i] * 0.997) and (ema100[i] >= ema100[i-4])
        downtrend = (curr_close < ema100[i]) and (ema20[i] <= ema50[i] * 1.003) and (ema100[i] <= ema100[i-4])
        
        candle_range = curr_high - curr_low
        if candle_range == 0:
            continue

        # --- ВХОД В LONG ---
        if uptrend and demand_zones:
            matched_zone = None
            for z in reversed(demand_zones):
                if (i - z['bar'] >= 4) and (curr_low <= z['top'] or lows[i-1] <= z['top']):
                    matched_zone = z
                    break
                    
            if matched_zone is not None:
                rsi_ok = (rsis[i-1] <= 58) or (rsis[i] <= 58)
                bull_break = (curr_close > curr_open) and (curr_close > highs[i-1]) and (bodies[i] > curr_atr * 0.18)
                bull_pinbar = (curr_close > curr_open) and ((curr_close - curr_low) / candle_range >= 0.60) and (curr_close > closes[i-1])
                
                if rsi_ok and (bull_break or bull_pinbar):
                    stop_price = min(matched_zone['bottom'], curr_low, lows[i-1]) - (curr_atr * 0.45)
                    r_dist = curr_close - stop_price
                    
                    if (curr_atr * 0.40) <= r_dist <= (curr_atr * 1.80):
                        in_position = True
                        pos_type = 'LONG'
                        entry_price = curr_close
                        stop_loss = stop_price
                        take_profit = entry_price + (r_dist * rr_ratio)
                        
                        # Расчет ставки по Мартингейлу 1.5x для текущего шага
                        current_stake = round(base_stake * (1.5 ** (current_step - 1)), 2)
                        if current_step > max_step_reached:
                            max_step_reached = current_step
                            
                        demand_zones.remove(matched_zone)
                        continue

        # --- ВХОД В SHORT ---
        if downtrend and supply_zones:
            matched_zone = None
            for z in reversed(supply_zones):
                if (i - z['bar'] >= 4) and (curr_high >= z['bottom'] or highs[i-1] >= z['bottom']):
                    matched_zone = z
                    break
                    
            if matched_zone is not None:
                rsi_ok = (rsis[i-1] >= 42) or (rsis[i] >= 42)
                bear_break = (curr_close < curr_open) and (curr_close < lows[i-1]) and (bodies[i] > curr_atr * 0.18)
                bear_pinbar = (curr_close < curr_open) and ((curr_high - curr_close) / candle_range >= 0.60) and (curr_close < closes[i-1])
                
                if rsi_ok and (bear_break or bear_pinbar):
                    stop_price = max(matched_zone['top'], curr_high, highs[i-1]) + (curr_atr * 0.45)
                    r_dist = stop_price - curr_close
                    
                    if (curr_atr * 0.40) <= r_dist <= (curr_atr * 1.80):
                        in_position = True
                        pos_type = 'SHORT'
                        entry_price = curr_close
                        stop_loss = stop_price
                        take_profit = entry_price - (r_dist * rr_ratio)
                        
                        current_stake = round(base_stake * (1.5 ** (current_step - 1)), 2)
                        if current_step > max_step_reached:
                            max_step_reached = current_step
                            
                        supply_zones.remove(matched_zone)
                        continue

    wins = trades.count('WIN')
    losses = trades.count('LOSS')
    total = wins + losses
    winrate = (wins / total * 100) if total > 0 else 0.0
    net_profit = capital - initial_capital
    
    return {
        'symbol': symbol,
        'trades': total,
        'winrate': winrate,
        'max_step': max_step_reached,
        'final_capital': capital,
        'profit_usd': net_profit
    }

def main():
    exchange = ccxt.bybit()
    print(f"Запуск бэктеста v8.0 (Мартингейл 8 шагов х1.5 + TP 1.25R)...\n")
    
    results = []
    for symbol in SYMBOLS:
        df = fetch_historical_data(exchange, symbol, timeframe='1h', days=60)
        if df is not None and len(df) > 200:
            df = prepare_indicators(df)
            res = backtest_strategy(df, symbol)
            results.append(res)
            print(f"[{symbol}] Сделок: {res['trades']} | Винрейт: {res['winrate']:.1f}% | Макс.шаг: {res['max_step']} | Баланс: ${res['final_capital']:.2f} ({res['profit_usd']:+.2f}$)")
        else:
            print(f"[{symbol}] Пропущено (мало данных)")
        time.sleep(0.1)
            
    if results:
        df_res = pd.DataFrame(results)
        profitable_pairs = len(df_res[df_res['profit_usd'] > 0])
        total_profit = df_res['profit_usd'].sum()
        print("\n" + "="*65)
        print(" ИТОГОВАЯ СВОДКА v8.0 (МАРТИНГЕЙЛ 8 ШАГОВ x1.5) ")
        print("="*65)
        print(f"Всего сделок по портфелю: {df_res['trades'].sum()}")
        print(f"Плюсовых пар из {len(df_res)}: {profitable_pairs}")
        print(f"Средний винрейт по портфелю: {df_res['winrate'].mean():.2f}%")
        print(f"Общий финансовый результат по всем парам: {total_profit:+.2f}$")
        print(f"Итоговый баланс единого депозита (старт $150): ${150.0 + total_profit:.2f}")
        best = df_res.loc[df_res['profit_usd'].idxmax()]
        worst = df_res.loc[df_res['profit_usd'].idxmin()]
        print(f"Лучшая пара: {best['symbol']} ({best['profit_usd']:+.2f}$)")
        print(f"Худшая пара: {worst['symbol']} ({worst['profit_usd']:+.2f}$)")
        print("="*65)

if __name__ == "__main__":
    main()
