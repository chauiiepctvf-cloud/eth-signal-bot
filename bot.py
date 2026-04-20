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
LEVERAGE = 20
ORDER_USDT = 20
MIN_SCORE = 7
MAX_SCORE = 13
SCAN_INTERVAL = 5 * 60
COOLDOWN = 15 * 60
HEARTBEAT_INTERVAL = 60 * 60  # раз в час

# Фильтр сессий (UTC)
SESSION_ASIAN_START = 0      # 00:00 UTC — азиатская сессия (вялая)
SESSION_ASIAN_END = 7        # 07:00 UTC
SESSION_LONDON_START = 7     # 07:00 UTC — лондонская (активная)
SESSION_NY_START = 13        # 13:00 UTC — американская (самая активная)
SESSION_NY_END = 21          # 21:00 UTC

# ATR фильтры
ATR_MIN_PCT = 0.002          # минимум 0.2% от цены — иначе рынок спит
ATR_MAX_PCT = 0.02           # максимум 2% — иначе данные кривые

# Счётчик потерь подряд — защита от плохого рынка
MAX_LOSSES_IN_ROW = 3
PAUSE_AFTER_LOSSES = 120 * 60  # пауза 2 часа

# Корреляция BTC — не входим в лонг ETH если BTC падает
BTC_SYMBOL = "BTCUSDT"

# ══ ТЕСТОВЫЙ РЕЖИМ ══════════════════════════
FORCE_TEST = False
# ════════════════════════════════════════════

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def home():
    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"
    return f"OKX Scalp Bot [{mode}] | {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"

# ══════════════════════════════════════════════
# СОСТОЯНИЕ БОТА
# ══════════════════════════════════════════════
last_trade_time = 0
last_heartbeat_time = 0
losses_in_row = 0
pause_until = 0
ob_history = []  # история стакана для динамики

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.error("TG токены не заданы")
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
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _sign(ts: str, method: str, path: str, body: str = "") -> dict:
    msg = ts + method.upper() + path + body
    sig = base64.b64encode(
        hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    h = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
    }
    h.update(OKX_DEMO_HEADER)
    return h

def okx_get(path: str, params: dict = None) -> dict:
    qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    hdr = _sign(_ts(), "GET", path + qs)
    try:
        return requests.get(OKX_BASE + path + qs, headers=hdr, timeout=10).json()
    except Exception as e:
        log.error(f"OKX GET {path}: {e}")
        return {}

def okx_post(path: str, body: dict) -> dict:
    data = json.dumps(body)
    hdr = _sign(_ts(), "POST", path, data)
    try:
        r = requests.post(OKX_BASE + path, headers=hdr, data=data, timeout=10)
        d = r.json()
        log.info(f"OKX {path} → code:{d.get('code')} msg:{d.get('msg')}")
        return d
    except Exception as e:
        log.error(f"OKX POST {path}: {e}")
        return {}

# ══════════════════════════════════════════════
# OKX — ТОРГОВЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════
def okx_set_leverage():
    okx_post("/api/v5/account/set-leverage", {
        "instId": SYMBOL,
        "lever": str(LEVERAGE),
        "mgnMode": "cross"
    })

def okx_get_balance() -> float:
    try:
        r = okx_get("/api/v5/account/balance", {"ccy": "USDT"})
        for d in r["data"][0]["details"]:
            if d["ccy"] == "USDT":
                return float(d["availBal"])
    except:
        pass
    return 0.0

def okx_get_positions() -> list:
    r = okx_get("/api/v5/account/positions", {"instId": SYMBOL})
    return [p for p in r.get("data", []) if float(p.get("pos", 0)) != 0]

