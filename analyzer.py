import ccxt
import pandas as pd
import numpy as np

SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'ADA/USDT',
    'AVAX/USDT', 'DOGE/USDT', 'LINK/USDT', 'DOT/USDT', 'NEAR/USDT',
    'APT/USDT', 'ARB/USDT', 'OP/USDT', 'SUI/USDT', 'INJ/USDT',
    'FIL/USDT', 'RENDER/USDT', 'ATOM/USDT', 'LTC/USDT', 'TIA/USDT'
]

def fetch_historical_data(exchange, symbol, timeframe='4h', limit=400):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        return None

def find_market_structure(df, window=5):
    """Поиск тренда и фильтра объема"""
    df['swing_high'] = df['high'][(df['high'] == df['high'].rolling(window*2+1, center=True).max())]
    df['swing_low'] = df['low'][(df['low'] == df['low'].rolling(window*2+1, center=True).min())]
    
    df['last_swing_high'] = df['swing_high'].ffill()
    df['last_swing_low'] = df['swing_low'].ffill()
    
    df['is_uptrend'] = df['last_swing_high'] > df['last_swing_high'].shift(5)
    df['is_uptrend'] = df['is_uptrend'].fillna(True)
    
    # Скользящая средняя объема для фильтрации ликвидности
    df['volume_sma'] = df['volume'].rolling(20).mean()
    return df

def backtest_strategy(df, symbol):
    trades = []
    initial_capital = 150.0  # Стартовый депозит $150
    capital = initial_capital
    risk_per_trade = 0.02    # Риск 2% на сделку
    
    in_position = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    allocated_risk_amount = 0
    
    for i in range(25, len(df)):
        if capital <= 10:
            break
            
        current_close = df.loc[i, 'close']
        current_low = df.loc[i, 'low']
        current_high = df.loc[i, 'high']
        is_uptrend = df.loc[i, 'is_uptrend']
        current_volume = df.loc[i, 'volume']
        avg_volume = df.loc[i, 'volume_sma']
        
        if not in_position:
            # Улучшенный поиск зоны спроса: ищем импульсную свечу за последние 10 баров
            # Импульс = тело свечи больше среднего диапазона
            lookback_slice = df.loc[i-10:i-2]
            body_sizes = (lookback_slice['close'] - lookback_slice['open']).abs()
            avg_body = body_sizes.mean()
            
            # Находим свечу с сильным бычьим импульсом
            impulse_candles = lookback_slice[body_sizes > avg_body * 1.3]
            
            if not impulse_candles.empty:
                # Берем диапазон последней импульсной зоны спроса
                zone_low = impulse_candles['low'].min()
                zone_high = impulse_candles['high'].max()
                
                # Условия входа: 
                # 1. Тренд восходящий
                # 2. Цена откатила в зону спроса
                # 3. Объем на текущем баре выше или около среднего (подтверждение интереса)
                volume_ok = current_volume >= (avg_volume * 0.8)
                
                if is_uptrend and current_low <= zone_high and current_low >= zone_low and volume_ok:
                    in_position = True
                    entry_price = current_close
                    stop_loss = zone_low * 0.990  # Чуть шире стоп за зону
                    
                    risk_distance = entry_price - stop_loss
                    if risk_distance <= 0:
                        in_position = False
                        continue
                        
                    take_profit = entry_price + (risk_distance * 2.2) # Чуть выше R:R (1:2.2)
                    allocated_risk_amount = capital * risk_per_trade
                    
        elif in_position:
            if current_low <= stop_loss:
                capital -= allocated_risk_amount
                in_position = False
                trades.append({'result': 'LOSS'})
            elif current_high >= take_profit:
                profit = allocated_risk_amount * 2.2
                capital += profit
                in_position = False
                trades.append({'result': 'WIN'})
                
    wins = len([t for t in trades if t['result'] == 'WIN'])
    losses = len([t for t in trades if t['result'] == 'LOSS'])
    total = wins + losses
    winrate = (wins / total * 100) if total > 0 else 0
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
    print(f"Запуск продвинутого бэктеста (со структурами импульсов и объемом) для {len(SYMBOLS)} пар...\n")
    
    results = []
    for symbol in SYMBOLS:
        df = fetch_historical_data(exchange, symbol, timeframe='4h', limit=400)
        if df is not None and not df.empty:
            df = find_market_structure(df)
            res = backtest_strategy(df, symbol)
            results.append(res)
            print(f"[{symbol}] Сделок: {res['trades']} | Винрейт: {res['winrate']:.1f}% | Баланс: ${res['final_capital']:.2f} ({res['profit_usd']:+.2f}$)")
        else:
            print(f"[{symbol}] Пропущено (нет данных)")
            
    if results:
        df_res = pd.DataFrame(results)
        print("\n" + "="*55)
        print(" ИТОГОВАЯ СВОДКА (ПРОДВИНУТАЯ СТРАТЕГИЯ) ")
        print("="*55)
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
