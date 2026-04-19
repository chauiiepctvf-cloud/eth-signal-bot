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
from datetime import datetime
from flask import Flask

# ══════════════════════════════════════════════
# НАСТРОЙКИ
# ══════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID          = os.environ.get("CHAT_ID")
SYMBOL           = "ETHUSDT"
SCAN_INTERVAL    = 5 * 60

BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET")
BYBIT_BASE_URL   = "https://api-testnet.bybit.com"

LEVERAGE         = 10
QTY              = 0.01
MIN_SCORE        = 7
MAX_SCORE        = 13

FORCE_TEST       = True   # принудительный сигнал

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def home():
    return f"Scalp Bot | {datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC"

def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.error("TELEGRAM_TOKEN или CHAT_ID не заданы")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
        if r.status_code == 200:
            log.info("Telegram ✅")
        else:
            log.error(f"Telegram ошибка: {r.text}")
    except Exception as e:
        log.error(f"Telegram: {e}")

# ══════════════════════════════════════════════
# BYBIT V5 API
# ══════════════════════════════════════════════
RECV_WINDOW = "5000"

def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

def bybit_post(endpoint: str, body: dict) -> dict:
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        log.error("Bybit ключи не заданы!")
        return {}
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body)
    sign_str = ts + BYBIT_API_KEY + RECV_WINDOW + body_str
    signature = _sign(BYBIT_API_SECRET, sign_str)

    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(BYBIT_BASE_URL + endpoint, headers=headers, data=body_str, timeout=10)
        data = r.json()
        log.info(f"Bybit {endpoint}: {data}")
        return data
    except Exception as e:
        log.error(f"Bybit POST {endpoint}: {e}")
        return {}

def bybit_get(endpoint: str, params: dict = None) -> dict:
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        return {}
    if params is None:
        params = {}
    ts = str(int(time.time() * 1000))
    query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sign_str = ts + BYBIT_API_KEY + RECV_WINDOW + query_str
    signature = _sign(BYBIT_API_SECRET, sign_str)

    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
    }

    try:
        r = requests.get(BYBIT_BASE_URL + endpoint, headers=headers, params=params, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"Bybit GET {endpoint}: {e}")
        return {}

def set_leverage(leverage: int = LEVERAGE) -> bool:
    body = {
        "category": "linear",
        "symbol": SYMBOL,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }
    r = bybit_post("/v5/position/set-leverage", body)
    code = r.get("retCode", -1)
    if code == 0:
        log.info(f"Плечо x{leverage} установлено ✅")
        return True
    elif code == 110043:
        log.info(f"Плечо уже x{leverage} — ОК")
        return True
    else:
        log.error(f"set_leverage ошибка: {r.get('retMsg')} (код {code})")
        return False

def switch_to_one_way_mode() -> bool:
    body = {
        "category": "linear",
        "symbol": SYMBOL,
        "mode": 0,
    }
    r = bybit_post("/v5/position/switch-mode", body)
    code = r.get("retCode", -1)
    if code in (0, 110025):
        log.info("One-way mode ✅")
        return True
    else:
        log.warning(f"switch_mode: {r.get('retMsg')} ({code}) — продолжаем")
        return True

def get_open_positions() -> list:
    r = bybit_get("/v5/position/list", {"category": "linear", "symbol": SYMBOL})
    positions = r.get("result", {}).get("list", [])
    return [p for p in positions if float(p.get("size", 0)) > 0]

def get_balance() -> float:
    r = bybit_get("/v5/account/wallet-balance", {"accountType": "CONTRACT"})
    try:
        coins = r["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                return float(c["availableToWithdraw"])
    except:
        pass
    return 0.0

# ══════════════════════════════════════════════
# УПРОЩЁННАЯ ФУНКЦИЯ ОТПРАВКИ ОРДЕРА (БЕЗ TP/SL)
# ══════════════════════════════════════════════
def open_simple_order(side: str, qty: float) -> dict:
    """Открывает простой рыночный ордер без стоп-лосса и тейк-профита"""
    set_leverage()
    switch_to_one_way_mode()
    body = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "positionIdx": 0,
    }
    r = bybit_post("/v5/order/create", body)
    return r

