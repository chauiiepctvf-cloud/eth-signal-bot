
import os
import time
import logging
import threading
import hashlib
import hmac
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from flask import Flask

# ══════════════════════════════════════════════

# НАСТРОЙКИ

# ══════════════════════════════════════════════

TELEGRAM_TOKEN   = os.environ.get(“TELEGRAM_TOKEN”)
CHAT_ID          = os.environ.get(“CHAT_ID”)
SYMBOL           = “ETHUSDT”
SCAN_INTERVAL    = 5 * 60

BYBIT_API_KEY    = os.environ.get(“BYBIT_API_KEY”)
BYBIT_API_SECRET = os.environ.get(“BYBIT_API_SECRET”)

# ⚠️ Для теста — testnet. Когда готов к реальной торговле:

# замени на https://api.bybit.com

BYBIT_BASE_URL   = “https://api-testnet.bybit.com”

LEVERAGE         = 10
QTY              = 0.01      # количество ETH на сделку
MIN_SCORE        = 7         # минимальный балл для входа
MAX_SCORE        = 13        # максимально возможный балл

logging.basicConfig(format=”%(asctime)s [%(levelname)s] %(message)s”, level=logging.INFO)
log = logging.getLogger(**name**)

app = Flask(**name**)

@app.route(”/”)
def home():
return f”Scalp Bot | {datetime.now(timezone.utc).strftime(’%d.%m.%Y %H:%M’)} UTC”

# ══════════════════════════════════════════════

# TELEGRAM

# ══════════════════════════════════════════════

def send_telegram(text):
if not TELEGRAM_TOKEN or not CHAT_ID:
log.error(“TELEGRAM_TOKEN или CHAT_ID не заданы”)
return
try:
r = requests.post(
f”https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage”,
json={“chat_id”: CHAT_ID, “text”: text, “parse_mode”: “HTML”},
timeout=15
)
if r.status_code == 200:
log.info(“Telegram ✅”)
else:
log.error(f”Telegram ошибка: {r.text}”)
except Exception as e:
log.error(f”Telegram: {e}”)

# ══════════════════════════════════════════════

# BYBIT V5 API — ПРАВИЛЬНАЯ ПОДПИСЬ

# ══════════════════════════════════════════════

RECV_WINDOW = “5000”

def _sign(secret: str, payload: str) -> str:
return hmac.new(secret.encode(“utf-8”), payload.encode(“utf-8”), hashlib.sha256).hexdigest()

def bybit_post(endpoint: str, body: dict) -> dict:
“”“POST запрос к Bybit V5 с правильной подписью.”””
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
log.error(“Bybit ключи не заданы!”)
return {}
ts        = str(int(time.time() * 1000))
body_str  = json.dumps(body)
# Строка для подписи: timestamp + api_key + recv_window + body
sign_str  = ts + BYBIT_API_KEY + RECV_WINDOW + body_str
signature = _sign(BYBIT_API_SECRET, sign_str)

headers = {
    "X-BAPI-API-KEY":         BYBIT_API_KEY,
    "X-BAPI-SIGN":            signature,
    "X-BAPI-SIGN-TYPE":       "2",
    "X-BAPI-TIMESTAMP":       ts,
    "X-BAPI-RECV-WINDOW":     RECV_WINDOW,
    "Content-Type":           "application/json",
}

try:
    r = requests.post(
        BYBIT_BASE_URL + endpoint,
        headers=headers,
        data=body_str,
        timeout=10
    )
    data = r.json()
    log.info(f"Bybit {endpoint}: {data}")
    return data
except Exception as e:
    log.error(f"Bybit POST {endpoint}: {e}")
    return {}

def bybit_get(endpoint: str, params: dict = None) -> dict:
“”“GET запрос к Bybit V5 с правильной подписью.”””
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
return {}
if params is None:
    params = {}

ts         = str(int(time.time() * 1000))
query_str  = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
sign_str   = ts + BYBIT_API_KEY + RECV_WINDOW + query_str
signature  = _sign(BYBIT_API_SECRET, sign_str)

headers = {
    "X-BAPI-API-KEY":     BYBIT_API_KEY,
    "X-BAPI-SIGN":        signature,
    "X-BAPI-SIGN-TYPE":   "2",
    "X-BAPI-TIMESTAMP":   ts,
    "X-BAPI-RECV-WINDOW": RECV_WINDOW,
}

try:
    r = requests.get(
        BYBIT_BASE_URL + endpoint,
        headers=headers,
        params=params,
        timeout=10
    )
    return r.json()
except Exception as e:
    log.error(f"Bybit GET {endpoint}: {e}")
    return {}

# ══════════════════════════════════════════════

# BYBIT — ТОРГОВЫЕ ФУНКЦИИ

# ══════════════════════════════════════════════

def set_leverage(leverage: int = LEVERAGE) -> bool:
“”“Устанавливает плечо.
