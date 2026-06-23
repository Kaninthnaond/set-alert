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

# SuperTrend — ตรงกับ indicator TradingView (CDC ActionZone) ที่ใช้งาน
SUPERTREND_PERIOD = 10    # ATR Period ของ SuperTrend
SUPERTREND_MULT   = 3.0   # Multiplier (hl2 ± ATR × ค่านี้)

RVOL_LOOKBACK        = 20    # จำนวนวันย้อนหลังที่ใช้คำนวณวอลุ่มเฉลี่ย (ไม่รวมวันล่าสุด)
RVOL_ALERT_THRESHOLD = 1.5   # วอลุ่มวันนี้ >= ค่านี้ x ค่าเฉลี่ย ถือว่า "ผิดปกติ" น่าสนใจ

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
            ws = sh.add_worksheet(title="ScanLog", rows=2000, cols=11)
            ws.append_row(["Date","Ticker","Bucket","Close","EMA12","EMA26","ATR Level","RVOL","Signal","Sent?"])
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
    # EMA สัญญาณ CDC
    df["ema_fast"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()

    # ATR (Wilder's RMA) — ใช้สำหรับ SuperTrend
    pc = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - pc).abs(),
        (df["Low"]  - pc).abs(),
    ], axis=1).max(axis=1)
    atr_st = tr.ewm(alpha=1/SUPERTREND_PERIOD, adjust=False).mean()

    # SuperTrend — ตรงกับ ta.atr + สูตร TradingView Pine Script
    hl2         = (df["High"] + df["Low"]) / 2
    basic_upper = (hl2 + SUPERTREND_MULT * atr_st).values
    basic_lower = (hl2 - SUPERTREND_MULT * atr_st).values
    close_arr   = df["Close"].values

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    trend       = [1] * len(df)

    for i in range(1, len(df)):
        # Final upper band: ยึดค่าเดิมถ้าราคายังอยู่ต่ำกว่า
        if basic_upper[i] < final_upper[i-1] or close_arr[i-1] > final_upper[i-1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i-1]
        # Final lower band: ยึดค่าเดิมถ้าราคายังอยู่สูงกว่า
        if basic_lower[i] > final_lower[i-1] or close_arr[i-1] < final_lower[i-1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i-1]
        # Trend direction
        if trend[i-1] == -1 and close_arr[i] > final_upper[i-1]:
            trend[i] = 1
        elif trend[i-1] == 1 and close_arr[i] < final_lower[i-1]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]

    df["st_trend"] = trend
    df["st_upper"] = final_upper
    df["st_lower"] = final_lower

    return df.dropna().reset_index(drop=True)


def compute_rvol(df: pd.DataFrame) -> float | None:
    """RVOL = วอลุ่มล่าสุด / วอลุ่มเฉลี่ย RVOL_LOOKBACK วันก่อนหน้า (ไม่รวมวันล่าสุด)"""
    if len(df) < RVOL_LOOKBACK + 1:
        return None
    today_vol = float(df["Volume"].iloc[-1])
    avg_vol   = float(df["Volume"].iloc[-(RVOL_LOOKBACK + 1):-1].mean())
    if avg_vol <= 0:
        return None
    return today_vol / avg_vol


