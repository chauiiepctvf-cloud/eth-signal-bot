import requests
import pandas as pd
import numpy as np
import os
from datetime import datetime
# ─────────────────────────────────────────────
# НАСТРОЙКИ
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "СЮДА
ВСТАВЬ
ТОКЕН
_
_
_
БОТА")
CHAT_ID = os.environ.get("CHAT_ID", "843894335")
SYMBOL = "ETHUSDT"
# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str):
url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
try:
requests.post(url, json=payload, timeout=10)
except Exception as e:
print(f"Telegram ошибка: {e}")
# ─────────────────────────────────────────────
# ЗАГРУЗКА СВЕЧЕЙ
# ─────────────────────────────────────────────
def get_klines(symbol: str = SYMBOL, interval: str = "1h", limit: int = 250) -> pd.DataFrame
url = (
f"https://api.binance.com/api/v3/klines"
f"?symbol={symbol}&interval={interval}&limit={limit}"
)
try:
data = requests.get(url, timeout=10).json()
except Exception as e:
print(f"Ошибка загрузки свечей ({interval}): {e}")
return None
df = pd.DataFrame(data, columns=[
"time", "open", "high", "low", "close", "volume",
"close_time", "quote_vol", "trades",
"taker_buy_base", "taker_buy_quote", "ignore"
])
for col in ("open", "high", "low", "close", "volume"):
df[col] = df[col].astype(float)
df["time"] = pd.to_datetime(df["time"], unit="ms")
return df.reset_index(drop=True)
# ─────────────────────────────────────────────
# ИНДИКАТОРЫ
# ─────────────────────────────────────────────
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
# Скользящие средние
df["MA50"] = df["close"].rolling(50).mean()
df["MA200"] = df["close"].rolling(200).mean()
df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
# RSI (14)
delta = df["close"].diff()
gain = delta.clip(lower=0)
loss = -delta.clip(upper=0)
avg_gain = gain.ewm(com=13, adjust=False).mean() avg_loss = loss.ewm(com=13, adjust=False).mean()
rs = avg_gain / avg_loss.replace(0, np.nan)
df["RSI"] = 100 - (100 / (1 + rs))
# Wilder smoothing
# ATR (14)
hl = df["high"] - df["low"]
hpc = (df["high"] - df["close"].shift()).abs()
lpc = (df["low"] - df["close"].shift()).abs()
tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
df["ATR"] = tr.ewm(com=13, adjust=False).mean() # Wilder smoothing
# Bollinger Bands (20, 2σ)
df["BB_mid"] = df["close"].rolling(20).mean()
bb_std = df["close"].rolling(20).std()
df["BB_upper"] = df["BB_mid"] + 2 * bb_std
df["BB_lower"] = df["BB_mid"] - 2 * bb_std
df["BB_width"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_mid"] # волатильность
# VWAP (скользящий, окно 20 свечей)
df["VWAP"] = (
(df["close"] * df["volume"]).rolling(20).sum()
/ df["volume"].rolling(20).sum()
)
# RSI дивергенция (простая: цена выше предыдущего хая, RSI — нет)
price_hh = df["close"] > df["close"].shift(5)
rsi_lh = df["RSI"] < df["RSI"].shift(5)
price_ll = df["close"] < df["close"].shift(5)
rsi_hl = df["RSI"] > df["RSI"].shift(5)
df["div_bear"] = price_hh & rsi_lh # медвежья дивергенция
df["div_bull"] = price_ll & rsi_hl # бычья дивергенция
# Объём: выше ли текущий объём среднего за 20 свечей
df["vol_avg"] = df["volume"].rolling(20).mean()
df["vol_spike"] = df["volume"] > df["vol_avg"] * 1.3
return df
# ─────────────────────────────────────────────
# ФАНДИНГ-РЕЙТ (текущий + история тренда)
# ─────────────────────────────────────────────
def get_funding_data() -> dict:
"""Возвращает текущий фандинг и тренд (растёт / падает)."""
result = {"rate": 0.0, "trend": "FLAT", "history": []}
try:
# Текущий фандинг
current = requests.get(
f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={SYMBOL}",
timeout=10
).json()
rate = float(current["lastFundingRate"])
result["rate"] = rate
# История последних 8 периодов фандинга
hist = requests.get(
f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={SYMBOL}&limit=8",
timeout=10
).json()
rates = [float(x["fundingRate"]) for x in hist]
result["history"] = rates
# Тренд фандинга
if len(rates) >= 3:
if rates[-1] > rates[-3]:
result["trend"] = "RISING"
elif rates[-1] < rates[-3]:
result["trend"] = "FALLING"
except Exception as e:
print(f"Ошибка фандинга: {e}")
return result
# ─────────────────────────────────────────────
# ОТКРЫТЫЙ ИНТЕРЕС
# ─────────────────────────────────────────────
def get_open_interest() -> float:
try:
data = requests.get(
f"https://fapi.binance.com/fapi/v1/openInterest?symbol={SYMBOL}",
timeout=10
).json()
return float(data["openInterest"])
except:
return 0.0
# ─────────────────────────────────────────────
# BIAS СТАРШЕГО ТАЙМФРЕЙМА (4h)
# ─────────────────────────────────────────────
def get_htf_bias(df_4h: pd.DataFrame | None) -> str:
"""
Определяет направление на 4h.
Возвращает: 'LONG', 'SHORT' или 'NEUTRAL'
"""
if df_4h is None or len(df_4h) < 50:
return "NEUTRAL"
row = df_4h.iloc[-1]
price = row["close"]
ma50 = row["MA50"]
ma200 = row["MA200"]
rsi = row["RSI"]
ema9 = row["EMA9"]
ema21 = row["EMA21"]
bull_points = 0
bear_points = 0
if price > ma50: bull_points += 1
else: bear_points += 1
if ma50 > ma200: bull_points += 1
else: bear_points += 1
if rsi > 50: bull_points += 1
else: bear_points += 1
if ema9 > ema21: bull_points += 1
else: bear_points += 1
if price > row["VWAP"]: bull_points += 1
else: bear_points += 1
if bull_points >= 4:
return "LONG"
elif bear_points >= 4:
return "SHORT"
return "NEUTRAL"
# ─────────────────────────────────────────────
# УРОВНИ ВХОДА / СТОП / ТЕЙК
# ─────────────────────────────────────────────
def get_trade_levels(
price: float,
atr: float,
signal_type: str,
trade_style: str
) -> tuple[float, float, float, float]:
"""Возвращает (entry, stop, tp1, tp2)."""
multipliers = {
"SCALP": (0.5, 0.5, 0.9),
"MID": (1.2, 1.2, 2.2),
"SWING": (2.0, 2.0, 3.5),
}
stop_m, tp1_m, tp2_m = multipliers.get(trade_style, multipliers["MID"])
if signal_type == "LONG":
return price, price - atr * stop_m, price + atr * tp1_m, price + atr * tp2_m
else:
return price, price + atr * stop_m, price - atr * tp1_m, price - atr * tp2_m
# ─────────────────────────────────────────────
# АНАЛИЗ ТАЙМФРЕЙМА
# ─────────────────────────────────────────────
def analyze_timeframe(
df: pd.DataFrame | None,
funding: dict,
oi: float,
tf_name: str,
trade_style: str,
htf_bias: str = "NEUTRAL"
) -> dict:
"""
Анализирует один таймфрейм.
Возвращает словарь с сигналом и деталями.
"""
default = {
"signal": " ЖДИ",
"reason": "Недостаточно данных",
"entry": None, "stop": None, "tp1": None, "tp2": None,
"score": 0, "direction": None,
}
if df is None or len(df) < 50:
return default
row = df.iloc[-1]
price = row["close"]
rsi = row["RSI"]
atr = row["ATR"]
ema9 = row["EMA9"]
ema21 = row["EMA21"]
ma50 = row["MA50"]
vwap = row["VWAP"]
bb_low = row["BB_lower"]
bb_top = row["BB_upper"]
bb_w = row["BB_width"]
vol_ok = row["vol_spike"]
div_bull = row["div_bull"]
div_bear = row["div_bear"]
fund_rate = funding["rate"]
fund_trend = funding["trend"]
# ── Считаем очки LONG ──────────────────────
long_score = 0
long_reasons = []
if rsi < 35:
long_score += 2
long_reasons.append(f"RSI={rsi:.1f} (перепродан)")
elif rsi < 45:
long_score += 1
long_reasons.append(f"RSI={rsi:.1f} (низкий)")
if price < vwap:
long_score += 1
long_reasons.append("Цена ниже VWAP")
if price <= bb_low * 1.005:
long_score += 2
long_reasons.append("Цена у нижней Bollinger Band")
if ema9 > ema21:
long_score += 1
long_reasons.append("EMA9 > EMA21")
if fund_rate < 0:
long_score += 2
long_reasons.append(f"Фандинг отриц. ({fund_rate:.5f})")
elif fund_rate < 0.001:
long_score += 1
long_reasons.append("Фандинг нейтральный")
if fund_trend == "FALLING":
long_score += 1
long_reasons.append("Фандинг снижается")
if div_bull:
long_score += 2
long_reasons.append("Бычья RSI дивергенция")
if vol_ok:
long_score += 1
long_reasons.append("Повышенный объём")
if trade_style == "MID" and oi > 2_000_000:
long_score += 1
long_reasons.append(f"OI высокий ({oi:,.0f})")
if htf_bias == "LONG":
long_score += 2
long_reasons.append("4h бычий")
elif htf_bias == "SHORT":
long_score -= 3 # Против тренда — штраф
# ── Считаем очки SHORT ─────────────────────
short_score = 0
short_reasons = []
if rsi > 65:
short_score += 2
short_reasons.append(f"RSI={rsi:.1f} (перекуплен)")
elif rsi > 55:
short_score += 1
short_reasons.append(f"RSI={rsi:.1f} (высокий)")
if price > vwap:
short_score += 1
short_reasons.append("Цена выше VWAP")
if price >= bb_top * 0.995:
short_score += 2
short_reasons.append("Цена у верхней Bollinger Band")
if ema9 < ema21:
short_score += 1
short_reasons.append("EMA9 < EMA21")
if fund_rate > 0.003:
short_score += 2
short_reasons.append(f"Фандинг высокий ({fund_rate:.5f})")
elif fund_rate > 0.001:
short_score += 1
short_reasons.append("Фандинг умеренный")
if fund_trend == "RISING":
short_score += 1
short_reasons.append("Фандинг растёт")
if div_bear:
short_score += 2
short_reasons.append("Медвежья RSI дивергенция")
if vol_ok:
short_score += 1
short_reasons.append("Повышенный объём")
if htf_bias == "SHORT":
short_score += 2
short_reasons.append("4h медвежий")
elif htf_bias == "LONG":
short_score -= 3
# Минимальный порог по стилю
thresholds = {"SCALP": 5, "MID": 6, "SWING": 7}
threshold = thresholds.get(trade_style, 6)
# ── Выбираем сигнал ────────────────────────
if long_score >= threshold and long_score > short_score:
direction = "LONG"
score = long_score
reasons = long_reasons
elif short_score >= threshold and short_score > long_score:
direction = "SHORT"
score = short_score
reasons = short_reasons
else:
return {
**default,
"signal": " ЖДИ",
"reason": f"Сигнал слабый (L:{long_score} / S:{short_score})",
"score": max(long_score, short_score),
}
entry, stop, tp1, tp2 = get_trade_levels(price, atr, direction, trade_style)
return {
"signal": " LONG" if direction == "LONG" else " SHORT",
"direction": direction,
"score": score,
"reason": " | ".join(reasons),
"entry": entry,
"stop": stop,
"tp1": tp1,
"tp2": tp2,
"rsi": rsi,
"atr": atr,
"bb_width": bb_w,
}
# ─────────────────────────────────────────────
# ФОРМАТИРОВАНИЕ СООБЩЕНИЯ
# ─────────────────────────────────────────────
def format_message(tf: str, style: str, result: dict, htf_bias: str) -> str:
bias_emoji = {"LONG": " ", "SHORT": " ", "NEUTRAL": " "}
lines = [
f"<b>── {SYMBOL} [{tf}] {style} ──</b>",
f"Сигнал: <b>{result['signal']}</b> (очков: {result['score']})",
f"Тренд 4h: {bias_emoji.get(htf_bias, ' ')} {htf_bias}",
f"Причины: {result['reason']}",
]
if result["entry"] is not None:
rr = abs(result["tp1"] - result["entry"]) / abs(result["entry"] - result["stop"])
lines += [
f"",
f"Вход: <b>{result['entry']:.2f}</b>",
f"Стоп: {result['stop']:.2f}",
f"ТП1: {result['tp1']:.2f}",
f"ТП2: {result['tp2']:.2f}",
f"R/R: {rr:.2f}",
]
if result.get("bb_width"):
vol_desc = "высокая" if result["bb_width"] > 0.05 else "низкая"
lines.append(f"Волатильность BB: {vol_desc} ({result['bb_width']:.3f})")
lines.append(f"\n {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
return "\n".join(lines)
# ─────────────────────────────────────────────
# ГЛАВНАЯ ФУНКЦИЯ
# ─────────────────────────────────────────────
def main():
print(f"[{datetime.utcnow()}] Запуск анализа {SYMBOL}...")
# Загружаем данные
df_4h = get_klines(interval="4h", limit=250)
df_1h = get_klines(interval="1h", limit=250)
df_15m = get_klines(interval="15m", limit=250)
# Считаем индикаторы
if df_4h is not None: df_4h = calculate_indicators(df_4h)
if df_1h is not None: df_1h = calculate_indicators(df_1h)
if df_15m is not None: df_15m = calculate_indicators(df_15m)
# Фандинг и OI
funding = get_funding_data()
oi = get_open_interest()
# Bias старшего ТФ
htf_bias = get_htf_bias(df_4h)
print(f"Фандинг: {funding['rate']:.5f} | Тренд: {funding['trend']} | OI: {oi:,.0f}")
print(f"4h Bias: {htf_bias}")
# Анализ по таймфреймам и стилям
analyses = [
(df_15m, "15m", "SCALP"),
(df_1h, "1h", "SCALP"),
(df_1h, "1h", "MID"),
(df_4h, "4h", "SWING"),
]
for df, tf, style in analyses:
result = analyze_timeframe(df, funding, oi, tf, style, htf_bias)
# Отправляем только реальные сигналы (не ЖДИ)
if result["direction"] is not None:
msg = format_message(tf, style, result, htf_bias)
print(msg)
send_telegram(msg)
else:
print(f"[{tf} {style}] Нет сигнала — {result['reason']}")
print("Анализ завершён.")
if __name__ == "__main__":
main()
