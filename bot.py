import os
import time
import logging
import threading
import hashlib
import hmac
import base64
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from flask import Flask

# ══════════════════════════════════════════════
# НАСТРОЙКИ
# ══════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_SECRET = os.environ.get("OKX_SECRET")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
OKX_BASE = "https://www.okx.com"
OKX_DEMO_HEADER = {"x-simulated-trading": "1"}
SYMBOL = "ETH-USDT-SWAP"
SYMBOL_BN = "ETHUSDT"
LEVERAGE = 5
ORDER_USDT = 20
MIN_SCORE = 7
MAX_SCORE = 13
SCAN_INTERVAL = 5 * 60
COOLDOWN = 15 * 60

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def home():
    return f"OKX Scalp Bot | {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"

def send_telegram(text: str):
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
            log.error(f"Telegram: {r.text}")
    except Exception as e:
        log.error(f"Telegram: {e}")

# ══════════════════════════════════════════════
# OKX API — ПОДПИСЬ
# ══════════════════════════════════════════════
def _okx_sign(timestamp: str, method: str, path: str, body: str = "") -> dict:
    message = timestamp + method.upper() + path + body
    signature = base64.b64encode(
        hmac.new(
            OKX_SECRET.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).digest()
    ).decode()
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
    }
    headers.update(OKX_DEMO_HEADER)
    return headers

def okx_get(path: str, params: dict = None) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    hdr = _okx_sign(ts, "GET", path + qs)
    try:
        r = requests.get(OKX_BASE + path + qs, headers=hdr, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"OKX GET {path}: {e}")
        return {}

def okx_post(path: str, body: dict) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = json.dumps(body)
    hdr = _okx_sign(ts, "POST", path, data)
    try:
        r = requests.post(OKX_BASE + path, headers=hdr, data=data, timeout=10)
        d = r.json()
        log.info(f"OKX POST {path}: {d}")
        return d
    except Exception as e:
        log.error(f"OKX POST {path}: {e}")
        return {}

# ══════════════════════════════════════════════
# OKX — ТОРГОВЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════
def okx_set_leverage() -> bool:
    r = okx_post("/api/v5/account/set-leverage", {
        "instId": SYMBOL,
        "lever": str(LEVERAGE),
        "mgnMode": "cross",
    })
    code = r.get("code", "-1")
    if code == "0":
        log.info(f"Плечо x{LEVERAGE} установлено ✅")
        return True
    log.warning(f"set_leverage: {r.get('msg')} (код {code})")
    return True

def okx_get_balance() -> float:
    r = okx_get("/api/v5/account/balance", {"ccy": "USDT"})
    try:
        for detail in r["data"][0]["details"]:
            if detail["ccy"] == "USDT":
                return float(detail["availBal"])
    except:
        pass
    return 0.0

def okx_get_positions() -> list:
    r = okx_get("/api/v5/account/positions", {"instId": SYMBOL})
    return [p for p in r.get("data", []) if float(p.get("pos", 0)) != 0]

def okx_place_order_simple(direction: str, qty: int) -> dict:
    """Открывает простой рыночный ордер без TP/SL"""
    side = "buy" if direction == "LONG" else "sell"
    pos_side = "long" if direction == "LONG" else "short"
    body = {
        "instId": SYMBOL,
        "tdMode": "cross",
        "side": side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(qty),
    }
    r = okx_post("/api/v5/trade/order", body)
    code = r.get("code", "-1")
    if code == "0":
        order_id = r["data"][0].get("ordId", "—")
        return {"ok": True, "orderId": order_id, "qty": qty}
    else:
        msg = r.get("msg", "") or (r.get("data", [{}])[0].get("sMsg", ""))
        return {"ok": False, "code": code, "msg": msg}

def okx_place_order(direction: str, entry: float, sl: float, tp: float) -> dict:
    """Открывает ордер (без TP/SL для проверки)"""
    okx_set_leverage()
    contract_size = 0.01
    qty = max(1, round(ORDER_USDT * LEVERAGE / entry / contract_size))
    
    result = okx_place_order_simple(direction, qty)
    return result

# ══════════════════════════════════════════════
# РЫНОЧНЫЕ ДАННЫЕ
# ══════════════════════════════════════════════
def get_klines(interval="5m", limit=150):
    try:
        data = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={SYMBOL_BN}&interval={interval}&limit={limit}",
            timeout=10
        ).json()
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
        d = requests.get(
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}",
            timeout=8
        ).json()
        return float(d["lastFundingRate"])
    except:
        return 0.0

def get_orderbook_imbalance() -> float:
    try:
        d = requests.get(
            f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=20",
            timeout=8
        ).json()
        bids = sum(float(b[1]) for b in d["bids"])
        asks = sum(float(a[1]) for a in d["asks"])
        tot = bids + asks
        return round((bids - asks) / tot * 100, 1) if tot else 0.0
    except:
        return 0.0

# ══════════════════════════════════════════════
# ИНДИКАТОРЫ
# ══════════════════════════════════════════════
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
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

