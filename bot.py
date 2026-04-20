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
OKX_DEMO_HEADER = {"x-simulated-trading": "1"}  # убери для реала
SYMBOL = "ETH-USDT-SWAP"
SYMBOL_BN = "ETHUSDT"
BTC_SYMBOL = "BTCUSDT"
LEVERAGE = 20
ORDER_USDT = 20
SCAN_INTERVAL = 3 * 60          # скан каждые 3 минуты
COOLDOWN = 10 * 60              # 10 мин между сделками
HEARTBEAT_INTERVAL = 60 * 60    # раз в час

# ── ПАРАМЕТРЫ СИГНАЛА ──────────────────────
MIN_SCORE = 5        # низкий порог = больше сигналов
MAX_SCORE = 11       # максимум очков

# ── TP / SL (множители ATR) ────────────────
SL_MULT = 1.0        # стоп = 1 ATR
TP_MULT = 2.0        # тейк = 2 ATR (R/R = 2:1)

# ── ATR ФИЛЬТР ─────────────────────────────
ATR_MIN_PCT = 0.001  # 0.1% — убираем только мёртвый рынок
ATR_MAX_PCT = 0.05   # 5% — защита от кривых данных

# ── ЗАЩИТЫ ─────────────────────────────────
MAX_LOSSES = 4
PAUSE_LOSSES = 60 * 60   # 1 час паузы

FORCE_TEST = False

# ══════════════════════════════════════════════
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def home():
    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    return f"OKX Scalp Bot [{mode}] | {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"

# Состояние
last_trade_time = 0
last_heartbeat_time = 0
losses_in_row = 0
pause_until = 0
ob_history = []

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
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
            log.error(f"Telegram ❌: {r.text}")
    except Exception as e:
        log.error(f"Telegram: {e}")

# ══════════════════════════════════════════════
# OKX API
# ══════════════════════════════════════════════
def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    sig = base64.b64encode(
        hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
        **OKX_DEMO_HEADER
    }

def okx_get(path, params=None):
    qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    hdr = _sign(_ts(), "GET", path + qs)
    try:
        return requests.get(OKX_BASE + path + qs, headers=hdr, timeout=10).json()
    except Exception as e:
        log.error(f"OKX GET {path}: {e}")
        return {}

def okx_post(path, body):
    data = json.dumps(body)
    hdr = _sign(_ts(), "POST", path, data)
    try:
        r = requests.post(OKX_BASE + path, headers=hdr, data=data, timeout=10)
        d = r.json()
        log.info(f"OKX {path} → {d.get('code')} {d.get('msg', '')}")
        return d
    except Exception as e:
        log.error(f"OKX POST {path}: {e}")
        return {}

def okx_set_leverage():
    okx_post("/api/v5/account/set-leverage", {
        "instId": SYMBOL,
        "lever": str(LEVERAGE),
        "mgnMode": "cross"
    })

def okx_get_balance():
    try:
        r = okx_get("/api/v5/account/balance", {"ccy": "USDT"})
        for d in r["data"][0]["details"]:
            if d["ccy"] == "USDT":
                return float(d["availBal"])
    except:
        pass
    return 0.0

def okx_get_positions():
    r = okx_get("/api/v5/account/positions", {"instId": SYMBOL})
    return [p for p in r.get("data", []) if float(p.get("pos", 0)) != 0]

