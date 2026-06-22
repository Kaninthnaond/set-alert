"""
SET Backtest — ทดสอบย้อนหลังระบบ EMA Crossover + ATR Breakout
ใช้สัญญาณชุดเดียวกับ set_alert_final.py แต่จำลองย้อนหลังเพื่อหา
Win Rate / Expectancy / Profit Factor ก่อนตัดสินใจปรับระบบจริง

รันแบบ manual เท่านั้น (workflow_dispatch) ไม่ใช่ตารางประจำวัน
"""
import os, sys, time, logging, json
import pandas as pd
import yfinance as yf
import requests
from datetime import datetime

# ============================================================
# CONFIG — ใช้ GitHub Secrets ชุดเดียวกับ set_alert_final.py
# ============================================================
SPREADSHEET_ID    = os.environ["SPREADSHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDENTIALS"]
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")

WATCHLIST_SHEET = "Watchlist"
TICKER_COL      = 1
SKIP_ROWS       = 2

EMA_FAST   = 12
EMA_SLOW   = 26
ATR_PERIOD = 14

# ---- สมมติฐานการเทรดจำลอง (ปรับได้ตรงนี้) ----
BACKTEST_PERIOD = "2y"   # ดึงข้อมูลย้อนหลังกี่ปีมาทดสอบ
MIN_BARS        = 60     # ต้องมีแท่งราคาขั้นต่ำเท่านี้ถึงจะทดสอบ
STOP_ATR_MULT   = 1.0   # stop = entry − ATR(วันสัญญาณ) × ค่านี้
TARGET_RR       = 1.5    # take-profit = ระยะ stop × ค่านี้ (R:R เป้าหมาย)
MAX_HOLD_DAYS   = 5     # ถ้ายังไม่โดน stop/target ภายในกี่วันทำการ ให้ปิดที่ราคาปิด

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
# GOOGLE SHEETS
# ============================================================
def get_gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds      = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def load_watchlist(gc) -> list[str]:
    sh   = gc.open_by_key(SPREADSHEET_ID)
    ws   = sh.worksheet(WATCHLIST_SHEET)
    vals = ws.col_values(TICKER_COL)
    tickers = [
        v.strip().upper()
        for v in vals[SKIP_ROWS:]
        if v.strip() and v.strip().upper().endswith(".BK")
    ]
    log.info(f"โหลด Watchlist: {len(tickers)} หุ้น")
    return tickers


def write_trades_sheet(gc, trades: list):
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet("Backtest_Trades")
        ws.clear()
    except Exception:
        ws = sh.add_worksheet(title="Backtest_Trades", rows=3000, cols=10)

    header = ["Ticker","Signal","Entry Date","Entry Price","Exit Date",
               "Exit Price","Exit Reason","Hold Days","R Multiple"]
    ws.append_row(header)

    rows = []
    for t in trades:
        rows.append([
            t["ticker"], t["signal"],
            str(t["entry_date"]), t["entry_price"],
            str(t["exit_date"]), t["exit_price"],
            t["exit_reason"], t["hold_days"], t["r_multiple"],
        ])
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    log.info(f"เขียน Backtest_Trades: {len(rows)} แถว")


def write_summary_sheet(gc, overall: dict, by_signal: dict):
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet("Backtest_Summary")
        ws.clear()
    except Exception:
        ws = sh.add_worksheet(title="Backtest_Summary", rows=100, cols=8)

    ws.append_row([f"SET Backtest Summary — สร้างเมื่อ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"])
    ws.append_row([])
    ws.append_row(["กลุ่ม", "จำนวนไม้", "Win Rate (%)", "Avg Win (R)", "Avg Loss (R)", "Expectancy (R)", "Profit Factor"])
    ws.append_row(["รวมทั้งหมด", overall["n_trades"], overall["win_rate"],
                    overall["avg_win_r"], overall["avg_loss_r"],
                    overall["expectancy_r"], overall["profit_factor"]])
    for name, s in by_signal.items():
        ws.append_row([name, s["n_trades"], s["win_rate"],
                        s["avg_win_r"], s["avg_loss_r"],
                        s["expectancy_r"], s["profit_factor"]])

    ws.append_row([])
    ws.append_row(["หมายเหตุ: stop=ATR×%.1f | target=stop×%.1f | ถือสูงสุด %d วันทำการ | ย้อนหลัง %s"
                    % (STOP_ATR_MULT, TARGET_RR, MAX_HOLD_DAYS, BACKTEST_PERIOD)])
    log.info("เขียน Backtest_Summary แล้ว")