def okx_place_order(direction: str, entry: float, sl: float, tp: float) -> dict:
    okx_set_leverage()
    side = "buy" if direction == "LONG" else "sell"
    pos_side = "long" if direction == "LONG" else "short"
    cls_side = "sell" if direction == "LONG" else "buy"

    qty = max(1, round(ORDER_USDT * LEVERAGE / entry / 0.01))
    log.info(f"Открываем: {direction} | qty:{qty} | entry:{entry:.2f} sl:{sl:.2f} tp:{tp:.2f}")

    # Шаг 1: рыночный ордер
    open_r = okx_post("/api/v5/trade/order", {
        "instId": SYMBOL,
        "tdMode": "cross",
        "side": side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(qty),
    })
    if open_r.get("code") != "0":
        errmsg = open_r.get("msg", "")
        if open_r.get("data"):
            errmsg = open_r["data"][0].get("sMsg", errmsg)
        return {"ok": False, "step": "open", "msg": errmsg}

    order_id = open_r["data"][0].get("ordId", "—")
    log.info(f"✅ Позиция открыта ordId:{order_id}")

    time.sleep(2)

    # Шаг 2: OCO (TP + SL одним запросом)
    algo_r = okx_post("/api/v5/trade/order-algo", {
        "instId": SYMBOL,
        "tdMode": "cross",
        "side": cls_side,
        "posSide": pos_side,
        "ordType": "oco",
        "sz": str(qty),
        "tpTriggerPx": str(round(tp, 2)),
        "tpOrdPx": "-1",
        "tpTriggerPxType": "last",
        "slTriggerPx": str(round(sl, 2)),
        "slOrdPx": "-1",
        "slTriggerPxType": "last",
    })
    algo_ok = algo_r.get("code") == "0"

    if algo_ok:
        algo_id = algo_r["data"][0].get("algoId", "—")
        log.info(f"✅ OCO выставлен algoId:{algo_id} | TP:{tp:.2f} SL:{sl:.2f}")
    else:
        errmsg = algo_r.get("msg", "")
        if algo_r.get("data"):
            errmsg = algo_r["data"][0].get("sMsg", errmsg)
        log.error(f"OCO ошибка: {errmsg} — пробую раздельно...")

        # Фолбэк: SL отдельно
        sl_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL,
            "tdMode": "cross",
            "side": cls_side,
            "posSide": pos_side,
            "ordType": "conditional",
            "sz": str(qty),
            "slTriggerPx": str(round(sl, 2)),
            "slOrdPx": "-1",
            "slTriggerPxType": "last",
        })
        sl_ok = sl_r.get("code") == "0"
        log.info(f"SL раздельно: {'✅' if sl_ok else '❌'} {sl_r.get('msg', '')}")

        # Фолбэк: TP отдельно
        tp_r = okx_post("/api/v5/trade/order-algo", {
            "instId": SYMBOL,
            "tdMode": "cross",
            "side": cls_side,
            "posSide": pos_side,
            "ordType": "conditional",
            "sz": str(qty),
            "tpTriggerPx": str(round(tp, 2)),
            "tpOrdPx": "-1",
            "tpTriggerPxType": "last",
        })
        tp_ok = tp_r.get("code") == "0"
        log.info(f"TP раздельно: {'✅' if tp_ok else '❌'} {tp_r.get('msg', '')}")

        algo_ok = sl_ok or tp_ok

    return {
        "ok": True,
        "orderId": order_id,
        "qty": qty,
        "algo_ok": algo_ok,
    }

# ══════════════════════════════════════════════
# ДАННЫЕ
# ══════════════════════════════════════════════
def get_klines(symbol=SYMBOL_BN, interval="5m", limit=150):
    try:
        data = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
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
        log.error(f"klines {symbol} {interval}: {e}")
        return None

def get_funding() -> float:
    try:
        return float(requests.get(
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL_BN}",
            timeout=8
        ).json()["lastFundingRate"])
    except:
        return 0.0

def get_ob() -> float:
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

def get_btc_trend() -> str:
    """Тренд BTC за последние 3 свечи 5m — для корреляции."""
    try:
        df = get_klines(BTC_SYMBOL, "5m", 5)
        if df is None:
            return "NEUTRAL"
        last = df.iloc[-1]["close"]
        prev3 = df.iloc[-3]["close"]
        chg = (last - prev3) / prev3 * 100
        if chg > 0.15:
            return "UP"
        if chg < -0.15:
            return "DOWN"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