def okx_place_order(direction, entry, sl, tp):
    okx_set_leverage()
    side = "buy" if direction == "LONG" else "sell"
    pos_side = "long" if direction == "LONG" else "short"
    cls_side = "sell" if direction == "LONG" else "buy"
    qty = max(1, round(ORDER_USDT * LEVERAGE / entry / 0.01))
    log.info(f"▶ {direction} qty:{qty} entry:{entry:.2f} sl:{sl:.2f} tp:{tp:.2f}")

    # Шаг 1: открываем позицию
    r = okx_post("/api/v5/trade/order", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": side, "posSide": pos_side,
        "ordType": "market", "sz": str(qty),
    })
    if r.get("code") != "0":
        msg = r.get("msg", "")
        if r.get("data"):
            msg = r["data"][0].get("sMsg", msg)
        return {"ok": False, "step": "open", "msg": msg}
    order_id = r["data"][0].get("ordId", "—")
    log.info(f"✅ Открыта ordId:{order_id}")
    time.sleep(2)

    # Шаг 2: OCO (TP + SL одним запросом)
    algo = okx_post("/api/v5/trade/order-algo", {
        "instId": SYMBOL, "tdMode": "cross",
        "side": cls_side, "posSide": pos_side,
        "ordType": "oco", "sz": str(qty),
        "tpTriggerPx": str(round(tp, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last",
        "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1", "slTriggerPxType": "last",
    })
    algo_ok = algo.get("code") == "0"
    if not algo_ok:
        log.warning("OCO не прошёл — пробую раздельно")
        sl_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL, "tdMode": "cross",
            "side": cls_side, "posSide": pos_side,
            "ordType": "conditional", "sz": str(qty),
            "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1", "slTriggerPxType": "last",
        })
        tp_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL, "tdMode": "cross",
            "side": cls_side, "posSide": pos_side,
            "ordType": "conditional", "sz": str(qty),
            "tpTriggerPx": str(round(tp, 2)), "tpOrdPx": "-1", "tpTriggerPxType": "last",
        })
        algo_ok = sl_r.get("code") == "0" or tp_r.get("code") == "0"
    return {"ok": True, "orderId": order_id, "qty": qty, "algo_ok": algo_ok}

# ══════════════════════════════════════════════
# ДАННЫЕ
# ══════════════════════════════════════════════
def get_klines(symbol=None, interval="5m", limit=150):
    sym = symbol or SYMBOL_BN
    try:
        data = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}",
            timeout=10
        ).json()
        df = pd.DataFrame(data, columns=[
            "time", "open", "high", "low", "close", "volume",
            "ct", "qv", "trades", "taker_buy_base", "tbq", "ignore"
        ])
        for c in ("open", "high", "low", "close", "volume", "taker_buy_base"):
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        log.error(f"klines {sym} {interval}: {e}")
        return None

def get_funding():
    try:
        return float(requests.get(
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}",
            timeout=8
        ).json()["lastFundingRate"])
    except:
        return 0.0

def get_ob():
    try:
        d = requests.get(f"https://api.binance.com/api/v3/depth?symbol={SYMBOL_BN}&limit=20", timeout=8).json()
        bids = sum(float(b[1]) for b in d["bids"])
        asks = sum(float(a[1]) for a in d["asks"])
        tot = bids + asks
        return round((bids - asks) / tot * 100, 1) if tot else 0.0
    except:
        return 0.0

def get_btc_momentum():
    """Изменение BTC за последние 3 свечи 3m."""
    try:
        df = get_klines(BTC_SYMBOL, "3m", 5)
        if df is None:
            return 0.0
        chg = (df.iloc[-1]["close"] - df.iloc[-3]["close"]) / df.iloc[-3]["close"] * 100
        return round(chg, 3)
    except:
        return 0.0