def check_signals(df: pd.DataFrame) -> dict:
    empty = {"ema_cross": False, "st_buy": False, "high_rvol": False, "detail": {}}
    if len(df) < 3:
        return empty
    t0, t1 = df.iloc[-1], df.iloc[-2]

    # EMA Crossover (วันแรกที่ EMA12 ข้าม EMA26 ขึ้น)
    ema_cross = bool(
        (t0["ema_fast"] > t0["ema_slow"]) and
        (t1["ema_fast"] <= t1["ema_slow"])
    )

    # SuperTrend Buy — trend พลิกจากขาลง (-1) เป็นขาขึ้น (1)
    # ตรงกับสัญญาณ Ⓑ ใน TradingView indicator
    st_buy = bool(
        int(t0["st_trend"]) == 1 and int(t1["st_trend"]) == -1
    )

    rvol      = compute_rvol(df)
    high_rvol = bool(rvol is not None and rvol >= RVOL_ALERT_THRESHOLD)

    idx = t0.name
    detail = {
        "date":      idx.strftime("%d %b %Y") if hasattr(idx, "strftime") else str(idx),
        "close":     round(float(t0["Close"]),    2),
        "ema_fast":  round(float(t0["ema_fast"]), 2),
        "ema_slow":  round(float(t0["ema_slow"]), 2),
        "st_line":   round(float(t0["st_lower"]) if t0["st_trend"] == 1 else float(t0["st_upper"]), 2),
        "st_trend":  "▲ Uptrend" if t0["st_trend"] == 1 else "▼ Downtrend",
        "rvol":      round(rvol, 2) if rvol is not None else None,
    }
    return {"ema_cross": ema_cross, "st_buy": st_buy, "high_rvol": high_rvol, "detail": detail}


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

    # กรณีมีแต่ RVOL ผิดปกติ ไม่มีสัญญาณราคา — ใช้ข้อความแบบสั้น แยกชัดจากสัญญาณซื้อ
    if sig["high_rvol"] and not sig["ema_cross"] and not sig["st_buy"]:
        out = [
            f"📦 <b>VOLUME SPIKE</b>  |  <b>{code}</b>",
            f"📅 {d['date']}   ราคาปิด <b>{d['close']}</b>",
            "",
            f"🔥 RVOL <b>{d['rvol']}x</b> (วอลุ่มสูงกว่าค่าเฉลี่ย {RVOL_LOOKBACK} วัน)",
            "",
            "<i>วอลุ่มผิดปกติ ยังไม่มีสัญญาณ EMA/SuperTrend — เฝ้าดูเพิ่มเติม</i>",
        ]
        return "\n".join(out)

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
    if sig["st_buy"]:
        out += ["",
            f"Ⓑ <b>SuperTrend Buy (วันแรก)</b>",
            f"   SuperTrend พลิกขาขึ้น | แนวรับ {d['st_line']}",
            f"   (ATR{SUPERTREND_PERIOD} × {SUPERTREND_MULT}  ตรงกับ TradingView)",
        ]
    if d.get("rvol") is not None:
        tag = " 🔥" if sig["high_rvol"] else ""
        out += ["", f"📦 RVOL {d['rvol']}x{tag}"]
    if sig["ema_cross"] and sig["st_buy"]:
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

    found, vol_only, skipped, sent = [], [], [], 0
    log_rows = []

    for ticker in watchlist:
        log.info(f"▶ {ticker}")
        df = fetch_ohlcv(ticker)
        if df is None:
            skipped.append(ticker)
            log_rows.append([now.strftime("%Y-%m-%d"), ticker, "", "", "", "", "", "", "fetch_failed", "—"])
            time.sleep(0.5)
            continue

        df  = compute_indicators(df)
        sig = check_signals(df)
        d   = sig["detail"]

        flags = []
        if sig["ema_cross"]: flags.append("EMA_CROSS")
        if sig["st_buy"]:    flags.append("SUPER_TREND")
        if sig["high_rvol"]: flags.append("HIGH_RVOL")
        signal_str = " ".join(flags) or "none"

        log.info(f"  rows={len(df)} Close={d.get('close','?')} RVOL={d.get('rvol','?')} Signal={signal_str}")

        log_rows.append([
            now.strftime("%Y-%m-%d"),
            ticker, "",
            d.get("close",""),
            d.get("ema_fast",""),
            d.get("ema_slow",""),
            d.get("st_line",""),
            d.get("rvol",""),
            signal_str, ""
        ])

        if sig["ema_cross"] or sig["st_buy"] or sig["high_rvol"]:
            if sig["ema_cross"] or sig["st_buy"]:
                found.append(ticker)
            else:
                vol_only.append(ticker)
            ok = send_telegram(build_message(ticker, sig))
            if ok:
                sent += 1
                log_rows[-1][-1] = "✅"
                log.info(f"  📨 Telegram ส่งแล้ว")
            time.sleep(1)

        time.sleep(0.4)

    # สรุปประจำวัน
    if found or vol_only:
        parts = []
        if found:
            parts.append(f"🔔 สัญญาณ: <b>{', '.join(t.replace('.BK','') for t in found)}</b>")
        if vol_only:
            parts.append(f"📦 วอลุ่มผิดปกติ: <b>{', '.join(t.replace('.BK','') for t in vol_only)}</b>")
        summary = (f"📊 <b>SET Alert สรุป</b> {now.strftime('%d %b %Y')}\n"
                   f"สแกน {len(watchlist)} หุ้น\n" + "\n".join(parts))
    else:
        summary = (f"📊 <b>SET Alert สรุป</b> {now.strftime('%d %b %Y')}\n"
                   f"สแกน {len(watchlist)} หุ้น\n"
                   f"✅ ไม่มีสัญญาณวันนี้")

    if skipped:
        summary += f"\n⚠️ ข้าม: {', '.join(t.replace('.BK','') for t in skipped)}"

    send_telegram(summary)

    # เขียน ScanLog กลับ Sheets
    write_scanlog(gc, log_rows)

    log.info(f"Done — สัญญาณราคา {len(found)} | วอลุ่มผิดปกติ {len(vol_only)} | ส่ง {sent}")


if __name__ == "__main__":
    run()