# ══════════════════════════════════════════════
# ФИЛЬТР СЕССИЙ
# ══════════════════════════════════════════════
def get_session_info() -> dict:
    """
    Возвращает текущую сессию и модификатор MIN_SCORE.
    Азиатская — поднимаем порог (меньше сигналов).
    Американская — опускаем порог (больше сигналов).
    """
    hour = datetime.now(timezone.utc).hour
    if SESSION_ASIAN_START <= hour < SESSION_ASIAN_END:
        return {"name": "🇯🇵 Азиатская", "score_mod": +2}   # требуем балл выше
    elif SESSION_NY_START <= hour < SESSION_NY_END:
        return {"name": "🇺🇸 Американская", "score_mod": -1} # чуть мягче
    elif SESSION_LONDON_START <= hour < SESSION_NY_START:
        return {"name": "🇬🇧 Лондонская", "score_mod": 0}
    else:
        return {"name": "🌙 Закрытие", "score_mod": +1}

# ══════════════════════════════════════════════
# ИНДИКАТОРЫ
# ══════════════════════════════════════════════
def calc(df: pd.DataFrame) -> pd.DataFrame:
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()

    d = df["close"].diff()
    gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
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
# СИГНАЛ
# ══════════════════════════════════════════════
def get_signal(df, funding, ob, session_mod=0, btc_trend="NEUTRAL"):
    global ob_history
    row = df.iloc[-1]
    prev = df.iloc[-2]
    price = row["close"]
    rsi = row["RSI"]
    atr = row["ATR"]

    # Тестовый режим
    if FORCE_TEST:
        direction = "LONG"
        score = 10
        if atr > price * ATR_MAX_PCT or atr <= 0:
            atr = price * 0.005
        entry = price
        sl = round(entry - atr * 1.2, 2)
        tp = round(entry + atr * 2.5, 2)
        return direction, entry, sl, tp, score, f"🧪 ТЕСТ | ATR:{atr:.2f} | Цена:{price:.2f}"

    # Фильтр флэта (ужесточённый)
    if row["BB_width"] < 0.008 and atr < price * 0.003:
        return None, None, None, None, 0, "Флэт (BB_width мал, ATR низкий)"

    # Минимальная волатильность для входа
    if atr < price * 0.003:
        return None, None, None, None, 0, f"ATR слишком мал ({atr:.2f}) — нет движения"

    # Динамика стакана
    ob_history.append(ob)
    if len(ob_history) > 5:
        ob_history.pop(0)
    ob_trend = "RISING" if len(ob_history) >= 3 and ob_history[-1] > ob_history[-3] else \
               "FALLING" if len(ob_history) >= 3 and ob_history[-1] < ob_history[-3] else \
               "FLAT"

    L = S = 0

    if row["EMA9"] > row["EMA21"] > row["EMA50"]:
        L += 2
    elif row["EMA9"] < row["EMA21"] < row["EMA50"]:
        S += 2

    if 28 < rsi < 45:
        L += 2
    elif 55 < rsi < 72:
        S += 2

    if price > row["VWAP"]:
        L += 1
    else:
        S += 1

    if price <= row["BB_lower"]:
        L += 2
    elif price >= row["BB_upper"]:
        S += 2

    if row["vol_spike"]:
        L += 1
        S += 1

    if funding < -0.001:
        L += 1
    elif funding > 0.003:
        S += 1

    if ob > 8:
        L += 2
    elif ob < -8:
        S += 2

    # Динамика стакана — усиливаем или ослабляем
    if ob_trend == "RISING":
        L += 1
    if ob_trend == "FALLING":
        S += 1

    if row["momentum"] > 0 and prev["momentum"] > 0:
        L += 1
    elif row["momentum"] < 0 and prev["momentum"] < 0:
        S += 1

    if row["buy_ratio"] > 0.55:
        L += 1
    elif row["buy_ratio"] < 0.45:
        S += 1

    # Корреляция BTC
    if btc_trend == "DOWN":
        L -= 2   # BTC падает — не входим в лонг ETH
    elif btc_trend == "UP":
        S -= 2   # BTC растёт — не входим в шорт ETH

    # Применяем модификатор сессии
    effective_min = MIN_SCORE + session_mod

    if L >= effective_min and L > S:
        direction, score = "LONG", L
    elif S >= effective_min and S > L:
        direction, score = "SHORT", S
    else:
        return None, None, None, None, max(L, S), \
               f"Балл ниже порога (L:{L} S:{S} порог:{effective_min})"

    # Защита ATR
    if atr > price * ATR_MAX_PCT or atr <= 0:
        atr = price * 0.005

    entry = price
    if direction == "LONG":
        sl = round(entry - atr * 1.2, 2)
        tp = round(entry + atr * 2.5, 2)
        if tp <= entry:
            tp = round(entry * 1.008, 2)
        if sl >= entry:
            sl = round(entry * 0.992, 2)
    else:
        sl = round(entry + atr * 1.2, 2)
        tp = round(entry - atr * 2.5, 2)
        if tp >= entry:
            tp = round(entry * 0.992, 2)
        if sl <= entry:
            sl = round(entry * 1.008, 2)

    log.info(f"{direction} | вход:{entry:.2f} sl:{sl:.2f} tp:{tp:.2f} atr:{atr:.2f}")

    reason = (f"Балл {score}/{MAX_SCORE} | RSI {rsi:.0f} | "
              f"OB {ob:+.1f}%({ob_trend}) | Fund {funding:.5f} | "
              f"ATR {atr:.2f} | BTC:{btc_trend}")
    return direction, entry, sl, tp, score, reason

