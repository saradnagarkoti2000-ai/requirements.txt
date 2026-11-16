import os
import time
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

API_KEY = os.getenv("CAP_API_KEY", "")
IDENTIFIER = os.getenv("CAP_IDENTIFIER", "")
PASSWORD = os.getenv("CAP_PASSWORD", "")
ENV_MODE = os.getenv("CAP_ENV", "DEMO").upper()

BASE_URL = (
    "https://api-capital.backend-capital.com"
    if ENV_MODE == "LIVE"
    else "https://demo-api-capital.backend-capital.com"
)

session_cache = {"cst": None, "token": None, "last_auth": 0}


def get_headers():
    now = time.time()
    # Capital.com sessions expire after 10 mins of inactivity; refresh every 8 mins
    if not session_cache["cst"] or (now - session_cache["last_auth"] > 480):
        resp = requests.post(
            f"{BASE_URL}/api/v1/session",
            headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
            json={
                "identifier": IDENTIFIER,
                "password": PASSWORD,
                "encryptedPassword": False,
            },
            timeout=10,
        )
        resp.raise_for_status()
        session_cache["cst"] = resp.headers.get("CST")
        session_cache["token"] = resp.headers.get("X-SECURITY-TOKEN")
        session_cache["last_auth"] = now

    session_cache["last_auth"] = now
    return {
        "X-CAP-API-KEY": API_KEY,
        "CST": session_cache["cst"],
        "X-SECURITY-TOKEN": session_cache["token"],
    }


def calc_ema(prices, period):
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema


def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


@app.get("/api/signal")
def get_gold_signal(resolution: str = "MINUTE_5"):
    try:
        headers = get_headers()
        r = requests.get(
            f"{BASE_URL}/api/v1/prices/GOLD",
            headers=headers,
            params={"resolution": resolution, "max": 40},
            timeout=10,
        )
        if r.status_code == 401:
            session_cache["cst"] = None
            headers = get_headers()
            r = requests.get(
                f"{BASE_URL}/api/v1/prices/GOLD",
                headers=headers,
                params={"resolution": resolution, "max": 40},
                timeout=10,
            )
        r.raise_for_status()
        candles = r.json().get("prices", [])
        if len(candles) < 25:
            return {"error": "Not enough candle data returned"}

        # Use mid-prices from completed & current candles
        closes = [(c["closePrice"]["bid"] + c["closePrice"]["ask"]) / 2 for c in candles]
        opens = [(c["openPrice"]["bid"] + c["openPrice"]["ask"]) / 2 for c in candles]
        highs = [(c["highPrice"]["bid"] + c["highPrice"]["ask"]) / 2 for c in candles]
        lows = [(c["lowPrice"]["bid"] + c["lowPrice"]["ask"]) / 2 for c in candles]

        ema9 = calc_ema(closes[-25:], 9)
        ema21 = calc_ema(closes[-30:], 21)
        rsi = calc_rsi(closes, 14)

        # Evaluate last two closed candles (-3 and -2) so signals don't flicker mid-candle
        prev_o, prev_c = opens[-3], closes[-3]
        curr_o, curr_c, curr_h, curr_l = opens[-2], closes[-2], highs[-2], lows[-2]

        prev_body = abs(prev_c - prev_o)
        curr_body = abs(curr_c - curr_o)
        lower_wick = min(curr_o, curr_c) - curr_l
        upper_wick = curr_h - max(curr_o, curr_c)

        bullish_engulf = (prev_c < prev_o) and (curr_c > curr_o) and (curr_c >= prev_o) and (curr_o <= prev_c)
        bearish_engulf = (prev_c > prev_o) and (curr_c < curr_o) and (curr_c <= prev_o) and (curr_o >= prev_c)
        bullish_pin = (lower_wick > curr_body * 1.8) and (curr_c >= curr_o)
        bearish_pin = (upper_wick > curr_body * 1.8) and (curr_c <= curr_o)

        pattern = "Neutral Candle"
        signal = "HOLD"
        reason = "Waiting for clean candlestick confirmation."

        if bullish_engulf or bullish_pin:
            pattern = "Bullish Engulfing" if bullish_engulf else "Bullish Rejection Wick"
            if ema9 > ema21 and 40 <= rsi <= 68:
                signal = "OPEN BUY"
                reason = f"{pattern} aligned with uptrend (EMA9 > EMA21, RSI {rsi:.1f})."
            elif ema9 < ema21:
                signal = "CLOSE SELL"
                reason = f"{pattern} appeared against downtrend — consider closing shorts."
        elif bearish_engulf or bearish_pin:
            pattern = "Bearish Engulfing" if bearish_engulf else "Bearish Rejection Wick"
            if ema9 < ema21 and 32 <= rsi <= 60:
                signal = "OPEN SELL"
                reason = f"{pattern} aligned with downtrend (EMA9 < EMA21, RSI {rsi:.1f})."
            elif ema9 > ema21:
                signal = "CLOSE BUY"
                reason = f"{pattern} appeared against uptrend — consider closing longs."
        elif rsi >= 76:
            signal = "CLOSE BUY"
            pattern = "Overbought Exhaustion"
            reason = f"RSI reached {rsi:.1f} — take profit / tighten stops on longs."
        elif rsi <= 24:
            signal = "CLOSE SELL"
            pattern = "Oversold Exhaustion"
            reason = f"RSI dropped to {rsi:.1f} — take profit / tighten stops on shorts."

        return {
            "price": round(closes[-1], 2),
            "signal": signal,
            "pattern": pattern,
            "reason": reason,
            "ema9": round(ema9, 2),
            "ema21": round(ema21, 2),
            "rsi": round(rsi, 1),
            "env": ENV_MODE,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()
