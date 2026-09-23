#!/usr/bin/env python3
"""
삼성전자(005930.KS) 주가 데이터 + 기술적 지표 + 매크로 데이터 수집.
출력:
  data/stock_data.csv   — 삼성전자 OHLCV + 기술적 지표 (FundamentalAgent 직접 사용)
  data/macro_data.csv   — KOSPI 지수 + USD/KRW 환율 (참고용)
"""
from __future__ import annotations

import sys
import io
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import math
import pandas as pd
import yfinance as yf

WARMUP_START = "2024-07-01"   # 지표 계산용 과거 데이터 시작 (CSV에는 포함 안 됨)
START_DATE = "2025-01-01"    # CSV에 저장할 실제 시작일
END_DATE = str(date.today())
SAMSUNG_TICKER = "005930.KS"
KOSPI_TICKER = "^KS11"
USDKRW_TICKER = "USDKRW=X"


# ── 기술적 지표 계산 함수 ────────────────────────────────────────────────────

def calc_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=window - 1, min_periods=window).mean()
    avg_loss = loss.ewm(com=window - 1, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).round(4)


def calc_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = close.ewm(span=fast, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, min_periods=slow).mean()
    macd_line = (ema_fast - ema_slow).round(4)
    signal_line = macd_line.ewm(span=signal, min_periods=signal).mean().round(4)
    histogram = (macd_line - signal_line).round(4)
    return macd_line, signal_line, histogram


