import threading
import time
import requests
import pandas as pd
import pandas_ta as ta
from flask import Flask, render_template, jsonify
from datetime import datetime
import concurrent.futures

# --- 配置 ---
BASE_URL = "https://fapi.binance.com"
INTERVAL = '12h'
RSI_PERIOD = 14
BBANDS_PERIOD = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
SCAN_INTERVAL_SECONDS = 900
MAX_WORKERS = 30

# --- 全局变量 ---
all_symbols_data = {}
data_lock = threading.Lock()

# --- Flask App 初始化 ---
app = Flask(__name__)


# --- 数据处理核心函数 ---
def process_symbol(symbol, ticker_data):
    """获取单个币种数据、计算所有指标并生成信号"""
    try:
        url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={INTERVAL}&limit=200"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        klines = response.json()

        df = pd.DataFrame(klines, columns=['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time',
                                           'quote_asset_volume', 'number_of_trades', 'taker_buy_base_asset_volume',
                                           'taker_buy_quote_asset_volume', 'ignore'])
        df['close'] = pd.to_numeric(df['close'])

        if len(df) < MACD_SLOW:
            return None

        # --- 一次性计算所有需要的指标 ---
        df.ta.rsi(length=RSI_PERIOD, append=True)
        df.ta.bbands(length=BBANDS_PERIOD, append=True)
        df.ta.macd(fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL, append=True)

        # --- 提取最新的指标数据 ---
        latest = df.iloc[-1]
        previous = df.iloc[-2]

        signals = []
        rsi_value = latest[f'RSI_{RSI_PERIOD}']
        if rsi_value < 30: signals.append("RSI 超卖")
        if rsi_value > 70: signals.append("RSI 超买")

        close_price = latest['close']
        lower_band = latest[f'BBL_{BBANDS_PERIOD}_2.0']
        upper_band = latest[f'BBU_{BBANDS_PERIOD}_2.0']
        if close_price < lower_band: signals.append("跌破布林下轨")
        if close_price > upper_band: signals.append("突破布林上轨")

        macd_line = latest[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
        signal_line = latest[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
        prev_macd_line = previous[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
        prev_signal_line = previous[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']

        if prev_macd_line < prev_signal_line and macd_line > signal_line: signals.append("MACD 金叉")
        if prev_macd_line > prev_signal_line and macd_line < signal_line: signals.append("MACD 死叉")

        stats = ticker_data.get(symbol, {})

        symbol_info = {
            "symbol": symbol,
            "price": f"{close_price:.4f}",
            "change": f"{float(stats.get('priceChangePercent', 0)):.2f}%",
            "quoteVolume": f"{int(float(stats.get('quoteVolume', 0)) / 1000000):,}M",
            "rsi": f"{rsi_value:.2f}",
            "bbl": f"{lower_band:.2f}",
            "bbu": f"{upper_band:.2f}",
            "macd": f"{macd_line:.2f}",
            "macds": f"{signal_line:.2f}",
            "signals": signals,
            "timestamp": datetime.now().strftime('%H:%M:%S')
        }
        return symbol_info
    except Exception:
        return None


# --- 后台监控任务 ---
def monitor_market():
    global all_symbols_data
    print("市场监控任务已启动...")
    while True:
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始新一轮扫描...")
        symbols = get_all_usdt_futures_symbols()
        if not symbols:
            time.sleep(60)
            continue
        ticker_data = get_24hr_ticker_data()

        temp_data_storage = {}
        print(f"正在使用 {MAX_WORKERS} 个线程并行获取 {len(symbols)} 个币种的数据...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_symbol = {executor.submit(process_symbol, symbol, ticker_data): symbol for symbol in symbols}

            processed_count = 0
            for future in concurrent.futures.as_completed(future_to_symbol):
                result = future.result()
                if result:
                    temp_data_storage[result['symbol']] = result
                processed_count += 1
                print(f"\r  进度: {processed_count}/{len(symbols)}", end="", flush=True)

        print("\n并行获取完成！")
        with data_lock:
            all_symbols_data = temp_data_storage
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 扫描完成，共更新 {len(all_symbols_data)} 个币种的数据。")
        time.sleep(SCAN_INTERVAL_SECONDS)


# --- 辅助函数 ---
def get_all_usdt_futures_symbols():
    try:
        url = f"{BASE_URL}/fapi/v1/exchangeInfo"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        symbols = [s['symbol'] for s in response.json()['symbols'] if
                   s['contractType'] == 'PERPETUAL' and s['status'] == 'TRADING' and s['symbol'].endswith('USDT')]
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 成功获取 {len(symbols)} 个 USDT 合约。")
        return symbols
    except Exception as e:
        print(f"错误：获取交易对列表失败 - {e}")
        return []


def get_24hr_ticker_data():
    try:
        url = f"{BASE_URL}/fapi/v1/ticker/24hr"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return {item['symbol']: item for item in response.json()}
    except Exception as e:
        print(f"错误：获取 24hr Ticker 数据失败 - {e}")
        return {}


# --- Flask 路由 ---
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/data')
def get_data():
    with data_lock:
        return jsonify(list(all_symbols_data.values()))


if __name__ == '__main__':
    monitor_thread = threading.Thread(target=monitor_market, daemon=True)
    monitor_thread.start()
    app.run(host='0.0.0.0', port=5000, debug=False)