# ══════════════════════════════════════════════
# ИНДИКАТОРЫ
# ══════════════════════════════════════════════
def calc(df):
    # EMA
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()

    # RSI
    d = df["close"].diff()
    gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_sig"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_bull"] = (df["MACD"] > df["MACD_sig"]) & (df["MACD"].shift() <= df["MACD_sig"].shift())
    df["MACD_bear"] = (df["MACD"] < df["MACD_sig"]) & (df["MACD"].shift() >= df["MACD_sig"].shift())

    # ATR
    hl = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"] - df["close"].shift()).abs()
    df["ATR"] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1).ewm(com=13, adjust=False).mean()

    # Bollinger
    bm = df["close"].rolling(20).mean()
    bs = df["close"].rolling(20).std()
    df["BB_up"] = bm + 2 * bs
    df["BB_dn"] = bm - 2 * bs
    df["BB_w"] = (df["BB_up"] - df["BB_dn"]) / bm
    df["BB_pct"] = (df["close"] - df["BB_dn"]) / (df["BB_up"] - df["BB_dn"] + 1e-9)

    # VWAP
    df["VWAP"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

    # CVD
    df["CVD"] = (df["taker_buy_base"] - (df["volume"] - df["taker_buy_base"])).rolling(20).sum()
    df["CVD_up"] = df["CVD"] > df["CVD"].shift(3)

    # Объём
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > df["vol_ma"] * 1.3

    # Моментум и taker ratio
    df["mom"] = df["close"] - df["close"].shift(4)
    df["buy_ratio"] = df["taker_buy_base"] / df["volume"].replace(0, np.nan)

    # Свечные паттерны
    body = (df["close"] - df["open"]).abs()
    rng = df["high"] - df["low"] + 1e-9
    lw = df[["open", "close"]].min(axis=1) - df["low"]
    uw = df["high"] - df[["open", "close"]].max(axis=1)
    df["hammer"] = (lw > body * 2) & (uw < body * 0.5) & (df["close"] > df["open"])
    df["shooter"] = (uw > body * 2) & (lw < body * 0.5) & (df["close"] < df["open"])
    return df

# ══════════════════════════════════════════════
# СИГНАЛ — НОВАЯ ЛОГИКА
# ══════════════════════════════════════════════
def get_signal(df, funding, ob, btc_mom):
    global ob_history
    row = df.iloc[-1]
    prev = df.iloc[-2]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]

    # FORCE TEST
    if FORCE_TEST:
        atr = max(atr, price * 0.003)
        entry = price
        sl = round(entry - atr * SL_MULT, 2)
        tp = round(entry + atr * TP_MULT, 2)
        return "LONG", entry, sl, tp, 10, f"🧪 ТЕСТ | ATR:{atr:.2f}"

    # Фильтр мёртвого рынка
    if atr < price * ATR_MIN_PCT:
        return None, None, None, None, 0, f"Рынок мёртвый (ATR {atr:.2f})"
    if atr > price * ATR_MAX_PCT:
        atr = price * 0.005

    # Динамика стакана
    ob_history.append(ob)
    if len(ob_history) > 6:
        ob_history.pop(0)
    ob_rising = len(ob_history) >= 3 and ob_history[-1] > ob_history[-3] + 2
    ob_falling = len(ob_history) >= 3 and ob_history[-1] < ob_history[-3] - 2

    L = S = 0

    # ── 1. EMA тренд (вес 2) ──────────────────
    if row["EMA9"] > row["EMA21"] > row["EMA50"]:
        L += 2
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]:
        S += 2
    elif row["EMA9"] > row["EMA21"]:
        L += 1
    elif row["EMA9"] < row["EMA21"]:
        S += 1

    # ── 2. RSI (вес 2) ────────────────────────
    if rsi < 35:
        L += 2
    elif rsi < 45:
        L += 1
    elif rsi > 65:
        S += 2
    elif rsi > 55:
        S += 1

    # ── 3. MACD пересечение (вес 2) ───────────
    if row["MACD_bull"]:
        L += 2
    elif row["MACD"] > row["MACD_sig"]:
        L += 1
    if row["MACD_bear"]:
        S += 2
    elif row["MACD"] < row["MACD_sig"]:
        S += 1

    # ── 4. CVD — поток денег (вес 1) ──────────
    if row["CVD_up"]:
        L += 1
    else:
        S += 1

    # ── 5. Bollinger (вес 1) ──────────────────
    bp = row["BB_pct"]
    if bp < 0.1:
        L += 1
    elif bp > 0.9:
        S += 1

    # ── 6. VWAP (вес 1) ───────────────────────
    if price < row["VWAP"]:
        L += 1
    else:
        S += 1

    # ── 7. Стакан + динамика (вес 1) ──────────
    if ob > 5 or ob_rising:
        L += 1
    if ob < -5 or ob_falling:
        S += 1

    # ── 8. BTC моментум (вес 1) ───────────────
    if btc_mom > 0.2:
        L += 1
        S = max(0, S - 1)
    elif btc_mom < -0.2:
        S += 1
        L = max(0, L - 1)

    # ── 9. Объём (вес 1) ──────────────────────
    if row["vol_spike"]:
        if L > S:
            L += 1
        else:
            S += 1

    # ── 10. Свечной паттерн (вес 1) ───────────
    if row["hammer"]:
        L += 1
    if row["shooter"]:
        S += 1

    # ── Фандинг (небольшой вес) ────────────────
    if funding < -0.001:
        L += 1
    elif funding > 0.003:
        S += 1

    # ── Сессия — только ночью штраф ────────────
    hour = datetime.now(timezone.utc).hour
    if 1 <= hour < 6:  # 01-06 UTC — ночь
        if L >= S:
            L = max(0, L - 1)
        else:
            S = max(0, S - 1)

    # Выбор направления
    if L >= MIN_SCORE and L > S:
        direction, score = "LONG", L
    elif S >= MIN_SCORE and S > L:
        direction, score = "SHORT", S
    else:
        return None, None, None, None, max(L, S), f"Балл {max(L,S)}/{MAX_SCORE} (L:{L} S:{S} порог:{MIN_SCORE})"

    # Уровни входа
    entry = price
    if direction == "LONG":
        sl = round(entry - atr * SL_MULT, 2)
        tp = round(entry + atr * TP_MULT, 2)
        if sl >= entry:
            sl = round(entry * 0.995, 2)
        if tp <= entry:
            tp = round(entry * 1.01, 2)
    else:
        sl = round(entry + atr * SL_MULT, 2)
        tp = round(entry - atr * TP_MULT, 2)
        if sl <= entry:
            sl = round(entry * 1.005, 2)
        if tp >= entry:
            tp = round(entry * 0.99, 2)

    dist_sl = abs(entry - sl)
    dist_tp = abs(tp - entry)
    rr = dist_tp / max(dist_sl, 0.01)
    log.info(f"{direction} балл:{score} | {entry:.2f} → SL:{sl:.2f} TP:{tp:.2f} R/R:{rr:.1f}")

    reason = f"Балл {score}/{MAX_SCORE} | RSI {rsi:.0f} | OB {ob:+.1f}% | BTC {btc_mom:+.2f}% | Fund {funding:.5f} | ATR {atr:.2f}"
    return direction, entry, sl, tp, score, reason

