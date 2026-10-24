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
    """Загрузка данных за 60 дней (1440 часовых свечей) в два запроса"""
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
    """Расчет структуры, тренда и волатильности (ATR) без заглядывания в будущее"""
    # EMA для фильтрации глобального направления
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema150'] = df['close'].ewm(span=150, adjust=False).mean()
    
    # ATR (14) для расчета адаптивных стопов и импульсов
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    
    # Размер тела свечи
    df['body'] = (df['close'] - df['open']).abs()
    return df

def backtest_strategy(df, symbol):
    initial_capital = 150.0
    capital = initial_capital
    risk_per_trade = 0.02  # Риск 2% ($3 на старте)
    
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    ema50 = df['ema50'].values
    ema150 = df['ema150'].values
    atrs = df['atr'].values
    bodies = df['body'].values
    
    in_position = False
    pos_type = None        # 'LONG' или 'SHORT'
    entry_price = 0.0
    stop_loss = 0.0
    tp1_price = 0.0        # Уровень 1R (фиксация 50% и перенос в БУ)
    tp2_price = 0.0        # Уровень 2.2R (полный тейк)
    be_activated = False   # Флаг перевода в безубыток
    risk_usd = 0.0
    
    trades = []
    
    for i in range(150, len(df)):
        if capital <= 10:
            break
            
        curr_open = opens[i]
        curr_high = highs[i]
        curr_low = lows[i]
        curr_close = closes[i]
        curr_atr = atrs[i]
        
        if np.isnan(curr_atr) or curr_atr == 0:
            continue
            
        # 1. СОПРОВОЖДЕНИЕ ОТКРЫТОЙ ПОЗИЦИИ
        if in_position:
            if pos_type == 'LONG':
                # Сначала проверяем стоп-лосс (консервативный тест)
                if curr_low <= stop_loss:
                    if be_activated:
                        # Вторая половина закрылась по безубытку, но первая дала +0.5R
                        capital += risk_usd * 0.55
                        trades.append('WIN')
                    else:
                        # Полный стоп (-1R)
                        capital -= risk_usd
                        trades.append('LOSS')
                    in_position = False
                    continue
                
                # Проверка полного тейк-профита (TP2 = 2.2R)
                if curr_high >= tp2_price:
                    # 50% закрыто на 1R (+0.5R) и 50% на 2.2R (+1.1R) = +1.6R чистыми
                    capital += risk_usd * 1.6
                    trades.append('WIN')
                    in_position = False
                    continue
                    
                # Проверка первого тейка (TP1 = 1.0R) -> перенос стопа в безубыток
                if not be_activated and curr_high >= tp1_price:
                    be_activated = True
                    stop_loss = entry_price  # Стоп в безубыток
                    
            elif pos_type == 'SHORT':
                if curr_high >= stop_loss:
                    if be_activated:
                        capital += risk_usd * 0.55
                        trades.append('WIN')
                    else:
                        capital -= risk_usd
                        trades.append('LOSS')
                    in_position = False
                    continue
                    
                if curr_low <= tp2_price:
                    capital += risk_usd * 1.6
                    trades.append('WIN')
                    in_position = False
                    continue
                    
                if not be_activated and curr_low <= tp1_price:
                    be_activated = True
                    stop_loss = entry_price
                    
            continue

        # 2. ПОИСК ТОЧКИ ВХОДА (Зона спроса/предложения + подтверждение разворота)
        # Определяем глобальный тренд
        uptrend = (curr_close > ema150[i]) and (ema50[i] > ema150[i])
        downtrend = (curr_close < ema150[i]) and (ema50[i] < ema150[i])
        
        # Окно поиска недавней зоны консолидации/импульса (последние 20 свечей)
        zone_low = np.min(lows[i-20:i-2])
        zone_high = np.max(highs[i-20:i-2])
        
        # --- СЦЕНАРИЙ LONG (Зона спроса + бычий слом/поглощение) ---
        if uptrend:
            # Цена недавно спустилась в нижнюю часть диапазона (зону спроса)
            touched_demand = (curr_low <= zone_low + curr_atr * 0.8) or (lows[i-1] <= zone_low + curr_atr * 0.8)
            # Подтверждение (Шаг 3): текущая свеча бычья и закрылась выше максимума предыдущей свечи (локальный слом вверх)
            bullish_confirm = (curr_close > curr_open) and (curr_close > highs[i-1]) and (bodies[i] > curr_atr * 0.25)
            
            if touched_demand and bullish_confirm:
                stop_price = min(curr_low, lows[i-1]) - (curr_atr * 0.3)
                risk_dist = curr_close - stop_price
                
                if risk_dist > 0 and (risk_dist / curr_close) < 0.04:  # Фильтр адекватного стопа (<4%)
                    in_position = True
                    pos_type = 'LONG'
                    entry_price = curr_close
                    stop_loss = stop_price
                    tp1_price = entry_price + (risk_dist * 1.0)  # На 1R берем часть и ставим БУ
                    tp2_price = entry_price + (risk_dist * 2.2)  # Основная цель
                    be_activated = False
                    risk_usd = capital * risk_per_trade
                    continue

        # --- СЦЕНАРИЙ SHORT (Зона предложения + медвежий слом/поглощение) ---
        if downtrend:
            # Цена поднялась в верхнюю часть диапазона (зону предложения)
            touched_supply = (curr_high >= zone_high - curr_atr * 0.8) or (highs[i-1] >= zone_high - curr_atr * 0.8)
            # Подтверждение: текущая свеча медвежья и пробила минимум предыдущей свечи (локальный слом вниз)
            bearish_confirm = (curr_close < curr_open) and (curr_close < lows[i-1]) and (bodies[i] > curr_atr * 0.25)
            
            if touched_supply and bearish_confirm:
                stop_price = max(curr_high, highs[i-1]) + (curr_atr * 0.3)
                risk_dist = stop_price - curr_close
                
                if risk_dist > 0 and (risk_dist / curr_close) < 0.04:
                    in_position = True
                    pos_type = 'SHORT'
                    entry_price = curr_close
                    stop_loss = stop_price
                    tp1_price = entry_price - (risk_dist * 1.0)
                    tp2_price = entry_price - (risk_dist * 2.2)
                    be_activated = False
                    risk_usd = capital * risk_per_trade
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
        'final_capital': capital,
        'profit_usd': net_profit
    }