def calc_bollinger(
    close: pd.Series,
    window: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ma = close.rolling(window=window, min_periods=window).mean()
    std = close.rolling(window=window, min_periods=window).std()
    upper = (ma + num_std * std).round(2)
    lower = (ma - num_std * std).round(2)
    pct_b = ((close - lower) / (upper - lower).replace(0, float("nan"))).round(4)
    return upper, lower, pct_b


def calc_volatility(close: pd.Series, window: int = 20) -> pd.Series:
    log_ret = (close / close.shift(1)).apply(lambda x: math.log(x) if x > 0 else float("nan"))
    return log_ret.rolling(window=window, min_periods=2).std().round(6)


# ── 데이터 수집 ──────────────────────────────────────────────────────────────

def _exclusive_end(end_date: str) -> str:
    """Convert the user-facing inclusive end date to yfinance's exclusive end."""
    parsed = datetime.strptime(end_date, "%Y-%m-%d").date()
    return str(parsed + timedelta(days=1))


def fetch_samsung(*, end_date: str = END_DATE) -> pd.DataFrame:
    print(f"삼성전자({SAMSUNG_TICKER}) 수집 중: {WARMUP_START} ~ {end_date} (지표 계산용 워밍업 포함)")
    raw = yf.download(
        SAMSUNG_TICKER,
        start=WARMUP_START,
        end=_exclusive_end(end_date),
        auto_adjust=False,
        progress=False,
    )

    if raw.empty:
        raise RuntimeError("yfinance에서 데이터를 받지 못했습니다.")

    # MultiIndex 컬럼 처리 (yfinance 0.2+ 이슈)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    close = raw["Close"]
    volume = raw["Volume"]

    # 전체 기간으로 지표 계산 (워밍업 포함이므로 START_DATE 이후엔 NaN 없음)
    pct_chg = close.pct_change().round(6)
    volume_chg = volume.pct_change().round(6)
    ma5 = close.rolling(5, min_periods=5).mean().round(2)
    ma20 = close.rolling(20, min_periods=20).mean().round(2)
    volatility = calc_volatility(close)
    rsi = calc_rsi(close, window=14)
    macd_line, macd_sig, macd_hist = calc_macd(close)
    bb_upper, bb_lower, bb_pct = calc_bollinger(close)

    # .values 로 numpy 배열 추출 → DatetimeIndex vs RangeIndex 불일치 방지
    df = pd.DataFrame({
        "date":           raw.index.strftime("%Y-%m-%d"),
        "open":           raw["Open"].round(0).astype("Int64").values,
        "high":           raw["High"].round(0).astype("Int64").values,
        "low":            raw["Low"].round(0).astype("Int64").values,
        "close":          close.round(0).astype("Int64").values,
        "adj_close":      raw["Adj Close"].round(2).values,
        "volume":         volume.astype("Int64").values,
        "pct_chg":        pct_chg.values,
        "volume_chg":     volume_chg.values,
        "ma5":            ma5.values,
        "ma20":           ma20.values,
        "volatility_20d": volatility.values,
        "rsi_14":         rsi.values,
        "macd":           macd_line.values,
        "macd_signal":    macd_sig.values,
        "macd_hist":      macd_hist.values,
        "bb_upper":       bb_upper.values,
        "bb_lower":       bb_lower.values,
        "bb_pct":         bb_pct.values,
    })

    # 워밍업 구간 제거 — START_DATE 이후만 저장
    df = df[df["date"] >= START_DATE].reset_index(drop=True)
    print(f"  → 워밍업 제거 후 {len(df)}행 ({df['date'].iloc[0]} ~ {df['date'].iloc[-1]})")
    return df


def fetch_macro(*, end_date: str = END_DATE) -> pd.DataFrame:
    print(f"매크로 데이터 수집 중: {START_DATE} ~ {end_date}")

    exclusive_end = _exclusive_end(end_date)
    kospi_raw = yf.download(KOSPI_TICKER, start=START_DATE, end=exclusive_end, auto_adjust=False, progress=False)
    usdkrw_raw = yf.download(USDKRW_TICKER, start=START_DATE, end=exclusive_end, auto_adjust=False, progress=False)

    if isinstance(kospi_raw.columns, pd.MultiIndex):
        kospi_raw.columns = kospi_raw.columns.get_level_values(0)
    if isinstance(usdkrw_raw.columns, pd.MultiIndex):
        usdkrw_raw.columns = usdkrw_raw.columns.get_level_values(0)

    macro = pd.DataFrame()
    macro["date"] = kospi_raw.index.strftime("%Y-%m-%d")
    macro["kospi_close"] = kospi_raw["Close"].round(2).values
    macro["kospi_pct_chg"] = kospi_raw["Close"].pct_change().round(6).values

    # USD/KRW는 날짜가 다를 수 있으므로 merge
    usdkrw = pd.DataFrame({
        "date": usdkrw_raw.index.strftime("%Y-%m-%d"),
        "usdkrw": usdkrw_raw["Close"].round(2).values,
    })
    macro = macro.merge(usdkrw, on="date", how="left")
    macro["usdkrw_pct_chg"] = macro["usdkrw"].pct_change().round(6)

    print(f"  → KOSPI {len(macro)}행, USD/KRW {len(usdkrw)}행 수집 완료")
    return macro.reset_index(drop=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="삼성전자 OHLCV·기술지표와 매크로 데이터를 수집합니다."
    )
    parser.add_argument(
        "--end-date",
        default=END_DATE,
        help="수집에 포함할 마지막 날짜(YYYY-MM-DD, 기본값: 오늘)",
    )
    args = parser.parse_args(argv)
    datetime.strptime(args.end_date, "%Y-%m-%d")

    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    stock_path = data_dir / "stock_data.csv"
    macro_path = data_dir / "macro_data.csv"

    stock_df = fetch_samsung(end_date=args.end_date)
    macro_df = fetch_macro(end_date=args.end_date)

    stock_df.to_csv(stock_path, index=False, encoding="utf-8-sig")
    macro_df.to_csv(macro_path, index=False, encoding="utf-8-sig")

    print()
    print(f"저장 완료:")
    print(f"  {stock_path}  ({len(stock_df)}행, {len(stock_df.columns)}컬럼)")
    print(f"  {macro_path}  ({len(macro_df)}행, {len(macro_df.columns)}컬럼)")
    print()
    print("[stock_data.csv 컬럼]")
    for col in stock_df.columns:
        tag = " ← FundamentalAgent 사용" if col in ("date","open","high","low","close","volume","pct_chg","ma5","ma20") else ""
        print(f"  {col}{tag}")
    print()
    print("[macro_data.csv 컬럼]")
    for col in macro_df.columns:
        print(f"  {col}")
    print()
    print("다음 단계: python scripts/03_load_stock_data.py")


if __name__ == "__main__":
    main()
