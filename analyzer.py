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
    """Поиск локальных экстремумов и определение тренда колоночным методом"""
    df['swing_high'] = df['high'][(df['high'] == df['high'].rolling(window*2+1, center=True).max())]
    df['swing_low'] = df['low'][(df['low'] == df['low'].rolling(window*2+1, center=True).min())]
    
    # Заполняем пропуски последними значениями
    df['last_swing_high'] = df['swing_high'].ffill()
    df['last_swing_low'] = df['swing_low'].ffill()
    
    # Векторизованное определение тренда (сравнение колонок целиком)
    df['is_uptrend'] = df['last_swing_high'] > df['last_swing_high'].shift(5)
    df['is_uptrend'] = df['is_uptrend'].fillna(True) # Заглушка для первых строк
    
    return df

def backtest_strategy(df, symbol):
    trades = []
    initial_capital = 150.0  # Стартовый депозит $150
    capital = initial_capital
    risk_per_trade = 0.02    # Риск 2% от капитала на сделку
    
    in_position = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    position_type = None
    allocated_risk_amount = 0
    
    for i in range(20, len(df)):
        if capital <= 10:  # Защита от слива
            break
            
        current_close = df.loc[i, 'close']
        current_low = df.loc[i, 'low']
        current_high = df.loc[i, 'high']
        is_uptrend = df.loc[i, 'is_uptrend']
        
        if not in_position:
            # Ищем зону спроса по недавним барам
            zone_low = df.loc[i-8:i-2, 'low'].min()
            zone_high = df.loc[i-8:i-2, 'high'].max()
            
            # Условие входа: восходящий тренд + цена в зоне спроса
            if is_uptrend and current_low <= zone_high and current_low >= zone_low:
                in_position = True
                position_type = 'LONG'
                entry_price = current_close
                stop_loss = zone_low * 0.992  # Стоп под зоной
                
                risk_distance = entry_price - stop_loss
                if risk_distance <= 0:
                    in_position = False
                    continue
                    
                take_profit = entry_price + (risk_distance * 2.0) # R:R 1:2
                allocated_risk_amount = capital * risk_per_trade
                
        elif in_position:
            if position_type == 'LONG':
                if current_low <= stop_loss:
                    capital -= allocated_risk_amount
                    in_position = False
                    trades.append({'result': 'LOSS'})
                elif current_high >= take_profit:
                    profit = allocated_risk_amount * 2.0
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
    print(f"Запуск доработанного бэктеста (фикс. риск 2%, без мартингейла) для {len(SYMBOLS)} пар...\n")
    
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
        print("\n" + "="*50)
        print(" ИТОГОВАЯ СВОДКА (ДОРАБОТАННАЯ СТРАТЕГИЯ) ")
        print("="*50)
        print(f"Средний винрейт по портфелю: {df_res['winrate'].mean():.2f}%")
        print(f"Средний конечный баланс: ${df_res['final_capital'].mean():.2f}")
        print(f"Общий финансовый результат по портфелю: {df_res['profit_usd'].sum():+.2f}$")
        best = df_res.loc[df_res['profit_usd'].idxmax()]
        worst = df_res.loc[df_res['profit_usd'].idxmin()]
        print(f"Лучшая пара: {best['symbol']} ({best['profit_usd']:+.2f}$)")
        print(f"Худшая пара: {worst['symbol']} ({worst['profit_usd']:+.2f}$)")
        print("="*50)

if __name__ == "__main__":
    main()