# ══════════════════════════════════════════════
# ШКАЛА БАЛЛОВ
# ══════════════════════════════════════════════
def score_bar(score):
    filled = round(score / MAX_SCORE * 10)
    bar = "█" * filled + "░" * (10 - filled)
    if score >= 8:
        emoji = "🟢"
    elif score >= 6:
        emoji = "🟡"
    else:
        emoji = "🔴"
    return f"{emoji} [{bar}] {score}/{MAX_SCORE}"

# ══════════════════════════════════════════════
# ГЛАВНЫЙ ЦИКЛ
# ══════════════════════════════════════════════
def run_scan():
    global last_trade_time, last_heartbeat_time, losses_in_row, pause_until
    now = time.time()

    if now < pause_until:
        log.info(f"⏸️ Пауза — осталось {int((pause_until - now) / 60)} мин")
        return

    df = get_klines(SYMBOL_BN, "5m", 150)
    if df is None:
        send_telegram("❌ Ошибка свечей")
        return

    calc(df)
    funding = get_funding()
    ob = get_ob()
    btc_mom = get_btc_momentum()
    price = df.iloc[-1]["close"]
    atr_val = df.iloc[-1]["ATR"]

    # Heartbeat раз в час
    if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
        last_heartbeat_time = now
        bal = okx_get_balance()
        pos = okx_get_positions()
        hour = datetime.now(timezone.utc).hour
        if 1 <= hour < 6:
            session = "🌙 Ночь"
        elif hour < 13:
            session = "🇬🇧 Лондон"
        else:
            session = "🇺🇸 Нью-Йорк"
        send_telegram(
            f"❤️ <b>Heartbeat</b>\n\n"
            f"💰 ETH: <b>{price:.2f}</b> ATR: {atr_val:.2f}\n"
            f"₿ BTC: {btc_mom:+.2f}% OB: {ob:+.1f}%\n"
            f"🌍 Сессия: {session}\n"
            f"💳 Баланс: {bal:.2f} USDT\n"
            f"📊 Позиций: {len(pos)}\n"
            f"📉 Потерь подряд: {losses_in_row}/{MAX_LOSSES}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
        )

    direction, entry, sl, tp, score, reason = get_signal(df, funding, ob, btc_mom)
    log.info(f"ETH:{price:.2f} BTC:{btc_mom:+.2f}% OB:{ob:+.1f} | {direction or 'нет'} балл:{score}")

    if direction is None:
        log.info(f"Нет сигнала: {reason}")
        return

    e = "🟢" if direction == "LONG" else "🔴"
    arrow = "↗️" if direction == "LONG" else "↘️"
    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    dist_sl = abs(entry - sl)
    dist_tp = abs(tp - entry)
    rr = dist_tp / max(dist_sl, 0.01)
    msg = [
        f"<b>[{mode}]</b>",
        f"{arrow} <b>SCALP {direction}</b> {e}",
        f"",
        f"<b>Надёжность:</b> {score_bar(score)}",
        f"",
        f"💰 Вход: <b>{entry:.2f}</b>",
        f"🛑 Стоп: {sl:.2f} (-{dist_sl:.2f}$)",
        f"🎯 Тейк: {tp:.2f} (+{dist_tp:.2f}$)",
        f"📊 R/R: {rr:.1f}",
        f"₿ BTC: {btc_mom:+.2f}%",
        f"",
        f"📊 {reason}",
        f"",
    ]

    cooldown_ok = (now - last_trade_time) > COOLDOWN
    has_pos = len(okx_get_positions()) > 0

    if not OKX_API_KEY:
        msg.append("⚠️ OKX_API_KEY не задан")
    elif not cooldown_ok:
        left = int((COOLDOWN - (now - last_trade_time)) // 60)
        msg.append(f"⏳ Кулдаун — {left} мин")
    elif has_pos:
        msg.append("⚠️ Позиция уже открыта")
    else:
        res = okx_place_order(direction, entry, sl, tp)
        if res["ok"]:
            last_trade_time = now
            losses_in_row = 0
            algo_s = "✅" if res["algo_ok"] else "⚠️ частично"
            msg += [
                f"✅ <b>ИСПОЛНЕНО НА OKX DEMO</b>",
                f"📦 Контрактов: {res['qty']}",
                f"⚙️ TP+SL: {algo_s}",
                f"🆔 OrderID: {res['orderId']}",
            ]
        else:
            losses_in_row += 1
            if losses_in_row >= MAX_LOSSES:
                pause_until = now + PAUSE_LOSSES
                msg.append(f"⏸️ {MAX_LOSSES} ошибки — пауза {PAUSE_LOSSES // 60} мин")
            msg += [f"❌ Ошибка [{res['step']}]: {res['msg']}"]

    msg.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')} UTC")
    send_telegram("\n".join(msg))

def bot_loop():
    log.info("🚀 Старт")
    bal = okx_get_balance()
    send_telegram(
        f"🚀 <b>OKX Scalp Bot</b>\n\n"
        f"🎭 Режим: {'🧪 ТЕСТ' if FORCE_TEST else '⚔️ БОЕВОЙ'}\n"
        f"📊 Символ: {SYMBOL}\n"
        f"⚙️ Плечо: x{LEVERAGE}\n"
        f"💰 Сделка: {ORDER_USDT}$ USDT\n"
        f"💳 Баланс: {bal:.2f} USDT\n"
        f"🎯 Мин балл: {MIN_SCORE}/{MAX_SCORE}\n"
        f"📐 TP/SL: {TP_MULT}/{SL_MULT} × ATR (R/R={TP_MULT/SL_MULT:.0f}:1)\n"
        f"⏱️ Скан: каждые {SCAN_INTERVAL // 60} мин\n"
        f"⏳ Кулдаун: {COOLDOWN // 60} мин"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Ошибка: {e}")
            send_telegram(f"❌ Ошибка: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
