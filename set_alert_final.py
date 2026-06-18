"""
SET Stock Alert — GitHub Actions Edition (no pandas_ta)
EMA และ ATR คำนวณด้วย pandas โดยตรง
"""
import os, sys, time, logging, requests
import pandas as pd
from datetime import datetime
from io import StringIO

# ============================================================
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

WATCHLIST = [
    "BDMS.BK", "BCH.BK",  "MEGA.BK",  "AP.BK",
    "SCB.BK",  "KTB.BK",  "TTW.BK",   "RATCH.BK", "RAM.BK",
    "SNNP.BK", "TFM.BK",  "OSP.BK",   "BGRIM.BK",
    "COCOCO.BK","OR.BK",  "TU.BK",
    "ASW.BK",  "AURA.BK", "BAM.BK",   "DIF.BK",
    "NER.BK",  "SAK.BK",  "SJWD.BK",  "SPRC.BK",
]

EMA_FAST   = 12
EMA_SLOW   = 26
ATR_PERIOD = 14
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def stooq_ticker(ticker: str) -> str:
    code, exch = ticker.upper().split(".")
    return f"{code.lower()}.{'th' if exch == 'BK' else exch.lower()}"


def fetch_ohlcv(ticker: str) -> pd.DataFrame | None:
    url = f"https://stooq.com/q/d/l/?s={stooq_ticker(ticker)}&i=d"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or len(r.text) < 100:
            log.warning(f"{ticker}: HTTP {r.status_code}")
            return None
        df = pd.read_csv(StringIO(r.text), parse_dates=["Date"])
        df.columns = [c.strip().title() for c in df.columns]
        df = df.sort_values("Date").reset_index(drop=True)
        if len(df) < 40:
            log.warning(f"{ticker}: ข้อมูลน้อยเกินไป ({len(df)} rows)")
            return None
        return df
    except Exception as e:
        log.warning(f"{ticker}: fetch failed — {e}")
        return None


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # EMA ด้วย pandas ewm
    df["ema_fast"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    # ATR คำนวณเอง
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    return df.dropna().reset_index(drop=True)


def check_signals(df: pd.DataFrame) -> dict:
    empty = {"ema_cross": False, "atr_buy": False, "detail": {}}
    if len(df) < 3:
        return empty

    t0, t1, t2 = df.iloc[-1], df.iloc[-2], df.iloc[-3]

    ema_cross = bool(
        (t0["ema_fast"] > t0["ema_slow"]) and
        (t1["ema_fast"] <= t1["ema_slow"])
    )
    atr_lvl_today = float(t1["High"]) + float(t1["atr"])
    atr_lvl_prev  = float(t2["High"]) + float(t2["atr"])
    atr_buy = bool(
        (float(t0["Close"]) > atr_lvl_today) and
        (float(t1["Close"]) <= atr_lvl_prev)
    )

    date_val = t0["Date"]
    detail = {
        "date":      date_val.strftime("%d %b %Y") if hasattr(date_val, "strftime") else str(date_val),
        "close":     round(float(t0["Close"]),    2),
        "ema_fast":  round(float(t0["ema_fast"]), 2),
        "ema_slow":  round(float(t0["ema_slow"]), 2),
        "atr":       round(float(t0["atr"]),      2),
        "atr_level": round(atr_lvl_today,          2),
        "prev_high": round(float(t1["High"]),     2),
    }
    return {"ema_cross": ema_cross, "atr_buy": atr_buy, "detail": detail}


def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Telegram error: {e}")
        return False


def build_message(ticker: str, sig: dict) -> str:
    d, code = sig["detail"], ticker.replace(".BK", "")
    out = [
        f"🔔 <b>SET ALERT</b>  |  <b>{code}</b>",
        f"📅 {d['date']}   ราคาปิด <b>{d['close']}</b>",
    ]
    if sig["ema_cross"]:
        out += [
            "",
            f"📈 <b>EMA Crossover (วันแรก)</b>",
            f"   EMA{EMA_FAST} = {d['ema_fast']}",
            f"   EMA{EMA_SLOW} = {d['ema_slow']}",
        ]
    if sig["atr_buy"]:
        out += [
            "",
            f"💥 <b>ATR Breakout Buy (วันแรก)</b>",
            f"   Close {d['close']}  >  ATR Level {d['atr_level']}",
            f"   (High เมื่อวาน {d['prev_high']} + ATR{ATR_PERIOD} {d['atr']})",
        ]
    if sig["ema_cross"] and sig["atr_buy"]:
        out += ["", "⭐ <b>Double Signal!</b>"]
    out += ["", "<i>ข้อมูลเพื่อการศึกษาเท่านั้น</i>"]
    return "\n".join(out)


def run():
    now = datetime.utcnow()
    log.info("=" * 52)
    log.info(f"SET Alert — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"Watchlist: {len(WATCHLIST)} หุ้น")
    log.info("=" * 52)

    if now.weekday() >= 5:
        log.info("วันหยุดตลาด — ออก")
        send_telegram("📊 SET Alert: วันหยุดตลาด ไม่สแกน")
        return

    found, skipped, sent = [], [], 0

    for ticker in WATCHLIST:
        log.info(f"▶ {ticker}")
        df = fetch_ohlcv(ticker)
        if df is None:
            skipped.append(ticker)
            time.sleep(0.5)
            continue

        df  = compute_indicators(df)
        sig = check_signals(df)
        d   = sig["detail"]

        flags = []
        if sig["ema_cross"]: flags.append("EMA_CROSS✅")
        if sig["atr_buy"]:   flags.append("ATR_BUY✅")
        log.info(f"  rows={len(df)} Close={d.get('close','?')} {' '.join(flags) or 'no signal'}")

        if sig["ema_cross"] or sig["atr_buy"]:
            found.append(ticker)
            if send_telegram(build_message(ticker, sig)):
                sent += 1
                log.info(f"  📨 Telegram ส่งแล้ว")
            time.sleep(1)

        time.sleep(0.3)

    if found:
        codes   = ", ".join(t.replace(".BK", "") for t in found)
        summary = f"📊 <b>SET Alert สรุป</b> {now.strftime('%d %b %Y')}\nสแกน {len(WATCHLIST)} หุ้น\n🔔 พบสัญญาณ: <b>{codes}</b>"
    else:
        summary = f"📊 <b>SET Alert สรุป</b> {now.strftime('%d %b %Y')}\nสแกน {len(WATCHLIST)} หุ้น\n✅ ไม่มีสัญญาณวันนี้"

    if skipped:
        summary += f"\n⚠️ ข้าม: {', '.join(t.replace('.BK','') for t in skipped)}"

    send_telegram(summary)
    log.info(f"Done — สัญญาณ {len(found)} | ส่ง {sent}")


if __name__ == "__main__":
    run()
