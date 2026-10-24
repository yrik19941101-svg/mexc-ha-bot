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

def backtest_strategy(df, symbol):
    trades = []
    initial_capital = 150.0  # Депозит $150
    capital = initial_capital
    
    base_stake = 0.5         # Первоначальная сделка $0.5
    max_steps = 4            # Мартингейл на 4 шага
    current_step = 1
    
    in_position = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    position_type = None
    current_stake = 0
    
    for i in range(10, len(df)):
        if capital <= 0:
            break  # Если депозит полностью слит
            
        current_close = df.loc[i, 'close']
        current_low = df.loc[i, 'low']
        current_high = df.loc[i, 'high']
        
        if not in_position:
            recent_low = df.loc[i-5:i-1, 'low'].min()
            
            # Условие входа в Лонг
            if current_low <= recent_low * 1.002:
                in_position = True
                position_type = 'LONG'
                entry_price = current_close
                stop_loss = recent_low * 0.993
                risk_distance = entry_price - stop_loss
                if risk_distance <= 0:
                    in_position = False
                    continue
                take_profit = entry_price + (risk_distance * 2.5) # R:R 1:2.5
                
                # Рассчитываем размер ставки по шагам мартингейла (0.5 -> 1.0 -> 2.0 -> 4.0)
                current_stake = base_stake * (2 ** (current_step - 1))
                
        elif in_position:
            if position_type == 'LONG':
                if current_low <= stop_loss:
                    # Убыток: вычитаем текущую ставку из капитала
                    capital -= current_stake
                    in_position = False
                    trades.append({'result': 'LOSS', 'step': current_step})
                    
                    # Увеличиваем шаг мартингейла
                    current_step += 1
                    if current_step > max_steps:
                        current_step = 1 # Сброс после 4 шагов
                        
                elif current_high >= take_profit:
                    # Прибыль: при R:R 1:2.5 получаем ставку * 2.5
                    profit = current_stake * 2.5
                    capital += profit
                    in_position = False
                    trades.append({'result': 'WIN', 'step': current_step})
                    
                    # Сброс мартингейла при победе
                    current_step = 1
                    
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
    print(f"Запуск бэктеста с Мартингейлом (4 шага, старт $150) для {len(SYMBOLS)} пар...\n")
    
    results = []
    for symbol in SYMBOLS:
        df = fetch_historical_data(exchange, symbol, timeframe='4h', limit=400)
        if df is not None and not df.empty:
            res = backtest_strategy(df, symbol)
            results.append(res)
            print(f"[{symbol}] Сделок: {res['trades']} | Винрейт: {res['winrate']:.1f}% | Баланс: ${res['final_capital']:.2f} ({res['profit_usd']:+.2f}$)")
        else:
            print(f"[{symbol}] Пропущено (нет данных)")
            
    if results:
        df_res = pd.DataFrame(results)
        print("\n" + "="*50)
        print(" ИТОГОВАЯ СВОДКА (МАРТИНГЕЙЛ) ")
        print("="*50)
        print(f"Средний винрейт: {df_res['winrate'].mean():.2f}%")
        print(f"Средний конечный баланс: ${df_res['final_capital'].mean():.2f}")
        print(f"Общий финансовый результат по портфелю: {df_res['profit_usd'].sum():+.2f}$")
        best = df_res.loc[df_res['profit_usd'].idxmax()]
        worst = df_res.loc[df_res['profit_usd'].idxmin()]
        print(f"Лучшая пара: {best['symbol']} ({best['profit_usd']:+.2f}$)")
        print(f"Худшая пара: {worst['symbol']} ({worst['profit_usd']:+.2f}$)")
        print("="*50)

if __name__ == "__main__":
    main()
