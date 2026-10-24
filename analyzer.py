import ccxt
import pandas as pd
import numpy as np

def calculate_strategy_indicators(df):
    """Рассчитывает индикаторы для импульсной стратегии."""
    # 1. Скользящие средние для тренда
    df['ema20'] = df['close'].ewm(span=20).mean()
    df['ema50'] = df['close'].ewm(span=50).mean()
    
    # 2. Полосы Боллинджера (20, 2)
    df['sma20'] = df['close'].rolling(window=20).mean()
    df['std20'] = df['close'].rolling(window=20).std()
    df['upper_band'] = df['sma20'] + (df['std20'] * 2)
    df['lower_band'] = df['sma20'] - (df['std20'] * 2)
    
    # 3. RSI (14 периодов)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # 4. Объемный всплеск (в 2 раза выше средних 20 свечей)
    df['vol_ma20'] = df['volume'].rolling(window=20).mean()
    df['volume_spike'] = df['volume'] > (df['vol_ma20'] * 2.0)
    
    return df

def run_smart_strategy():
    print("Подключение к Binance Futures и загрузка данных за 30 дней...")
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    exchange.load_markets()
    
    # Получаем топ-30 монет по объему
    tickers = exchange.fetch_tickers()
    futures_tickers = {s: d for s, d in tickers.items() if 'USDT' in s and d.get('quoteVolume')}
    sorted_coins = sorted(futures_tickers.items(), key=lambda x: x[1].get('quoteVolume', 0), reverse=True)
    top_30 = [item[0] for item in sorted_coins[:30]]
    
    strategy_results = {}
    
    print(f"Анализ {len(top_30)} монет по импульсной стратегии...")
    
    for symbol in top_30:
        try:
            # 720 часовых свечей = 30 дней
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=720)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = calculate_strategy_indicators(df)
            
            last = df.iloc[-1]
            prev = df.iloc[-2] # Предыдущая свеча для поиска пробоя
            
            current_price = float(last['close'])
            rsi_val = float(last['rsi']) if not pd.isna(last['rsi']) else 50.0
            
            # Логика стратегии «Импульсный пробой волатильности»
            # Условия для покупки: 
            # 1. Цена пробила верхнюю полосу Боллинджера
            # 2. Наблюдается мощный всплеск объема
            # 3. RSI вышел из зоны накопления (> 50, но еще не перекуплен < 75)
            is_breakout = (prev['close'] <= prev['upper_band']) and (current_price > last['upper_band'])
            
            if is_breakout and last['volume_spike'] and (55 < rsi_val < 75):
                signal = 'STRONG_BUY_BREAKOUT'
                # Расчет уровней риск-менеджмента для стратегии
                entry_price = current_price
                stop_loss = round(entry_price * 0.97, 4)   # Риск 3%
                take_profit = round(entry_price * 1.06, 4) # Цель +6% (ROI профит)
            else:
                signal = 'WAIT / HOLD'
                entry_price = current_price
                stop_loss = 0
                take_profit = 0
                
            strategy_results[symbol] = {
                'price': entry_price,
                'rsi': round(rsi_val, 2),
                'signal': signal,
                'stop_loss': stop_loss,
                'take_profit': take_profit
            }
        except Exception as e:
            print(f"Ошибка по монете {symbol}: {e}")
            
    return strategy_results

if __name__ == "__main__":
    results = run_smart_strategy()
    print("\n==============================================")
    print("      РЕЗУЛЬТАТЫ ИМПУЛЬСНОЙ СТРАТЕГИИ ТОРГОВЛИ     ")
    print("==============================================")
    for coin, data in results.items():
        if data['signal'] == 'STRONG_BUY_BREAKOUT':
            print(f"🚀 СИГНАЛ НА ПОКУПКУ: {coin}")
            print(f"   Цена входа: {data['price']} | RSI: {data['rsi']}")
            print(f"   Стоп-лосс: {data['stop_loss']} | Тейк-профит: {data['take_profit']}")
            print("-" * 46)
        else:
            print(f"{coin} -> Цена: {data['price']}, RSI: {data['rsi']}, Статус: {data['signal']}")
