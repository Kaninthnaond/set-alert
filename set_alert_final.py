"""
SET Stock Alert — GitHub Actions + Google Sheets Watchlist
ดึง Watchlist จาก Google Sheets โดยใช้ Service Account
"""
import os, sys, time, logging, requests, json, base64
import pandas as pd
import yfinance as yf
from datetime import datetime

# ============================================================
# CONFIG — ค่าจาก GitHub Secrets ทั้งหมด
# ============================================================
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
SPREADSHEET_ID    = os.environ["SPREADSHEET_ID"]       # ID ของ Google Sheet
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS"]   # JSON key ของ Service Account

WATCHLIST_SHEET   = "Watchlist"   # ชื่อ sheet tab
TICKER_COL        = 1             # คอลัมน์ A = 1
SKIP_ROWS         = 2             # ข้าม 2 แถวแรก (title + header)

EMA_FAST   = 12
EMA_SLOW   = 26
ATR_PERIOD = 14

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ============================================================
# GOOGLE SHEETS — ใช้ gspread + Service Account
# ============================================================
def get_gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    # GOOGLE_CREDENTIALS เป็น JSON string — parse โดยตรง
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds      = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def load_watchlist(gc) -> list[str]:
    """ดึง Ticker จาก Google Sheets คอลัมน์ A"""
    sh      = gc.open_by_key(SPREADSHEET_ID)
    ws      = sh.worksheet(WATCHLIST_SHEET)
    vals    = ws.col_values(TICKER_COL)
    tickers = [
        v.strip().upper()
        for v in vals[SKIP_ROWS:]
        if v.strip() and v.strip().upper() not in ("TICKER", "")
        and v.strip().upper().endswith(".BK")
    ]
    log.info(f"โหลด Watchlist จาก Sheets: {len(tickers)} หุ้น")
    return tickers


def write_scanlog(gc, rows: list):
    """เขียนผล scan ลง ScanLog sheet"""
    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sh.worksheet("ScanLog")
        except:
            ws = sh.add_worksheet(title="ScanLog", rows=2000, cols=10)
            ws.append_row(["Date","Ticker","Bucket","Close","EMA12","EMA26","ATR Level","Signal","Sent?"])
        for row in rows:
            ws.append_row(row)
        log.info(f"เขียน ScanLog {len(rows)} แถว")
    except Exception as e:
        log.warning(f"เขียน ScanLog ไม่ได้: {e}")


# ============================================================
# DATA & INDICATORS
# ============================================================
def fetch_ohlcv(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 40:
            log.warning(f"{ticker}: ข้อมูลน้อยเกินไป ({len(df)} rows)")
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df[["Open","High","Low","Close","Volume"]]
    except Exception as e:
        log.warning(f"{ticker}: fetch failed — {e}")
        return None


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    pc = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - pc).abs(),
        (df["Low"]  - pc).abs(),
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
    idx = t0.name
    detail = {
        "date":      idx.strftime("%d %b %Y") if hasattr(idx,"strftime") else str(idx),
        "close":     round(float(t0["Close"]),    2),
        "ema_fast":  round(float(t0["ema_fast"]), 2),
        "ema_slow":  round(float(t0["ema_slow"]), 2),
        "atr":       round(float(t0["atr"]),      2),
        "atr_level": round(atr_lvl_today,          2),
        "prev_high": round(float(t1["High"]),     2),
    }
    return {"ema_cross": ema_cross, "atr_buy": atr_buy, "detail": detail}


# ============================================================
# TELEGRAM
# ============================================================
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
    d, code = sig["detail"], ticker.replace(".BK","")
    out = [
        f"🔔 <b>SET ALERT</b>  |  <b>{code}</b>",
        f"📅 {d['date']}   ราคาปิด <b>{d['close']}</b>",
    ]
    if sig["ema_cross"]:
        out += ["",
            f"📈 <b>EMA Crossover (วันแรก)</b>",
            f"   EMA{EMA_FAST} = {d['ema_fast']}",
            f"   EMA{EMA_SLOW} = {d['ema_slow']}",
        ]
    if sig["atr_buy"]:
        out += ["",
            f"💥 <b>ATR Breakout Buy (วันแรก)</b>",
            f"   Close {d['close']}  >  ATR Level {d['atr_level']}",
            f"   (High เมื่อวาน {d['prev_high']} + ATR{ATR_PERIOD} {d['atr']})",
        ]
    if sig["ema_cross"] and sig["atr_buy"]:
        out += ["", "⭐ <b>Double Signal!</b>"]
    out += ["", "<i>ข้อมูลเพื่อการศึกษาเท่านั้น</i>"]
    return "\n".join(out)


# ============================================================
# MAIN
# ============================================================
def run():
    now = datetime.utcnow()
    log.info("=" * 52)
    log.info(f"SET Alert (Sheets) — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 52)

    if now.weekday() >= 5:
        send_telegram("📊 SET Alert: วันหยุดตลาด ไม่สแกน")
        return

    # เชื่อมต่อ Google Sheets
    gc       = get_gspread_client()
    watchlist = load_watchlist(gc)

    if not watchlist:
        send_telegram("⚠️ SET Alert: ไม่พบหุ้นใน Watchlist sheet")
        return

    found, skipped, sent = [], [], 0
    log_rows = []

    for ticker in watchlist:
        log.info(f"▶ {ticker}")
        df = fetch_ohlcv(ticker)
        if df is None:
            skipped.append(ticker)
            log_rows.append([now.strftime("%Y-%m-%d"), ticker, "", "", "", "", "", "fetch_failed", "—"])
            time.sleep(0.5)
            continue

        df  = compute_indicators(df)
        sig = check_signals(df)
        d   = sig["detail"]

        flags = []
        if sig["ema_cross"]: flags.append("EMA_CROSS")
        if sig["atr_buy"]:   flags.append("ATR_BUY")
        signal_str = " ".join(flags) or "none"

        log.info(f"  rows={len(df)} Close={d.get('close','?')} Signal={signal_str}")

        log_rows.append([
            now.strftime("%Y-%m-%d"),
            ticker, "",
            d.get("close",""),
            d.get("ema_fast",""),
            d.get("ema_slow",""),
            d.get("atr_level",""),
            signal_str, ""
        ])

        if sig["ema_cross"] or sig["atr_buy"]:
            found.append(ticker)
            ok = send_telegram(build_message(ticker, sig))
            if ok:
                sent += 1
                log_rows[-1][-1] = "✅"
                log.info(f"  📨 Telegram ส่งแล้ว")
            time.sleep(1)

        time.sleep(0.4)

    # สรุปประจำวัน
    if found:
        codes   = ", ".join(t.replace(".BK","") for t in found)
        summary = (f"📊 <b>SET Alert สรุป</b> {now.strftime('%d %b %Y')}\n"
                   f"สแกน {len(watchlist)} หุ้น\n"
                   f"🔔 พบสัญญาณ: <b>{codes}</b>")
    else:
        summary = (f"📊 <b>SET Alert สรุป</b> {now.strftime('%d %b %Y')}\n"
                   f"สแกน {len(watchlist)} หุ้น\n"
                   f"✅ ไม่มีสัญญาณวันนี้")

    if skipped:
        summary += f"\n⚠️ ข้าม: {', '.join(t.replace('.BK','') for t in skipped)}"

    send_telegram(summary)

    # เขียน ScanLog กลับ Sheets
    write_scanlog(gc, log_rows)

    log.info(f"Done — สัญญาณ {len(found)} | ส่ง {sent}")


if __name__ == "__main__":
    run()
