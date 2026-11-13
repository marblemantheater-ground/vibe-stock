"""종목 사전 선정 (그랜빌 120일선 조건): 장 마감 후 수집한 일봉으로 다음 거래일 매매 후보를 고른다.

조건 (모두 만족해야 통과):
  ① 일봉이 ma_window(120)개 이상
  ② 유동성: 종가 min_price 이상, 최근 liquidity_window 일 평균 거래대금 min_avg_amount_usd 이상
  ③ 당일 일봉이 120일선에 걸쳐 있음 (저가 <= 120일선 <= 고가)
  ④ 최근 below_days(20)일은 일봉 전체가 그날의 120일선 아래 (고가 < 120일선)
  ⑤ 그보다 과거로 거슬러 올라가며 일봉 전체가 120일선 위에 있던 날(저가 > 120일선)을 찾고
  ⑥ 그날의 120일선 < 오늘 120일선
  ⑦ 그날의 저가 < 오늘 고가
→ "120일선 위에 있다가 내려와 한동안 아래에 머물던 주가가 다시 120일선을 뚫으려는 첫 시점"
"""
from __future__ import annotations

import csv
import logging
import tomllib
from collections import Counter
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

from market.daily import DailyBar, daily_csv_path, load_daily_csv
from market.watchlist import WatchItem

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenerParams:
    ma_window: int = 120
    min_price: float = 1.0
    liquidity_window: int = 20
    min_avg_amount_usd: float = 1_000_000
    below_days: int = 20
    add_to_watchlist: bool = True   # 다음 거래일 실시간 감시 종목에 선정 결과를 추가

    @classmethod
    def from_toml(cls, path: Path) -> ScreenerParams:
        with path.open("rb") as f:
            raw = tomllib.load(f).get("screener", {})
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"{path.name} [screener]: 알 수 없는 설정 {sorted(unknown)}")
        params = cls(**raw)
        if params.ma_window < 2 or params.liquidity_window < 1 or params.below_days < 1:
            raise ValueError("ma_window >= 2, liquidity_window >= 1, below_days >= 1 이어야 합니다")
        return params


@dataclass(frozen=True)
class ScreenResult:
    item: WatchItem
    passed: bool
    reason: str               # 통과 시 요약, 탈락 시 탈락 사유 (사유별 집계에 쓰므로 짧고 일정하게)
    date: date | None = None
    close: float = 0.0
    ma: float = 0.0
    avg_amount: float = 0.0


def sma(values: list[float], n: int) -> float:
    """마지막 n 개의 단순평균. 개수가 모자라면 ValueError."""
    if len(values) < n:
        raise ValueError(f"데이터 {len(values)}개 < {n}개")
    return sum(values[-n:]) / n


def screen_bars(item: WatchItem, bars: list[DailyBar], p: ScreenerParams) -> ScreenResult:
    """bars: 오래된 순, bars[-1] 이 기준일(가장 최근 거래일)."""
    if len(bars) < p.ma_window:
        return ScreenResult(item, False, f"일봉 {p.ma_window}개 미만")
    today = bars[-1]
    ma = sma([b.close for b in bars], p.ma_window)
    avg_amount = sum(b.amount for b in bars[-p.liquidity_window:]) / min(len(bars), p.liquidity_window)
    base = dict(date=today.date, close=today.close, ma=ma, avg_amount=avg_amount)

    if today.close < p.min_price:
        return ScreenResult(item, False, f"종가 ${p.min_price:g} 미만", **base)
    if avg_amount < p.min_avg_amount_usd:
        return ScreenResult(item, False, "거래대금 부족", **base)
    if not today.low <= ma <= today.high:
        return ScreenResult(item, False, f"{p.ma_window}일선에 안 걸침", **base)

    # 하루씩 과거로 (back=1 이 어제), 그날 기준 120일선과 일봉 위치 비교
    closes = [b.close for b in bars]
    n, w = len(bars), p.ma_window
    past = None
    for back in range(1, n - w + 1):
        bar = bars[n - 1 - back]
        ma_then = sum(closes[n - back - w:n - back]) / w
        if back <= p.below_days:
            if bar.high >= ma_then:
                return ScreenResult(item, False, f"최근 {p.below_days}일 중 {w}일선 위로 간 날 있음", **base)
        elif bar.low > ma_then:
            past = (bar, ma_then)
            break
    if past is None:
        return ScreenResult(item, False, f"과거에 {w}일선 위 일봉 없음", **base)
    past_bar, past_ma = past
    if not past_ma < ma:
        return ScreenResult(item, False, f"과거 {w}일선이 오늘보다 높음", **base)
    if not past_bar.low < today.high:
        return ScreenResult(item, False, "오늘 고가가 과거 저가 이하", **base)
    return ScreenResult(
        item, True,
        f"MA{w} {ma:,.2f} 걸침, 최근 {p.below_days}일 이평선 아래, "
        f"{past_bar.date} 이평선 위(MA {past_ma:,.2f}, 저가 {past_bar.low:,.2f})", **base)


def run_screen(items: list[WatchItem], daily_root: Path, last_day: date, p: ScreenerParams,
               out_dir: Path) -> list[ScreenResult]:
    """수집된 일봉 CSV 로 전 종목을 검사하고 통과 종목을 out_dir/{last_day}.csv 로 저장한다."""
    results: list[ScreenResult] = []
    for item in items:
        bars = load_daily_csv(daily_csv_path(daily_root, item))
        if not bars:
            results.append(ScreenResult(item, False, "일봉 없음 (미수집)"))
        elif bars[-1].date < last_day:
            results.append(ScreenResult(item, False, "일봉이 최신 아님 (재수집 필요)"))
        else:
            results.append(screen_bars(item, [b for b in bars if b.date <= last_day], p))

    passed = sorted((r for r in results if r.passed), key=lambda r: r.avg_amount, reverse=True)
    log.info("종목 선정 (%s 기준): %d종목 중 %d종목 통과", last_day, len(results), len(passed))
    for reason, n in Counter(r.reason for r in results if not r.passed).most_common():
        log.info("  탈락 %5d  %s", n, reason)
    for r in passed[:20]:
        log.info(f"  ✔ {r.item.symbol:<6} {r.item.exchange:<4} 종가 ${r.close:,.2f}  "
                 f"평균거래대금 ${r.avg_amount / 1e6:,.1f}M  {r.reason}")
    if len(passed) > 20:
        log.info("  ... 외 %d종목", len(passed) - 20)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{last_day.isoformat()}.csv"
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:   # 엑셀에서 한글 깨짐 방지
        w = csv.writer(f)
        w.writerow(["symbol", "exchange", "name", "date", "close", f"ma{p.ma_window}", "avg_amount_usd", "reason"])
        for r in passed:
            w.writerow([r.item.symbol, r.item.exchange, r.item.name, r.date, r.close, round(r.ma, 4),
                        round(r.avg_amount), r.reason])
    tmp.replace(path)
    log.info("선정 결과 저장: %s", path)
    return results
