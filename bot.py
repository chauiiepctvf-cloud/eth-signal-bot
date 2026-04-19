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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
SYMBOL = "ETHUSDT"
SCAN_INTERVAL = 5 * 60                     # 5 минут для скальпинга

BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET")
BYBIT_BASE_URL = "https://api-testnet.bybit.com"
LEVERAGE = 10
QTY = 0.01

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def home():
    return f"Scalp Bot works | {datetime.now().strftime('%d.%m.%Y %H:%M')} UTC"

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════
def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.error("TELEGRAM_TOKEN or CHAT_ID not set!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
        if r.status_code == 200:
            log.info("Telegram: message sent")
        else:
            log.error(f"Telegram failed: {r.text}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ══════════════════════════════════════════════
# BYBIT API
# ══════════════════════════════════════════════
def bybit_request(endpoint, params=None):
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        return None
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
        log.error(f"Bybit error: {e}")
        return None

def set_leverage(leverage=LEVERAGE):
    return bybit_request("/v5/position/set-leverage", {
        "symbol": SYMBOL,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage)
    })

def open_position(side, quantity, stop_loss, take_profit):
    set_leverage()
    return bybit_request("/v5/order/create", {
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(quantity),
        "timeInForce": "GoodTillCancel",
        "stopLoss": str(round(stop_loss, 2)),
        "takeProfit": str(round(take_profit, 2))
    })

# ══════════════════════════════════════════════
# ДАННЫЕ
# ══════════════════════════════════════════════
def get_klines(interval="5m", limit=150):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
        data = requests.get(url, timeout=10).json()
        df = pd.DataFrame(data, columns=[
            "time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        for c in ("open", "high", "low", "close", "volume", "taker_buy_base"):
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        log.error(f"get_klines error: {e}")
        return None

def get_funding_rate():
    try:
        data = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL}", timeout=10).json()
        return float(data["lastFundingRate"])
    except Exception as e:
        log.error(f"funding error: {e}")
        return 0.0

def get_orderbook_imbalance():
    try:
        data = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL}&limit=20", timeout=10).json()
        bids = sum(float(b[1]) for b in data["bids"])
        asks = sum(float(a[1]) for a in data["asks"])
        total = bids + asks
        return round((bids - asks) / total * 100, 1) if total else 0.0
    except Exception as e:
        log.error(f"orderbook error: {e}")
        return 0.0

# ══════════════════════════════════════════════
# ИНДИКАТОРЫ (улучшено для скальпинга)
# ══════════════════════════════════════════════
def calc_indicators(df):
    # EMA — быстрее MA, лучше для скальпинга
    df["EMA9"] = df["close"].ewm(span=9).mean()
    df["EMA21"] = df["close"].ewm(span=21).mean()
    df["EMA50"] = df["close"].ewm(span=50).mean()

    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # ATR
    hl = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(com=13, adjust=False).mean()

    # VWAP
    df["VWAP"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

    # Bollinger Bands
    df["BB_mid"] = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    df["BB_upper"] = df["BB_mid"] + 2 * std
    df["BB_lower"] = df["BB_mid"] - 2 * std
    df["BB_width"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_mid"]

    # Объём
    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > df["vol_avg"] * 1.5

    # Momentum
    df["momentum"] = df["close"] - df["close"].shift(4)

    # Taker buy ratio (давление покупателей)
    df["buy_ratio"] = df["taker_buy_base"] / df["volume"].replace(0, np.nan)

    return df

# ══════════════════════════════════════════════
# СИГНАЛ (скоринг система)
# ══════════════════════════════════════════════
def get_scalp_signal(df, funding, ob):
    row = df.iloc[-1]
    prev = df.iloc[-2]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]

    # Фильтр: не торгуем при слишком узком рынке
    if row["BB_width"] < 0.008:
        return None, None, None, None, None

    long_score = 0
    short_score = 0

    # 1. EMA тренд (макс 2 очка)
    if row["EMA9"] > row["EMA21"] > row["EMA50"]:
        long_score += 2
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]:
        short_score += 2

    # 2. RSI (макс 2 очка)
    if 28 < rsi < 45:
        long_score += 2
    elif 55 < rsi < 72:
        short_score += 2

    # 3. VWAP (макс 1 очко)
    if price > row["VWAP"]:
        long_score += 1
    else:
        short_score += 1

    # 4. Bollinger Bands (макс 2 очка)
    if price <= row["BB_lower"]:
        long_score += 2
    elif price >= row["BB_upper"]:
        short_score += 2

    # 5. Объём (макс 1 очко)
    if row["vol_spike"]:
        long_score += 1
        short_score += 1

    # 6. Funding rate (макс 1 очко)
    if funding < -0.001:
        long_score += 1
    elif funding > 0.003:
        short_score += 1

    # 7. Order book (макс 2 очка)
    if ob > 8:
        long_score += 2
    elif ob < -8:
        short_score += 2

    # 8. Momentum (макс 1 очко)
    if row["momentum"] > 0 and prev["momentum"] > 0:
        long_score += 1
    elif row["momentum"] < 0 and prev["momentum"] < 0:
        short_score += 1

    # 9. Taker buy ratio (макс 1 очко)
    if row["buy_ratio"] > 0.55:
        long_score += 1
    elif row["buy_ratio"] < 0.45:
        short_score += 1

    # Итого макс = 13 очков, порог = 7
    MAX_SCORE = 13
    THRESHOLD = 7

    if long_score >= THRESHOLD:
        entry = price
        stop = round(entry - atr * 0.5, 2)
        tp = round(entry + atr * 1.5, 2)
        reason = (f"Score={long_score}/{MAX_SCORE} | RSI={rsi:.0f} | "
                  f"OB={ob:.1f}% | Fund={funding:.4f} | "
                  f"BB={'нижняя' if price <= row['BB_lower'] else 'норма'} | "
                  f"VWAP={'выше' if price > row['VWAP'] else 'ниже'}")
        return "LONG", entry, stop, tp, reason

    if short_score >= THRESHOLD:
        entry = price
        stop = round(entry + atr * 0.5, 2)
        tp = round(entry - atr * 1.5, 2)
        reason = (f"Score={short_score}/{MAX_SCORE} | RSI={rsi:.0f} | "
                  f"OB={ob:.1f}% | Fund={funding:.4f} | "
                  f"BB={'верхняя' if price >= row['BB_upper'] else 'норма'} | "
                  f"VWAP={'выше' if price > row['VWAP'] else 'ниже'}")
        return "SHORT", entry, stop, tp, reason

    return None, None, None, None, None

# ══════════════════════════════════════════════
# ОСНОВНОЙ СКАН
# ══════════════════════════════════════════════
def run_scan():
    log.info("Scanning...")
    df = get_klines("5m", 150)
    if df is None or df.empty:
        send_telegram("❌ Ошибка получения данных")
        return

    df = calc_indicators(df)
    funding = get_funding_rate()
    ob = get_orderbook_imbalance()
    direction, entry, stop, tp, reason = get_scalp_signal(df, funding, ob)

    row = df.iloc[-1]
    rsi = row["RSI"]
    price = row["close"]
    atr = row["ATR"]
    bb_width = row["BB_width"]

    if direction:
        emoji = "🟢" if direction == "LONG" else "🔴"
        rr = round(abs(tp - entry) / abs(entry - stop), 2)
        msg = (f"{emoji} SCALP SIGNAL — {direction}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f" Entry: {entry:.2f}\n"
               f" Stop: {stop:.2f}\n"
               f" TP: {tp:.2f}\n"
               f" R/R: 1:{rr}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f" {reason}\n"
               f"━━━━━━━━━━━━━━━━━━━━\n"
               f"ATR: {atr:.2f} | BB width: {bb_width:.4f}")
        send_telegram(msg)

        if BYBIT_API_KEY and BYBIT_API_SECRET:
            order = open_position("Buy" if direction == "LONG" else "Sell", QTY, stop, tp)
            if order and order.get("retCode") == 0:
                send_telegram(f"✅ Ордер отправлен: {direction} {QTY} {SYMBOL}")
            else:
                send_telegram(f"❌ Ошибка ордера: {order}")
    else:
        # Статус без сигнала
        vwap_pos = "выше" if price > row["VWAP"] else "ниже"
        trend = "▲" if row["EMA9"] > row["EMA21"] else "▼"
        send_telegram(f"🟡 ОЖИДАНИЕ {trend}\n"
                      f"Price: {price:.2f} | RSI: {rsi:.0f}\n"
                      f"VWAP: {vwap_pos} | OB: {ob:.1f}%\n"
                      f"Fund: {funding:.4f} | ATR: {atr:.2f}")

# ══════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════
def bot_loop():
    send_telegram(f"🚀 Scalp Bot запущен\n"
                  f"Символ: {SYMBOL} | Интервал: {SCAN_INTERVAL // 60} мин\n"
                  f"Плечо: {LEVERAGE}x | Объём: {QTY}")
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Loop error: {e}")
            send_telegram(f"❌ Ошибка бота: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
