import ccxt
import pandas as pd
import numpy as np

def calculate_indicators(df):
    # 1. Экспоненциальная средняя (EMA20)
    df['ema20'] = df['close'].ewm(span=20).mean()
    
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
    
    # 4. Анализ объемов (сравнение с 20-периодной средней)
    df['vol_ma20'] = df['volume'].rolling(window=20).mean()
    df['high_volume'] = df['volume'] > (df['vol_ma20'] * 1.5) # Объем выше среднего в 1.5 раза
    
    return df

def analyze_market_with_indicators():
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    exchange.load_markets()
    
    # Получаем топ-30 по объему
    tickers = exchange.fetch_tickers()
    futures_tickers = {s: d for s, d in tickers.items() if 'USDT' in s and d.get('quoteVolume')}
    sorted_coins = sorted(futures_tickers.items(), key=lambda x: x[1].get('quoteVolume', 0), reverse=True)
    top_30 = [item[0] for item in sorted_coins[:30]]
    
    strategy_signals = {}
    
    for symbol in top_30:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = calculate_indicators(df)
            
            last = df.iloc[-1]
            
            # Закономерность 1: Сигнал на покупку (Откат + Перепроданность + Объем)
            # Условия: цена коснулась или ушла ниже нижней полосы Боллинджера И RSI < 30 И объем выше среднего
            is_oversold = last['close'] <= last['lower_band'] and last['rsi'] < 30
            
            # Закономерность 2: Сигнал на продажу/шорт (Перекупленность)
            # Условия: цена вышла за верхнюю полосу Боллинджера И RSI > 70
            is_overbought = last['close'] >= last['upper_band'] and last['rsi'] > 70
            
            if is_oversold and last['high_volume']:
                signal = 'STRONG_BUY_DIP'
            elif is_overbought:
                signal = 'STRONG_SELL_TOP'
            else:
                signal = 'NEUTRAL'
                
            strategy_signals[symbol] = {
                'close': last['close'],
                'rsi': round(last['rsi'], 2),
                'signal': signal
            }
        except Exception as e:
            print(f"Ошибка по {symbol}: {e}")
            
    return strategy_signals

if __name__ == "__main__":
    signals = analyze_market_with_indicators()
    print("Результаты анализа по индикаторам:", signals)