# ============================================================
# DATA & INDICATORS
# ============================================================
def fetch_ohlcv(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.Ticker(ticker).history(period=BACKTEST_PERIOD, interval="1d", auto_adjust=True)
        if df.empty or len(df) < MIN_BARS:
            log.warning(f"{ticker}: ข้อมูลน้อยเกินไป ({len(df)} rows)")
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df[["Open","High","Low","Close","Volume"]]
    except Exception as e:
        log.warning(f"{ticker}: fetch failed — {e}")
        return None


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """เหมือนใน set_alert_final.py แต่เก็บคอลัมน์ Date ไว้ใช้อ้างอิงตอน backtest"""
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
    df = df.dropna()
    df["Date"] = df.index
    df = df.reset_index(drop=True)
    return df


def detect_signals(df: pd.DataFrame) -> list:
    """สแกนทุกวันในประวัติ หาแถวที่เกิด EMA crossover หรือ ATR breakout (กฎเดียวกับ set_alert_final.py)"""
    signals = []
    for i in range(2, len(df)):
        t0, t1, t2 = df.iloc[i], df.iloc[i-1], df.iloc[i-2]
        ema_cross = bool((t0["ema_fast"] > t0["ema_slow"]) and (t1["ema_fast"] <= t1["ema_slow"]))
        atr_lvl_today = float(t1["High"]) + float(t1["atr"])
        atr_lvl_prev  = float(t2["High"]) + float(t2["atr"])
        atr_buy = bool((float(t0["Close"]) > atr_lvl_today) and (float(t1["Close"]) <= atr_lvl_prev))
        if ema_cross or atr_buy:
            kind = "+".join([k for k, v in [("EMA_CROSS", ema_cross), ("ATR_BUY", atr_buy)] if v])
            signals.append({"idx": i, "kind": kind, "atr": float(t0["atr"])})
    return signals


# ============================================================
# TRADE SIMULATION
# ============================================================
def simulate_trade(df: pd.DataFrame, sig_idx: int, atr_at_signal: float) -> dict | None:
    entry_idx = sig_idx + 1
    if entry_idx >= len(df):
        return None  # ไม่มีวันถัดไปให้เข้าซื้อ (สัญญาณอยู่ปลายข้อมูลพอดี)

    entry_price = float(df.iloc[entry_idx]["Open"])
    stop_price  = entry_price - atr_at_signal * STOP_ATR_MULT
    risk        = entry_price - stop_price
    if risk <= 0:
        return None
    target_price = entry_price + risk * TARGET_RR

    exit_price, exit_reason, exit_idx = None, None, None
    last_idx = min(entry_idx + MAX_HOLD_DAYS, len(df) - 1)

    for j in range(entry_idx, last_idx + 1):
        bar = df.iloc[j]
        hit_stop   = float(bar["Low"])  <= stop_price
        hit_target = float(bar["High"]) >= target_price
        if hit_stop:  # ถ้าวันเดียวกันโดนทั้งคู่ ให้ stop มาก่อน (อนุรักษ์นิยม)
            exit_price, exit_reason, exit_idx = stop_price, "stop", j
            break
        elif hit_target:
            exit_price, exit_reason, exit_idx = target_price, "target", j
            break

    if exit_price is None:
        exit_idx = last_idx
        exit_price = float(df.iloc[exit_idx]["Close"])
        exit_reason = "time"

    pnl_per_share = exit_price - entry_price
    r_multiple    = pnl_per_share / risk

    return {
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "entry_date": df.iloc[entry_idx]["Date"].strftime("%Y-%m-%d"),
        "entry_price": round(entry_price, 2),
        "exit_date": df.iloc[exit_idx]["Date"].strftime("%Y-%m-%d"),
        "exit_price": round(exit_price, 2),
        "exit_reason": exit_reason,
        "hold_days": exit_idx - entry_idx,
        "r_multiple": round(r_multiple, 3),
    }


def backtest_ticker(ticker: str) -> list:
    raw = fetch_ohlcv(ticker)
    if raw is None:
        return []
    df   = compute_indicators(raw)
    sigs = detect_signals(df)

    trades, blocked_until = [], -1
    for s in sigs:
        if s["idx"] <= blocked_until:
            continue  # ยังถือสถานะจากไม้ก่อนหน้าอยู่ ข้ามสัญญาณซ้อนทับ
        t = simulate_trade(df, s["idx"], s["atr"])
        if t is None:
            continue
        t["ticker"] = ticker
        t["signal"] = s["kind"]
        trades.append(t)
        blocked_until = t["exit_idx"]
    return trades


# ============================================================
# STATS
# ============================================================
def summarize(trades: list) -> dict | None:
    if not trades:
        return None
    df = pd.DataFrame(trades)
    n = len(df)
    wins   = df[df["r_multiple"] > 0]
    losses = df[df["r_multiple"] <= 0]
    win_rate   = round(len(wins) / n * 100, 1)
    avg_win_r  = round(wins["r_multiple"].mean(), 2) if len(wins) else 0.0
    avg_loss_r = round(losses["r_multiple"].mean(), 2) if len(losses) else 0.0
    expectancy = round(df["r_multiple"].mean(), 3)
    loss_sum = abs(losses["r_multiple"].sum())
    profit_factor = round(wins["r_multiple"].sum() / loss_sum, 2) if loss_sum > 0 else "∞"
    return {
        "n_trades": n, "win_rate": win_rate,
        "avg_win_r": avg_win_r, "avg_loss_r": avg_loss_r,
        "expectancy_r": expectancy, "profit_factor": profit_factor,
    }


def build_telegram_summary(overall: dict, by_signal: dict, n_tickers: int) -> str:
    out = [
        "📐 <b>SET Backtest Summary</b>",
        f"ทดสอบย้อนหลัง {BACKTEST_PERIOD} | {n_tickers} หุ้นใน Watchlist",
        f"กฎ: stop=ATR×{STOP_ATR_MULT} | target=stop×{TARGET_RR} | ถือสูงสุด {MAX_HOLD_DAYS} วัน",
        "",
        f"รวมทั้งหมด: {overall['n_trades']} ไม้",
        f"Win Rate: <b>{overall['win_rate']}%</b>",
        f"Avg Win: {overall['avg_win_r']}R | Avg Loss: {overall['avg_loss_r']}R",
        f"Expectancy: <b>{overall['expectancy_r']}R / ไม้</b>",
        f"Profit Factor: <b>{overall['profit_factor']}</b>",
    ]
    if by_signal:
        out.append("")
        out.append("แยกตามประเภทสัญญาณ:")
        for name, s in by_signal.items():
            out.append(f"  • {name}: {s['n_trades']} ไม้ | Win {s['win_rate']}% | Exp {s['expectancy_r']}R")
    out += ["", "<i>ข้อมูลเพื่อการศึกษาเท่านั้น ไม่ใช่คำแนะนำการลงทุน</i>"]
    return "\n".join(out)


def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("ไม่ได้ตั้ง Telegram secrets — ข้ามการส่ง")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        log.warning(f"Telegram error: {e}")


# ============================================================
# MAIN
# ============================================================
def run():
    log.info("=" * 52)
    log.info(f"SET Backtest — period={BACKTEST_PERIOD}")
    log.info("=" * 52)

    gc = get_gspread_client()
    watchlist = load_watchlist(gc)
    if not watchlist:
        log.warning("ไม่พบหุ้นใน Watchlist")
        return

    all_trades = []
    for ticker in watchlist:
        log.info(f"▶ {ticker}")
        trades = backtest_ticker(ticker)
        log.info(f"  พบ {len(trades)} ไม้")
        all_trades.extend(trades)
        time.sleep(0.4)

    if not all_trades:
        send_telegram("📐 SET Backtest: ไม่พบไม้เทรดจากสัญญาณในช่วงที่ทดสอบ")
        log.info("ไม่พบไม้เทรดเลย จบการทำงาน")
        return

    overall = summarize(all_trades)

    by_signal = {}
    df_all = pd.DataFrame(all_trades)
    for sig_name in sorted(df_all["signal"].unique()):
        subset_trades = [t for t in all_trades if t["signal"] == sig_name]
        s = summarize(subset_trades)
        if s:
            by_signal[sig_name] = s

    write_trades_sheet(gc, all_trades)
    write_summary_sheet(gc, overall, by_signal)
    send_telegram(build_telegram_summary(overall, by_signal, len(watchlist)))

    log.info(f"Done — รวม {len(all_trades)} ไม้ | Expectancy {overall['expectancy_r']}R | Win Rate {overall['win_rate']}%")


if __name__ == "__main__":
    run()
