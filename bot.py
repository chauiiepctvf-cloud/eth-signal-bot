import os
import time
import logging
import threading
import hashlib
import hmac
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from flask import Flask

# ══════════════════════════════════════════════
# НАСТРОЙКИ
# ══════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("8710461065:AAEtou4YT-j283WLBX2AsouBEX9mQDXsGicх")
CHAT_ID = os.environ.get("CHAT_ID")
SYMBOL = "ETHUSDT"
SCAN_INTERVAL = 30 * 60  # 30 минут

# Bybit Testnet
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET")
BYBIT_BASE_URL = "https://api-testnet.bybit.com"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def home():
    return f"Scalp Bot работает | {datetime.now().strftime('%d.%m.%Y %H:%M')} UTC"

# ══════════════════════════════════════════════
# TELEGRAM (без HTML)
# ══════════════════════════════════════════════
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
        if r.status_code == 200:
            log.info("Сообщение отправлено")
        else:
            log.error(f"Ошибка: {r.text}")
    except Exception as e:
        log.error(f"Telegram: {e}")

# ══════════════════════════════════════════════
# BYBIT API
# ══════════════════════════════════════════════
def bybit_request(endpoint, params=None):
    timestamp = int(time.time() * 10**3)
    if params is None:
        params = {}
    params["api_key"] = BYBIT_API_KEY
    params["timestamp"] = timestamp
    param_str = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    params["sign"] = signature
    url = f"{BYBIT_BASE_URL}{endpoint}"
    try:
        return requests.post(url, data=params, timeout=10).json()
    except Exception as e:
        log.error(f"Bybit: {e}")
        return None

def set_leverage(leverage=10):
    return bybit_request("/v5/position/set-leverage", {"symbol": SYMBOL, "leverage": str(leverage)})

def open_position(side, quantity, stop_loss, take_profit):
    set_leverage()
    return bybit_request("/v5/order/create", {
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(quantity),
        "timeInForce": "GoodTillCancel",
        "stopLoss": str(stop_loss),
        "takeProfit": str(take_profit)
    })

# ══════════════════════════════════════════════
# ДАННЫЕ
# ══════════════════════════════════════════════
def get_klines(interval="15m", limit=100):
    url = f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
    data = requests.get(url).json()
    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for c in ("open", "high", "low", "close", "volume", "taker_buy_base"):
        df[c] = df[c].astype(float)
    return df

def get_funding_rate():
    data = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL}").json()
    return float(data["lastFundingRate"])

def get_orderbook_imbalance():
    data = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL}&limit=20").json()
    bids = sum(float(b[1]) for b in data["bids"])
    asks = sum(float(a[1]) for a in data["asks"])
    total = bids + asks
    return round((bids - asks) / total * 100, 1) if total else 0.0

def calc_indicators(df):
    df["MA50"] = df["close"].rolling(50).mean()
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    
    hl = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(com=13, adjust=False).mean()
    
    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > df["vol_avg"] * 1.3
    return df

def get_scalp_signal(df, funding, ob):
    row = df.iloc[-1]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]
    vol_ok = row["vol_spike"]
    
    if price > row["MA50"] and rsi < 40 and funding < 0 and vol_ok and ob > 5:
        direction = "LONG"
        entry = price
        stop = entry - atr * 0.6
        tp = entry + atr * 1.2
        reason = f"RSI={rsi:.0f}, volume high, orderbook +{ob:.0f}%"
        return direction, entry, stop, tp, reason
    elif price < row["MA50"] and rsi > 60 and funding > 0.003 and vol_ok and ob < -5:
        direction = "SHORT"
        entry = price
        stop = entry + atr * 0.6
        tp = entry - atr * 1.2
        reason = f"RSI={rsi:.0f}, volume high, orderbook {ob:.0f}%"
        return direction, entry, stop, tp, reason
    return None, None, None, None, None

def run_scan():
    log.info("Сканирование...")
    df = get_klines("15m", 100)
    if df is None:
        send_telegram("Data error")
        return
    df = calc_indicators(df)
    funding = get_funding_rate()
    ob = get_orderbook_imbalance()
    direction, entry, stop, tp, reason = get_scalp_signal(df, funding, ob)
    if direction:
        msg = f"SCALP SIGNAL\nDirection: {direction}\nEntry: {entry:.2f}\nStop: {stop:.2f}\nTP: {tp:.2f}\nReason: {reason}"
        send_telegram(msg)
        if BYBIT_API_KEY and BYBIT_API_SECRET:
            qty = 0.01
            if direction == "LONG":
                order = open_position("Buy", qty, stop, tp)
            else:
                order = open_position("Sell", qty, stop, tp)
            if order and order.get("retCode") == 0:
                send_telegram(f"Order sent: {direction}")
            else:
                send_telegram(f"Order error: {order}")
    else:
        send_telegram(f"WAIT | Price: {df.iloc[-1]['close']:.0f} | RSI: {df.iloc[-1]['RSI']:.0f}")

def bot_loop():
    send_telegram(f"Scalp Bot started | interval {SCAN_INTERVAL // 60} min")
    while True:
        run_scan()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