# ══════════════════════════════════════════════
# РЫНОЧНЫЕ ДАННЫЕ
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
        log.error(f"get_klines: {e}")
        return None

def get_funding_rate() -> float:
    try:
        d = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL}", timeout=8).json()
        return float(d["lastFundingRate"])
    except:
        return 0.0

def get_orderbook_imbalance() -> float:
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL}&limit=20", timeout=8).json()
        bids = sum(float(b[1]) for b in d["bids"])
        asks = sum(float(a[1]) for a in d["asks"])
        tot = bids + asks
        return round((bids - asks) / tot * 100, 1) if tot else 0.0
    except:
        return 0.0

def calc_indicators(df):
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    hl = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"] - df["close"].shift()).abs()
    df["ATR"] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1).ewm(com=13, adjust=False).mean()

    df["VWAP"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

    bm = df["close"].rolling(20).mean()
    bs = df["close"].rolling(20).std()
    df["BB_upper"] = bm + 2 * bs
    df["BB_lower"] = bm - 2 * bs
    df["BB_width"] = (df["BB_upper"] - df["BB_lower"]) / bm

    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > df["vol_avg"] * 1.5

    df["momentum"] = df["close"] - df["close"].shift(4)
    df["buy_ratio"] = df["taker_buy_base"] / df["volume"].replace(0, np.nan)

    return df

def get_scalp_signal(df, funding, ob, force_test=False):
    row = df.iloc[-1]
    prev = df.iloc[-2]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]

    if row["BB_width"] < 0.008 and not force_test:
        return None, None, None, None, 0, "Флэт"

    long_s = short_s = 0

    if row["EMA9"] > row["EMA21"] > row["EMA50"]:
        long_s += 2
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]:
        short_s += 2

    if 28 < rsi < 45:
        long_s += 2
    elif 55 < rsi < 72:
        short_s += 2

    if price > row["VWAP"]:
        long_s += 1
    else:
        short_s += 1

    if price <= row["BB_lower"]:
        long_s += 2
    elif price >= row["BB_upper"]:
        short_s += 2

    if row["vol_spike"]:
        long_s += 1
        short_s += 1

    if funding < -0.001:
        long_s += 1
    elif funding > 0.003:
        short_s += 1

    if ob > 8:
        long_s += 2
    elif ob < -8:
        short_s += 2

    if row["momentum"] > 0 and prev["momentum"] > 0:
        long_s += 1
    elif row["momentum"] < 0 and prev["momentum"] < 0:
        short_s += 1

    if row["buy_ratio"] > 0.55:
        long_s += 1
    elif row["buy_ratio"] < 0.45:
        short_s += 1

    if force_test:
        long_s = 10

    if long_s >= MIN_SCORE and long_s > short_s:
        direction = "LONG"
        score = long_s
        entry = price
        stop = round(entry - atr * 0.5, 2)
        tp = round(entry + atr * 1.5, 2)
    elif short_s >= MIN_SCORE and short_s > long_s:
        direction = "SHORT"
        score = short_s
        entry = price
        stop = round(entry + atr * 0.5, 2)
        tp = round(entry - atr * 1.5, 2)
    else:
        return None, None, None, None, max(long_s, short_s), "Балл ниже порога"

    reason = (f"Балл {score}/{MAX_SCORE} | RSI {rsi:.0f} | "
              f"OB {ob:+.1f}% | Fund {funding:.5f} | "
              f"ATR {atr:.2f}")
    return direction, entry, stop, tp, score, reason

def score_bar(score, max_score=MAX_SCORE) -> str:
    pct = score / max_score
    filled = round(pct * 10)
    bar = "█" * filled + "░" * (10 - filled)
    emoji = "🟢" if pct >= 0.8 else ("🟡" if pct >= 0.6 else "🔴")
    return f"{emoji} [{bar}] {score}/{max_score}"