# ══════════════════════════════════════════════
# СИСТЕМА БАЛЛОВ
# ══════════════════════════════════════════════
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
        direction, score = "LONG", long_s
    elif short_s >= MIN_SCORE and short_s > long_s:
        direction, score = "SHORT", short_s
    else:
        return None, None, None, None, max(long_s, short_s), "Балл ниже порога"

    max_atr = price * 0.02
    if atr > max_atr or atr <= 0:
        log.warning(f"ATR некорректный ({atr:.2f}) — используем 0.5% цены")
        atr = price * 0.005

    entry = price
    if direction == "LONG":
        sl = round(entry - atr * 0.5, 2)
        tp = round(entry + atr * 1.5, 2)
        if tp <= entry:
            tp = round(entry * 1.003, 2)
        if sl >= entry:
            sl = round(entry * 0.997, 2)
    else:
        sl = round(entry + atr * 0.5, 2)
        tp = round(entry - atr * 1.5, 2)
        if tp >= entry:
            tp = round(entry * 0.997, 2)
        if sl <= entry:
            sl = round(entry * 1.003, 2)

    log.info(f"Сигнал: {direction} | Вход:{entry:.2f} SL:{sl:.2f} TP:{tp:.2f} ATR:{atr:.2f}")

    reason = (f"Балл {score}/{MAX_SCORE} | RSI {rsi:.0f} | "
              f"OB {ob:+.1f}% | Fund {funding:.5f} | ATR {atr:.2f}")
    return direction, entry, sl, tp, score, reason

def score_bar(score) -> str:
    pct = score / MAX_SCORE
    filled = round(pct * 10)
    bar = "█" * filled + "░" * (10 - filled)
    if pct >= 0.77:
        emoji = "🟢"
    elif pct >= 0.6:
        emoji = "🟡"
    else:
        emoji = "🔴"
    return f"{emoji} [{bar}] {score}/{MAX_SCORE}"

last_trade_time = 0

def run_scan(force_test=False):
    global last_trade_time
    df = get_klines("5m", 150)
    if df is None:
        send_telegram("❌ Ошибка загрузки свечей")
        return

    calc_indicators(df)
    funding = get_funding_rate()
    ob = get_orderbook_imbalance()
    price = df.iloc[-1]["close"]

    direction, entry, sl, tp, score, reason = get_scalp_signal(
        df, funding, ob, force_test=force_test
    )

    log.info(f"Цена: {price:.2f} | Балл: {score} | {direction or 'нет сигнала'}")

    if direction is None:
        log.info(f"Нет сигнала — {reason}")
        return

    e = "🟢" if direction == "LONG" else "🔴"
    arrow = "↗️" if direction == "LONG" else "↘️"
    dist_tp = abs(tp - entry)
    dist_sl = abs(sl - entry)
    msg = [
        f"{arrow} <b>SCALP {direction}</b> {e}",
        f"",
        f"<b>Надёжность:</b>",
        score_bar(score),
        f"",
        f"💰 Вход: <b>{entry:.2f}</b>",
        f"🛑 Стоп: {sl:.2f} (-{dist_sl:.2f}$)",
        f"🎯 Тейк: {tp:.2f} (+{dist_tp:.2f}$)",
        f"📊 R/R: {dist_tp / max(dist_sl, 0.01):.1f}",
        f"",
        f"📊 {reason}",
        f"",
    ]

    now = time.time()
    cooldown = (now - last_trade_time) > COOLDOWN
    positions = okx_get_positions()
    has_pos = len(positions) > 0

    if not OKX_API_KEY:
        msg.append("⚠️ OKX_API_KEY не задан — ордер не отправлен")
    elif not cooldown:
        left = int((COOLDOWN - (now - last_trade_time)) // 60)
        msg.append(f"⏳ Кулдаун — осталось {left} мин")
    elif has_pos:
        msg.append(f"⚠️ Уже есть открытая позиция — пропускаю")
    else:
        result = okx_place_order(direction, entry, sl, tp)
        if result["ok"]:
            last_trade_time = now
            msg += [
                f"✅ <b>ИСПОЛНЕНО НА OKX DEMO</b>",
                f"📦 Контрактов: {result['qty']}",
                f"🆔 OrderID: {result['orderId']}",
            ]
        else:
            msg += [
                f"❌ Ошибка OKX (код {result['code']}):",
                f"📝 {result['msg']}",
            ]

    msg.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')} UTC")
    send_telegram("\n".join(msg))

def bot_loop():
    log.info(f"🚀 OKX Scalp Bot запущен | {SYMBOL}")
    balance = okx_get_balance()
    send_telegram(
        f"🚀 <b>OKX Scalp Bot запущен</b>\n\n"
        f"🎭 Режим: DEMO (simulated)\n"
        f"📊 Символ: {SYMBOL}\n"
        f"⚙️ Плечо: x{LEVERAGE}\n"
        f"💰 Сделка: {ORDER_USDT}$ USDT\n"
        f"💳 Баланс: {balance:.2f} USDT\n"
        f"🎯 Мин балл: {MIN_SCORE}/{MAX_SCORE}\n"
        f"⏱️ Скан: каждые {SCAN_INTERVAL // 60} мин\n\n"
        f"⚠️ Бот торгует автоматически на демо-счёте"
    )

    while True:
        try:
            run_scan(force_test=True)
        except Exception as e:
            log.error(f"Ошибка: {e}")
            send_telegram(f"❌ Ошибка бота: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
