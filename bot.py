import os
import time
import logging
import threading
from datetime import datetime
import requests
import pandas as pd
import numpy as np
from flask import Flask

# ══════════════════════════════════════════════
# НАСТРОЙКИ (через переменные окружения Render)
# ══════════════════════════════════════════════
TOKEN = os.environ.get("TELEGRAM_TOKEN", "8710461065:AAEtou4YT-j283WLBX2AsouBEX9mQDXsGic")
CHAT_ID = os.environ.get("CHAT_ID", "843894335")
SYMBOL = "ETHUSDT"
SCAN_INTERVAL = 30 * 60  # каждые 30 минут

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# Flask-заглушка — Render требует открытый порт
app = Flask(__name__)

@app.route("/")
def home():
    return f"ETH Signal Bot работает | {datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC"

# ══════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════
def send(text: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
        if r.status_code == 200:
            log.info("Сообщение отправлено")
        else:
            log.error(f"Telegram ошибка: {r.text}")
    except Exception as e:
        log.error(f"Telegram: {e}")

# ══════════════════════════════════════════════
# ДАННЫЕ BINANCE
# ══════════════════════════════════════════════
def get_klines(interval="1h", limit=300):
    try:
        data = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
            timeout=15
        ).json()
        df = pd.DataFrame(data, columns=[
            "time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        for c in ("open", "high", "low", "close", "volume", "taker_buy_base", "quote_vol"):
            df[c] = df[c].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        return df.reset_index(drop=True)
    except Exception as e:
        log.error(f"Свечи {interval}: {e}")
        return None

def get_funding():
    try:
        r = {"rate": 0.0, "trend": "FLAT", "rates": []}
        cur = requests.get(
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL}",
            timeout=10
        ).json()
        r["rate"] = float(cur["lastFundingRate"])
        hist = requests.get(
            f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={SYMBOL}&limit=10",
            timeout=10
        ).json()
        rates = [float(x["fundingRate"]) for x in hist]
        r["rates"] = rates
        if len(rates) >= 3:
            if rates[-1] > rates[-3]:
                r["trend"] = "RISING"
            elif rates[-1] < rates[-3]:
                r["trend"] = "FALLING"
    except Exception as e:
        log.error(f"Фандинг: {e}")
    return r

def get_oi():
    try:
        d = requests.get(
            f"https://fapi.binance.com/fapi/v1/openInterest?symbol={SYMBOL}",
            timeout=10
        ).json()
        return float(d["openInterest"])
    except:
        return 0.0

def get_oi_history():
    try:
        d = requests.get(
            f"https://fapi.binance.com/futures/data/openInterestHist?symbol={SYMBOL}&period=1h&limit=5",
            timeout=10
        ).json()
        vals = [float(x["sumOpenInterest"]) for x in d]
        trend = "RISING" if vals[-1] > vals[-3] else ("FALLING" if vals[-1] < vals[-3] else "FLAT")
        return {"current": vals[-1], "trend": trend}
    except:
        return {"current": 0.0, "trend": "FLAT"}

def get_ls_ratio():
    try:
        d = requests.get(
            f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={SYMBOL}&period=1h&limit=5",
            timeout=10
        ).json()
        lp = float(d[-1]["longAccount"]) * 100
        lp_3 = float(d[-3]["longAccount"]) * 100 if len(d) >= 3 else lp
        trend = "RISING" if lp > lp_3 else ("FALLING" if lp < lp_3 else "FLAT")
        return {"long_pct": lp, "short_pct": 100 - lp, "trend": trend}
    except:
        return {"long_pct": 50.0, "short_pct": 50.0, "trend": "FLAT"}

def get_taker_ratio():
    try:
        d = requests.get(
            f"https://fapi.binance.com/futures/data/takerlongshortRatio?symbol={SYMBOL}&period=1h&limit=3",
            timeout=10
        ).json()
        ratio = float(d[-1]["buySellRatio"])
        return ratio
    except:
        return 1.0

def get_orderbook():
    try:
        d = requests.get(
            f"https://api.binance.com/api/v3/depth?symbol={SYMBOL}&limit=20",
            timeout=10
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
def calc(df: pd.DataFrame) -> pd.DataFrame:
    # Скользящие средние
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["MA50"] = df["close"].rolling(50).mean()
    df["MA200"] = df["close"].rolling(200).mean()

    # RSI Wilder
    d = df["close"].diff()
    gain = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # RSI сглаженный (сигнальная линия RSI)
    df["RSI_sig"] = df["RSI"].ewm(span=9, adjust=False).mean()

    # Stochastic RSI
    rsi_min = df["RSI"].rolling(14).min()
    rsi_max = df["RSI"].rolling(14).max()
    stoch_k = (df["RSI"] - rsi_min) / (rsi_max - rsi_min + 1e-10) * 100
    df["STOCH_K"] = stoch_k.rolling(3).mean()
    df["STOCH_D"] = df["STOCH_K"].rolling(3).mean()

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_sig"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["MACD"] - df["MACD_sig"]
    df["MACD_cross_bull"] = (df["MACD"] > df["MACD_sig"]) & (df["MACD"].shift(1) <= df["MACD_sig"].shift(1))
    df["MACD_cross_bear"] = (df["MACD"] < df["MACD_sig"]) & (df["MACD"].shift(1) >= df["MACD_sig"].shift(1))

    # ATR Wilder
    hl = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"] - df["close"].shift()).abs()
    df["ATR"] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1).ewm(com=13, adjust=False).mean()

    # Bollinger Bands
    df["BB_mid"] = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std()
    df["BB_upper"] = df["BB_mid"] + 2 * bb_std
    df["BB_lower"] = df["BB_mid"] - 2 * bb_std
    df["BB_width"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_mid"]
    df["BB_pct"] = (df["close"] - df["BB_lower"]) / (df["BB_upper"] - df["BB_lower"] + 1e-10)

    # VWAP скользящий
    df["VWAP"] = (df["close"] * df["volume"]).rolling(20).sum() / df["volume"].rolling(20).sum()

    # CVD — кумулятивная дельта объёма
    df["delta"] = df["taker_buy_base"] - (df["volume"] - df["taker_buy_base"])
    df["CVD"] = df["delta"].rolling(20).sum()
    df["CVD_up"] = df["CVD"] > df["CVD"].shift(3)

    # Объём
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > df["vol_ma"] * 1.5

    # RSI дивергенция (улучшенная — ищем за 10 свечей)
    df["div_bull"] = (
        (df["close"] < df["close"].shift(10)) &
        (df["RSI"] > df["RSI"].shift(10)) &
        (df["RSI"] < 50)
    )
    df["div_bear"] = (
        (df["close"] > df["close"].shift(10)) &
        (df["RSI"] < df["RSI"].shift(10)) &
        (df["RSI"] > 50)
    )

    # Уровни поддержки/сопротивления
    w = 8
    df["is_high"] = df["high"].rolling(w * 2 + 1, center=True).max() == df["high"]
    df["is_low"] = df["low"].rolling(w * 2 + 1, center=True).min() == df["low"]

    # Волатильность (ATR / цена) — для фильтрации флэта
    df["vol_pct"] = df["ATR"] / df["close"] * 100
    return df

# ══════════════════════════════════════════════
# СВЕЧНЫЕ ПАТТЕРНЫ
# ══════════════════════════════════════════════
def patterns(df):
    if len(df) < 3:
        return [], []
    c = df.iloc[-1]
    p = df.iloc[-2]
    pp = df.iloc[-3]
    body = abs(c["close"] - c["open"])
    rng = max(c["high"] - c["low"], 1e-10)
    uw = c["high"] - max(c["close"], c["open"])
    lw = min(c["close"], c["open"]) - c["low"]
    bull, bear = [], []

    if lw > body * 2 and uw < body * 0.5 and c["close"] > c["open"]:
        bull.append("Молот")
    if uw > body * 2 and lw < body * 0.5 and c["close"] < c["open"]:
        bear.append("Пад.звезда")
    if body < rng * 0.08:
        (bull if c["close"] >= p["close"] else bear).append("Доджи")
    if (c["close"] > c["open"] and p["close"] < p["open"] and
        c["open"] <= p["close"] and c["close"] >= p["open"]):
        bull.append("Поглощение")
    if (c["close"] < c["open"] and p["close"] > p["open"] and
        c["open"] >= p["close"] and c["close"] <= p["open"]):
        bear.append("Поглощение")
    if lw > rng * 0.65 and body < rng * 0.25:
        bull.append("Пин↑")
    if uw > rng * 0.65 and body < rng * 0.25:
        bear.append("Пин↓")
    if (c["close"] > c["open"] and p["close"] > p["open"] and pp["close"] > pp["open"] and
        c["close"] > p["close"] > pp["close"]):
        bull.append("3солдата")
    if (c["close"] < c["open"] and p["close"] < p["open"] and pp["close"] < pp["open"] and
        c["close"] < p["close"] < pp["close"]):
        bear.append("3вороны")
    return bull, bear

# ══════════════════════════════════════════════
# УРОВНИ S/R
# ══════════════════════════════════════════════
def find_levels(df, n=100):
    price = df.iloc[-1]["close"]
    recent = df.tail(n)
    highs = recent[recent["is_high"]]["high"].dropna().values
    lows = recent[recent["is_low"]]["low"].dropna().values
    res = sorted([h for h in highs if h > price * 1.001])[:3]
    sup = sorted([l for l in lows if l < price * 0.999], reverse=True)[:3]
    return sup, res

# ══════════════════════════════════════════════
# ТРЕНД 4H / 1D
# ══════════════════════════════════════════════
def htf_bias(df4h, df1d=None):
    if df4h is None or len(df4h) < 50:
        return "NEUTRAL", 0
    r = df4h.iloc[-1]
    bull = bear = 0
    if r["close"] > r["EMA50"]: bull += 1
    else: bear += 1
    if r["EMA50"] > r["EMA200"]: bull += 1
    else: bear += 1
    if r["RSI"] > 50: bull += 1
    else: bear += 1
    if r["EMA9"] > r["EMA21"]: bull += 1
    else: bear += 1
    if r["CVD_up"]: bull += 1
    else: bear += 1
    if r["MACD"] > r["MACD_sig"]: bull += 1
    else: bear += 1
    if r["close"] > r["VWAP"]: bull += 1
    else: bear += 1

    if df1d is not None and len(df1d) >= 50:
        rd = df1d.iloc[-1]
        if rd["close"] > rd["MA200"]: bull += 2
        else: bear += 2
        if rd["EMA50"] > rd["EMA200"]: bull += 1
        else: bear += 1

    total = bull + bear
    strength = round(bull / total * 100) if total > 0 else 50
    if bull >= 5:
        return "LONG", strength
    if bear >= 5:
        return "SHORT", 100 - strength
    return "NEUTRAL", 50

# ══════════════════════════════════════════════
# СИСТЕМА ОЧКОВ — ПОЛНАЯ
# ══════════════════════════════════════════════
def score_signal(df, fund, oi_hist, style, bias, ls, taker, ob):
    empty = {"direction": None, "ls": 0, "ss": 0, "score": 0}
    if df is None or len(df) < 50:
        return empty
    r = df.iloc[-1]
    price = r["close"]
    rsi = r["RSI"]
    atr = r["ATR"]
    vol_pct = r["vol_pct"]

    if vol_pct < 0.3 and style == "SCALP":
        return {**empty, "ls": -1, "ss": -1}

    fund_r = fund["rate"]
    ft = fund["trend"]
    lp = ls["long_pct"]
    oi_tr = oi_hist["trend"]
    L, R_L = 0, []
    S, R_S = 0, []

    # RSI
    if rsi < 30:
        L += 3
        R_L.append(f"RSI {rsi:.0f}↓")
    elif rsi < 40:
        L += 2
        R_L.append(f"RSI {rsi:.0f}↓")
    elif rsi < 50:
        L += 1
        R_L.append(f"RSI {rsi:.0f}↓")
    if rsi > 70:
        S += 3
        R_S.append(f"RSI {rsi:.0f}↑")
    elif rsi > 60:
        S += 2
        R_S.append(f"RSI {rsi:.0f}↑")
    elif rsi > 50:
        S += 1
        R_S.append(f"RSI {rsi:.0f}↑")

    # RSI пересек сигнальную линию
    if r["RSI"] > r["RSI_sig"] and df["RSI"].iloc[-2] <= df["RSI_sig"].iloc[-2]:
        L += 1
        R_L.append("RSI↗сигнал")
    if r["RSI"] < r["RSI_sig"] and df["RSI"].iloc[-2] >= df["RSI_sig"].iloc[-2]:
        S += 1
        R_S.append("RSI↘сигнал")

    # Stochastic RSI
    sk = r["STOCH_K"]
    sd = r["STOCH_D"]
    if sk < 20 and sd < 20:
        L += 2
        R_L.append(f"StochRSI {sk:.0f}перепрод")
    elif sk < 20 and sk > sd:
        L += 1
        R_L.append("StochRSI↑")
    if sk > 80 and sd > 80:
        S += 2
        R_S.append(f"StochRSI {sk:.0f}перекуп")
    elif sk > 80 and sk < sd:
        S += 1
        R_S.append("StochRSI↓")

    # MACD
    if r["MACD_cross_bull"]:
        L += 2
        R_L.append("MACD крест↑")
    elif r["MACD"] > r["MACD_sig"] and r["MACD_hist"] > 0:
        L += 1
        R_L.append("MACD↑")
    if r["MACD_cross_bear"]:
        S += 2
        R_S.append("MACD крест↓")
    elif r["MACD"] < r["MACD_sig"] and r["MACD_hist"] < 0:
        S += 1
        R_S.append("MACD↓")

    # VWAP
    vwap_diff = (price - r["VWAP"]) / r["VWAP"] * 100
    if price < r["VWAP"]:
        L += 1
        R_L.append(f"Цена<VWAP({vwap_diff:.1f}%)")
    else:
        S += 1
        R_S.append(f"Цена>VWAP(+{vwap_diff:.1f}%)")

    # Bollinger
    bp = r["BB_pct"]
    if bp < 0.05:
        L += 3
        R_L.append(f"BB нижняя({bp:.2f})")
    elif bp < 0.2:
        L += 1
        R_L.append(f"BB низ")
    if bp > 0.95:
        S += 3
        R_S.append(f"BB верхняя({bp:.2f})")
    elif bp > 0.8:
        S += 1
        R_S.append(f"BB верх")

    # EMA
    if r["EMA9"] > r["EMA21"]:
        L += 1
        R_L.append("EMA9>21")
    else:
        S += 1
        R_S.append("EMA9<21")
    if r["EMA50"] > r["EMA200"]:
        L += 1
        R_L.append("EMA50>200")
    else:
        S += 1
        R_S.append("EMA50<200")

    # CVD
    if r["CVD_up"]:
        L += 2
        R_L.append("CVD↑покупки")
    else:
        S += 2
        R_S.append("CVD↓продажи")

    # RSI дивергенция
    if r["div_bull"]:
        L += 3
        R_L.append("Div RSI")
    if r["div_bear"]:
        S += 3
        R_S.append("Div RSI")

    # Фандинг
    if fund_r < -0.001:
        L += 3
        R_L.append(f"Fund{fund_r:.5f}↓")
    elif fund_r < 0:
        L += 2
        R_L.append(f"Fund{fund_r:.5f}↓")
    elif fund_r < 0.001:
        L += 1
        R_L.append("Fund нейтр")
    if fund_r > 0.005:
        S += 3
        R_S.append(f"Fund{fund_r:.5f}↑")
    elif fund_r > 0.003:
        S += 2
        R_S.append(f"Fund{fund_r:.5f}↑")
    elif fund_r > 0.001:
        S += 1
        R_S.append("Fund умерен")
    if ft == "FALLING":
        L += 1
        R_L.append("Fund↓тренд")
    if ft == "RISING":
        S += 1
        R_S.append("Fund↑тренд")

    # Long/Short Ratio
    if lp < 40:
        L += 3
        R_L.append(f"L/S {lp:.0f}%")
    elif lp < 45:
        L += 2
        R_L.append(f"L/S {lp:.0f}%")
    elif lp < 50:
        L += 1
    if lp > 70:
        S += 3
        R_S.append(f"L/S {lp:.0f}%")
    elif lp > 65:
        S += 2
        R_S.append(f"L/S {lp:.0f}%")
    elif lp > 55:
        S += 1

    # Taker Ratio
    if taker > 1.2:
        L += 2
        R_L.append(f"Taker {taker:.2f}покуп")
    elif taker > 1.0:
        L += 1
    if taker < 0.8:
        S += 2
        R_S.append(f"Taker {taker:.2f}прод")
    elif taker < 1.0:
        S += 1

    # Стакан
    if ob > 20:
        L += 3
        R_L.append(f"Стакан+{ob:.0f}%")
    elif ob > 10:
        L += 2
        R_L.append(f"Стакан+{ob:.0f}%")
    elif ob > 5:
        L += 1
    if ob < -20:
        S += 3
        R_S.append(f"Стакан{ob:.0f}%")
    elif ob < -10:
        S += 2
        R_S.append(f"Стакан{ob:.0f}%")
    elif ob < -5:
        S += 1

    # OI тренд
    if oi_tr == "RISING":
        L += 1
        R_L.append("OI↑")
    elif oi_tr == "FALLING":
        S += 1
        R_S.append("OI↓")

    # Объём
    if r["vol_spike"]:
        L += 1
        R_L.append("Vol↑")
        S += 1
        R_S.append("Vol↑")

    # HTF Bias
    if bias == "LONG":
        L += 3
        R_L.append("4h бычий")
        S -= 5
    elif bias == "SHORT":
        S += 3
        R_S.append("4h медвежий")
        L -= 5

    # Порог
    thr = {"SCALP": 8, "MID": 10, "SWING": 12}.get(style, 10)
    if L >= thr and L > S:
        direction, sc, reasons = "LONG", L, R_L
    elif S >= thr and S > L:
        direction, sc, reasons = "SHORT", S, R_S
    else:
        return {**empty, "ls": L, "ss": S}

    # Уровни входа
    m = {"SCALP": (0.6, 0.8, 1.4), "MID": (1.2, 1.5, 2.5), "SWING": (2.0, 2.5, 4.0)}
    sm, t1m, t2m = m.get(style, m["MID"])
    if direction == "LONG":
        entry = price
        stop = entry - atr * sm
        tp1 = entry + atr * t1m
        tp2 = entry + atr * t2m
    else:
        entry = price
        stop = entry + atr * sm
        tp1 = entry - atr * t1m
        tp2 = entry - atr * t2m
    rr = abs(tp1 - entry) / max(abs(entry - stop), 0.01)

    return {
        "direction": direction,
        "score": sc,
        "ls": L,
        "ss": S,
        "reasons": reasons,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
        "bb_pct": r["BB_pct"],
        "stoch": r["STOCH_K"],
        "macd_hist": r["MACD_hist"],
    }

# ══════════════════════════════════════════════
# СБОРКА ОДНОГО СООБЩЕНИЯ
# ══════════════════════════════════════════════
def build_report(df15m, df1h, df4h, df1d, fund, oi, oi_hist, ls, taker, ob):
    bias, bias_str = htf_bias(df4h, df1d)
    bias_e = {"LONG": "🟢", "SHORT": "🔴", "NEUTRAL": "⚪"}
    price = df1h.iloc[-1]["close"] if df1h is not None else 0
    atr1h = df1h.iloc[-1]["ATR"] if df1h is not None else 0

    pb1h, ps1h = patterns(df1h) if df1h is not None else ([], [])
    pb15, ps15 = patterns(df15m) if df15m is not None else ([], [])
    sup, res = find_levels(df1h) if df1h is not None else ([], [])

    fe = "🔴" if fund["rate"] > 0.003 else ("🟢" if fund["rate"] < 0 else "⚪")
    lse = "🔴" if ls["long_pct"] > 65 else ("🟢" if ls["long_pct"] < 40 else "⚪")
    obe = "🟢" if ob > 10 else ("🔴" if ob < -10 else "⚪")
    te = "🟢" if taker > 1.2 else ("🔴" if taker < 0.8 else "⚪")
    oie = "🟢" if oi_hist["trend"] == "RISING" else ("🔴" if oi_hist["trend"] == "FALLING" else "⚪")

    lines = [
        f"{'━'*30}",
        f"{SYMBOL} {price:.2f} $",
        f"{'━'*30}",
        "",
        "РЫНОК",
        f"{fe} Фандинг: {fund['rate']:.5f} ({fund['trend']})",
        f"{lse} Лонг/Шорт: {ls['long_pct']:.1f}% / {ls['short_pct']:.1f}%",
        f"{te} Taker ratio: {taker:.2f}",
        f"{obe} Стакан: {'+' if ob > 0 else ''}{ob:.1f}%",
        f"{oie} OI тренд: {oi_hist['trend']} ({oi_hist['current']:,.0f})",
        f"{bias_e.get(bias, '⚪')} 4h/1d тренд: {bias} ({bias_str}%)",
    ]

    if sup or res:
        lines += ["", "УРОВНИ (1h)"]
        if res:
            lines.append("Сопр: " + " · ".join(f"{x:.2f}" for x in res))
        if sup:
            lines.append("Подд: " + " · ".join(f"{x:.2f}" for x in sup))

    all_bull = pb1h + pb15
    all_bear = ps1h + ps15
    if all_bull or all_bear:
        lines += ["", "ПАТТЕРНЫ"]
        if all_bull:
            lines.append("🟢 " + " ".join(all_bull))
        if all_bear:
            lines.append("🔴 " + " ".join(all_bear))

    lines += ["", "СИГНАЛЫ", ""]

    combos = [
        (df15m, "15m", "SCALP"),
        (df1h, "1h", "SCALP"),
        (df1h, "1h", "MID"),
        (df4h, "4h", "SWING"),
    ]
    any_signal = False

    for df, tf, style in combos:
        res_s = score_signal(df, fund, oi_hist, style, bias, ls, taker, ob)
        if res_s["direction"] is None:
            ls_n = res_s.get("ls", 0)
            ss_n = res_s.get("ss", 0)
            if ls_n == -1:
                lines.append(f"[{tf} {style}] Флэт — нет входа")
            else:
                lines.append(f"[{tf} {style}] ЖДИ L:{ls_n} S:{ss_n}")
        else:
            any_signal = True
            e = "🟢" if res_s["direction"] == "LONG" else "🔴"
            lines.append(
                f"{e} [{tf} · {style} · {res_s['direction']}] {res_s['score']} очков\n"
                f"   Вход: {res_s['entry']:.2f}\n"
                f"   Стоп: {res_s['stop']:.2f} | ТП1: {res_s['tp1']:.2f} ТП2: {res_s['tp2']:.2f}\n"
                f"   R/R: {res_s['rr']:.1f} | Stoch: {res_s['stoch']:.0f} MACD: {'↑' if res_s['macd_hist'] > 0 else '↓'}\n"
                f"   Причины: {' · '.join(res_s['reasons'][:6])}"
            )

    if not any_signal:
        lines.append("Чётких сигналов нет — жди подтверждения")

    lines.append(f"\n{datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC")
    return "\n".join(lines)

# ══════════════════════════════════════════════
# ОСНОВНОЙ ЦИКЛ
# ══════════════════════════════════════════════
def run_scan():
    log.info("Запуск сканирования...")
    try:
        df15m = get_klines("15m", 300)
        df1h = get_klines("1h", 300)
        df4h = get_klines("4h", 300)
        df1d = get_klines("1d", 300)
        for df in (df15m, df1h, df4h, df1d):
            if df is not None:
                calc(df)
        fund = get_funding()
        oi = get_oi()
        oi_hist = get_oi_history()
        ls = get_ls_ratio()
        taker = get_taker_ratio()
        ob = get_orderbook()
        report = build_report(df15m, df1h, df4h, df1d, fund, oi, oi_hist, ls, taker, ob)
        send(report)
        log.info("Скан завершён")
    except Exception as e:
        log.error(f"Ошибка скана: {e}")
        send(f"Ошибка скана: {e}")

def bot_loop():
    log.info(f"Бот запущен | {SYMBOL} | интервал {SCAN_INTERVAL // 60} мин")
    send(
        f"ETH Signal Bot запущен\n"
        f"Символ: {SYMBOL}\n"
        f"Интервал: каждые {SCAN_INTERVAL // 60} мин\n"
        f"{datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC"
    )
    while True:
        run_scan()
        log.info(f"Следующий скан через {SCAN_INTERVAL // 60} мин")
        time.sleep(SCAN_INTERVAL)

# ══════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════
if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