last_trade_time = 0
COOLDOWN = 15 * 60

def run_scan():
    global last_trade_time
    df = get_klines("5m", 150)
    if df is None:
        send_telegram("❌ Ошибка загрузки свечей")
        return

    calc_indicators(df)
    funding = get_funding_rate()
    ob = get_orderbook_imbalance()

    direction, entry, stop, tp, score, reason = get_scalp_signal(
        df, funding, ob, force_test=FORCE_TEST
    )

    price = df.iloc[-1]["close"]
    log.info(f"Цена: {price:.2f} | Балл: {score} | Направление: {direction}")

    if direction is None:
        log.info(f"Нет сигнала — {reason}")
        return

    e = "🟢" if direction == "LONG" else "🔴"
    arrow = "↗️" if direction == "LONG" else "↘️"
    msg_lines = [
        f"{arrow} <b>SCALP {direction}</b> {e}",
        f"",
        f"<b>Надёжность:</b>",
        score_bar(score),
        f"",
        f"💰 Вход:  <b>{entry:.2f}</b>",
        f"🛑 Стоп:  {stop:.2f}",
        f"🎯 Тейк:  {tp:.2f}",
        f"📊 {reason}",
        f"",
    ]

    now = time.time()
    cooldown = (now - last_trade_time) > COOLDOWN
    positions = get_open_positions()
    has_pos = len(positions) > 0

    if not cooldown:
        msg_lines.append(f"⏳ Кулдаун — жду {int((COOLDOWN - (now - last_trade_time)) // 60)} мин")
    elif has_pos:
        msg_lines.append(f"⚠️ Уже есть открытая позиция — пропускаю")
    elif not BYBIT_API_KEY:
        msg_lines.append(f"⚠️ BYBIT_API_KEY не задан — ордер не отправлен")
    else:
        bybit_side = "Buy" if direction == "LONG" else "Sell"
        # Отправляем простой ордер без стоп-лосса и тейк-профита
        result = open_simple_order(bybit_side, QTY)
        code = result.get("retCode", -1)

        if code == 0:
            order_id = result.get("result", {}).get("orderId", "—")
            last_trade_time = now
            msg_lines += [
                f"✅ <b>ИСПОЛНЕНО НА BYBIT</b>",
                f"   OrderID: {order_id}",
                f"   Кол-во: {QTY} ETH",
            ]
            log.info(f"✅ Ордер открыт: {direction} {QTY} ETH | ID: {order_id}")
        else:
            err = result.get("retMsg", "неизвестно")
            msg_lines += [
                f"❌ Ошибка Bybit (код {code}):",
                f"   {err}",
            ]
            log.error(f"Bybit ошибка {code}: {err}")

    msg_lines.append(f"\n⏰ {datetime.utcnow().strftime('%d.%m.%Y %H:%M:%S')} UTC")
    send_telegram("\n".join(msg_lines))

def bot_loop():
    log.info(f"🚀 Бот запущен | {SYMBOL} | скан каждые {SCAN_INTERVAL // 60} мин")
    balance = get_balance()
    send_telegram(
        f"🚀 <b>Scalp Bot запущен</b>\n\n"
        f"Биржа:   Bybit {'TESTNET' if 'testnet' in BYBIT_BASE_URL else 'MAINNET'}\n"
        f"Символ:  {SYMBOL}\n"
        f"Плечо:   x{LEVERAGE}\n"
        f"Объём:   {QTY} ETH/сделка\n"
        f"Баланс:  {balance:.2f} USDT\n"
        f"Мин балл:{MIN_SCORE}/{MAX_SCORE}\n"
        f"Скан:    каждые {SCAN_INTERVAL // 60} мин\n\n"
        f"⚠️ Бот торгует автоматически!"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Ошибка цикла: {e}")
            send_telegram(f"⚠️ Ошибка: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
