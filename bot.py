import requests
import pandas as pd
import numpy as np
from datetime import datetime
import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
SYMBOL = "ETHUSDT"

def send(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"Ошибка: {e}")

def get_klines(interval="1h", limit=100):
    try:
        data = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
            timeout=10
        ).json()
        closes = [float(c[4]) for c in data]
        highs = [float(c[2]) for c in data]
        lows = [float(c[3]) for c in data]
        volumes = [float(c[5]) for c in data]
        return closes, highs, lows, volumes
    except:
        return None, None, None, None

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calculate_ma(closes, period=50):
    if len(closes) < period:
        return closes[-1]
    return sum(closes[-period:]) / period

def get_funding_rate():
    try:
        data = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL}", timeout=10).json()
        return float(data["lastFundingRate"])
    except:
        return 0.0

def get_open_interest():
    try:
        data = requests.get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={SYMBOL}", timeout=10).json()
        return float(data["openInterest"])
    except:
        return 0.0

def main():
    closes_1h, _, _, vols_1h = get_klines("1h", 100)
    if closes_1h is None:
        send("❌ Ошибка загрузки данных")
        return
    
    price = closes_1h[-1]
    ma50 = calculate_ma(closes_1h, 50)
    rsi = calculate_rsi(closes_1h, 14)
    funding = get_funding_rate()
    oi = get_open_interest()
    avg_vol = sum(vols_1h[-20:]) / 20 if vols_1h else 0
    vol_ok = vols_1h[-1] > avg_vol * 1.3 if vols_1h else False
    
    message = f"""📊 <b>ETH Анализ</b> | {datetime.now().strftime('%d.%m.%Y %H:%M')}

💰 <b>Цена:</b> ${price:.0f}
📉 <b>MA50:</b> ${ma50:.0f}
📊 <b>RSI(14):</b> {rsi:.1f}
💸 <b>Фандинг:</b> {funding*100:.4f}%
📐 <b>OI:</b> {oi/1e6:.2f} млн
📊 <b>Объём:</b> {'✅ высокий' if vol_ok else '⚠️ средний'}

━━━━━━━━━━━━━━━━━━━━━━
🎯 <b>СИГНАЛЫ</b>
"""
    signals = []
    if price > ma50 and rsi < 40 and funding < 0 and oi > 2_000_000:
        signals.append("🟢 СРЕДНЕСРОЧНЫЙ ПОКУПКА")
    elif price < ma50 and rsi > 60 and funding > 0.005 and oi < 1_500_000:
        signals.append("🔴 СРЕДНЕСРОЧНЫЙ ПРОДАЖА")
    
    if signals:
        message += "\n".join(signals)
    else:
        message += "🟡 ЖДИ — нет чётких сигналов"
    
    send(message)

if __name__ == "__main__":
    main()