def main():
    exchange = ccxt.bybit()
    print(f"Запуск бэктеста v4.0 (1h, 60 дней, LONG+SHORT, подтверждение + БУ на 1R)...\n")
    
    results = []
    for symbol in SYMBOLS:
        df = fetch_historical_data(exchange, symbol, timeframe='1h', days=60)
        if df is not None and len(df) > 200:
            df = prepare_indicators(df)
            res = backtest_strategy(df, symbol)
            results.append(res)
            print(f"[{symbol}] Сделок: {res['trades']} | Винрейт: {res['winrate']:.1f}% | Баланс: ${res['final_capital']:.2f} ({res['profit_usd']:+.2f}$)")
        else:
            print(f"[{symbol}] Пропущено (мало данных)")
        time.sleep(0.1)
            
    if results:
        df_res = pd.DataFrame(results)
        print("\n" + "="*55)
        print(" ИТОГОВАЯ СВОДКА v4.0 (ПОДТВЕРЖДЕНИЕ + БЕЗУБЫТОК) ")
        print("="*55)
        print(f"Всего сделок по портфелю: {df_res['trades'].sum()}")
        print(f"Средний винрейт по портфелю: {df_res['winrate'].mean():.2f}%")
        print(f"Средний конечный баланс: ${df_res['final_capital'].mean():.2f}")
        print(f"Общий финансовый результат по портфелю: {df_res['profit_usd'].sum():+.2f}$")
        best = df_res.loc[df_res['profit_usd'].idxmax()]
        worst = df_res.loc[df_res['profit_usd'].idxmin()]
        print(f"Лучшая пара: {best['symbol']} ({best['profit_usd']:+.2f}$)")
        print(f"Худшая пара: {worst['symbol']} ({worst['profit_usd']:+.2f}$)")
        print("="*55)

if __name__ == "__main__":
    main()