# ══════════════════════════════════════════════
# ШКАЛА БАЛЛОВ
# ══════════════════════════════════════════════
def score_bar(score) -> str:
    filled = round(score / MAX_SCORE * 10)
    bar = "█" * filled + "░" * (10 - filled)
    if score / MAX_SCORE >= 0.77:
        emoji = "🟢"
    elif score / MAX_SCORE >= 0.6:
        emoji = "🟡"
    else:
        emoji = "🔴"
    return f"{emoji} [{bar}] {score}/{MAX_SCORE}"

# ══════════════════════════════════════════════
# ГЛАВНЫЙ ЦИКЛ
# ══════════════════════════════════════════════
def run_scan():
    global last_trade_time, last_heartbeat_time, losses_in_row, pause_until
    current_time = time.time()

    # Пауза после серии потерь
    if current_time < pause_until:
        remaining = int((pause_until - current_time) / 60)
        log.info(f"⏸️ Пауза после {MAX_LOSSES_IN_ROW} потерь — осталось {remaining} мин")
        return

    df = get_klines(SYMBOL_BN, "5m", 150)
    if df is None:
        send_telegram("❌ Ошибка свечей")
        return

    calc(df)
    funding = get_funding()
    ob = get_ob()
    btc_trend = get_btc_trend()
    session = get_session_info()
    price = df.iloc[-1]["close"]

    # Heartbeat раз в час
    if current_time - last_heartbeat_time >= HEARTBEAT_INTERVAL:
        last_heartbeat_time = current_time
        balance = okx_get_balance()
        positions = okx_get_positions()
        pos_info = f"Открытых позиций: {len(positions)}" if positions else "Позиций нет"
        send_telegram(
            f"❤️ <b>Heartbeat</b>\n\n"
            f"Режим: {'🧪 ТЕСТ' if FORCE_TEST else '⚔️ БОЕВОЙ'}\n"
            f"💰 Цена ETH: <b>{price:.2f}</b>\n"
            f"💳 Баланс: {balance:.2f} USDT\n"
            f"🌍 Сессия: {session['name']}\n"
            f"₿ BTC тренд: {btc_trend}\n"
            f"📊 {pos_info}\n"
            f"📉 Потерь подряд: {losses_in_row}/{MAX_LOSSES_IN_ROW}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
        )

    direction, entry, sl, tp, score, reason = get_signal(
        df, funding, ob, session["score_mod"], btc_trend
    )

    log.info(f"Цена:{price:.2f} | {direction or 'нет'} | балл:{score}")

    if direction is None:
        log.info(f"Нет сигнала: {reason}")
        return

    e = "🟢" if direction == "LONG" else "🔴"
    arrow = "↗️" if direction == "LONG" else "↘️"
    mode = "🧪 ТЕСТ" if FORCE_TEST else "⚔️ БОЕВОЙ"

    msg = [
        f"<b>[{mode}] {session['name']}</b>",
        f"{arrow} <b>SCALP {direction}</b> {e}",
        f"",
        f"<b>Надёжность:</b> {score_bar(score)}",
        f"",
        f"💰 Вход: <b>{entry:.2f}</b>",
        f"🛑 Стоп: {sl:.2f} (-{abs(sl - entry):.2f}$)",
        f"🎯 Тейк: {tp:.2f} (+{abs(tp - entry):.2f}$)",
        f"📊 R/R: {abs(tp - entry) / max(abs(sl - entry), 0.01):.1f}",
        f"₿ BTC: {btc_trend}",
        f"",
        f"📊 {reason}",
        f"",
    ]

    now = current_time
    cooldown = (now - last_trade_time) > COOLDOWN
    has_pos = len(okx_get_positions()) > 0

    if not OKX_API_KEY:
        msg.append("⚠️ OKX_API_KEY не задан")
    elif not cooldown:
        left = int((COOLDOWN - (now - last_trade_time)) // 60)
        msg.append(f"⏳ Кулдаун — осталось {left} мин")
    elif has_pos:
        msg.append(f"⚠️ Позиция уже открыта — пропускаю")
    else:
        res = okx_place_order(direction, entry, sl, tp)
        if res["ok"]:
            last_trade_time = now
            losses_in_row = 0
            algo_s = "✅" if res["algo_ok"] else "⚠️ частично"
            msg += [
                f"✅ <b>ИСПОЛНЕНО НА OKX DEMO</b>",
                f"📦 Контрактов: {res['qty']}",
                f"⚙️ TP + SL: {algo_s}",
                f"🆔 OrderID: {res['orderId']}",
            ]
        else:
            losses_in_row += 1
            if losses_in_row >= MAX_LOSSES_IN_ROW:
                pause_until = now + PAUSE_AFTER_LOSSES
                msg.append(f"⏸️ {MAX_LOSSES_IN_ROW} ошибки подряд — пауза {PAUSE_AFTER_LOSSES // 60} мин")
            msg += [
                f"❌ Ошибка OKX [{res['step']}]:",
                f"📝 {res['msg']}",
            ]

    msg.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')} UTC")
    send_telegram("\n".join(msg))

def bot_loop():
    log.info(f"🚀 Старт | FORCE_TEST={FORCE_TEST}")
    balance = okx_get_balance()
    send_telegram(
        f"🚀 <b>OKX Scalp Bot</b>\n\n"
        f"🎭 Режим: {'🧪 ТЕСТ' if FORCE_TEST else '⚔️ БОЕВОЙ'}\n"
        f"📊 Символ: {SYMBOL}\n"
        f"⚙️ Плечо: x{LEVERAGE}\n"
        f"💰 Сделка: {ORDER_USDT}$ USDT\n"
        f"💳 Баланс: {balance:.2f} USDT\n"
        f"🎯 Мин балл: {MIN_SCORE}/{MAX_SCORE}\n"
        f"⏱️ Скан: каждые {SCAN_INTERVAL // 60} мин\n\n"
        f"🛡️ <b>Новые защиты:</b>\n"
        f"• Фильтр сессий (азиат/лондон/NY)\n"
        f"• Корреляция BTC\n"
        f"• ATR фильтр снизу\n"
        f"• Динамика стакана\n"
        f"• Пауза после {MAX_LOSSES_IN_ROW} ошибок\n"
        f"• Heartbeat раз в час"
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
