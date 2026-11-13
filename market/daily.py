"""일봉: 해외주식 기간별시세(HHDFS76240000)를 연속조회해 원하는 개수만큼 과거 일봉을 모은다.

한 번에 최대 100일치가 오고(최신순), 다음 페이지는 기준일자(BYMD)를 앞 페이지의 가장 오래된 날짜로
바꿔 요청한다. 경계 날짜가 두 페이지에 모두 들어오므로 날짜로 중복을 제거한다.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from core.api_client import KisClient
from core.exceptions import KisError
from market.watchlist import WatchItem

log = logging.getLogger(__name__)

_DAILY_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"
_TR_DAILY = "HHDFS76240000"   # 실전·모의 공통
_PAGE_SIZE = 100
_MAX_PAGES = 30               # 3,000일 ≈ 12년. 서버가 같은 페이지를 반복해도 끝없이 돌지 않게
_OUTAGE_FAILURES = 10        # 연속 실패가 이만큼이면 서버 장애·점검으로 보고 잠시 멈춤
_OUTAGE_PAUSE_SEC = 60.0
_MAX_OUTAGE_PAUSES = 10       # 장애가 이만큼(약 10분) 이어지면 중단 → 남은 종목은 다음 실행 때 이어서
_ITEM_ATTEMPTS = 2            # 종목별 시도 횟수 (실패하면 목록 끝으로 보내 한 번 더)
DEFAULT_DAILY_COUNT = 190     # 120일선 + 기울기·과거 구간 비교용. 2페이지(100 + 99일, 경계 1일 중복)로 받을 수 있는 양


@dataclass(frozen=True)
class DailyBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float     # 거래량 (주)
    amount: float     # 거래대금 (USD)


def _parse(row: dict) -> DailyBar | None:
    try:
        bar = DailyBar(
            date=datetime.strptime(row["xymd"], "%Y%m%d").date(),
            open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
            close=float(row["clos"]), volume=float(row.get("tvol") or 0), amount=float(row.get("tamt") or 0),
        )
    except (KeyError, ValueError, TypeError):
        return None
    return bar if bar.close > 0 else None   # 거래정지 등으로 비어 있는 날은 제외


async def fetch_daily_bars(client: KisClient, item: WatchItem, count: int = 200,
                           base_date: date | None = None, adjusted: bool = True) -> list[DailyBar]:
    """base_date(없으면 오늘)부터 과거로 최대 count 개 일봉 (오래된 순).

    adjusted=True 면 수정주가(액면분할·배당 반영) — 이동평균 계산에는 수정주가를 써야 분할일에 가격이 끊기지 않는다.
    상장 기간이 짧으면 count 보다 적게 돌아온다.
    """
    bars: dict[date, DailyBar] = {}
    bymd = base_date.strftime("%Y%m%d") if base_date else ""   # 빈 값 = 오늘부터
    for _ in range(_MAX_PAGES):
        resp = await client.get(_DAILY_PATH, _TR_DAILY, {
            "AUTH": "", "EXCD": item.quote_exchange, "SYMB": item.symbol,
            "GUBN": "0",                      # 0: 일, 1: 주, 2: 월
            "BYMD": bymd, "MODP": "1" if adjusted else "0",
        })
        page = [b for b in map(_parse, resp.body.get("output2") or []) if b]
        new = [b for b in page if b.date not in bars]
        bars.update((b.date, b) for b in new)
        if len(bars) >= count or len(page) < _PAGE_SIZE // 2 or not new:
            break   # 목표 달성 / 상장 초기까지 도달 / 더 이상 새 데이터 없음
        bymd = min(b.date for b in page).strftime("%Y%m%d")
    else:
        log.warning("[%s] 일봉 연속조회가 %d페이지를 넘어 중단", item.symbol, _MAX_PAGES)

    ordered = sorted(bars.values(), key=lambda b: b.date)
    return ordered[-count:]


# ── 저장 (data/daily/{거래소}/{종목}.csv) ───────────────────
_CSV_FIELDS = ["date", "open", "high", "low", "close", "volume", "amount"]
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def daily_csv_path(root: Path, item: WatchItem) -> Path:
    # 'BRK/B' 같은 기호와 Windows 예약어(PRN, CON 등 실제 티커 존재)를 파일명으로 쓸 수 있게 변환
    name = item.symbol.replace("/", "_").replace("\\", "_")
    if name.upper().split(".")[0] in _WINDOWS_RESERVED:
        name = "_" + name
    return root / item.exchange / f"{name}.csv"


def save_daily_csv(path: Path, bars: list[DailyBar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(_CSV_FIELDS)
        w.writerows([b.date.isoformat(), b.open, b.high, b.low, b.close, b.volume, b.amount] for b in bars)
    os.replace(tmp, path)


def load_daily_csv(path: Path) -> list[DailyBar]:
    try:
        with path.open(encoding="utf-8", newline="") as f:
            return [DailyBar(date.fromisoformat(r["date"]), float(r["open"]), float(r["high"]), float(r["low"]),
                             float(r["close"]), float(r["volume"]), float(r["amount"]))
                    for r in csv.DictReader(f)]
    except (OSError, KeyError, ValueError):
        return []   # 없거나 깨진 파일 → 다시 받는다


# ── 전체 종목 수집 ─────────────────────────────────────────
@dataclass
class CollectStats:
    total: int = 0
    fetched: int = 0
    skipped: int = 0     # 이미 최신 데이터가 있어 건너뜀
    empty: int = 0       # 일봉이 없음 (상장폐지·거래정지 등)
    failed: int = 0


async def collect_daily(client: KisClient, items: list[WatchItem], root: Path, last_day: date,
                        count: int = DEFAULT_DAILY_COUNT, concurrency: int | None = None) -> CollectStats:
    """종목마다 last_day 까지 일봉 count 개를 받아 CSV 로 저장한다.

    - 이미 last_day 까지 받아 둔 종목은 건너뛰므로, 중간에 끊겨도 다시 실행하면 이어서 진행한다.
    - 요청 간격은 KisClient 의 레이트리미터가 지킨다 (키움처럼 sleep 을 직접 넣을 필요 없음).
      응답을 기다리는 동안 다른 종목 요청을 보낼 수 있도록 여러 종목을 동시에 진행한다.
    - 한 종목이 실패해도 다음 종목으로 넘어가고, 실패한 종목은 마지막에 한 번 더 시도한다.
    - 연속으로 실패하면 서버 장애·점검으로 보고 전체 작업을 잠시 멈췄다가 이어간다
      (기다리는 동안에도 asyncio.sleep 이라 프로그램이 멈추지 않는다 — 키움의 QTest.qWait 에 해당).
    """
    stats = CollectStats(total=len(items))
    workers = concurrency or max(2, min(30, int(client.settings.max_rps * 5)))
    queue: asyncio.Queue[tuple[WatchItem, int]] = asyncio.Queue()
    for item in items:
        queue.put_nowait((item, 1))
    started = time.monotonic()
    consecutive_failures = outage_pauses = 0
    paused_until = 0.0
    aborted = False

    def progress() -> None:
        done = stats.fetched + stats.skipped + stats.empty + stats.failed
        if done % 100 and done != stats.total:
            return
        elapsed = time.monotonic() - started
        rate = (stats.fetched + stats.empty + stats.failed) / elapsed if elapsed else 0
        eta = (stats.total - done) / rate / 60 if rate else 0
        log.info(f"일봉 수집 {done:,}/{stats.total:,} (받음 {stats.fetched:,}, 최신이라 건너뜀 {stats.skipped:,}, "
                 f"데이터 없음 {stats.empty:,}, 실패 {stats.failed:,}) 남은 시간 약 {eta:.0f}분")

    async def worker() -> None:
        nonlocal consecutive_failures, outage_pauses, paused_until, aborted
        while True:
            try:
                item, attempt = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if aborted:
                stats.failed += 1
                progress()
                continue
            path = daily_csv_path(root, item)
            existing = load_daily_csv(path)
            if existing and existing[-1].date >= last_day:
                stats.skipped += 1
                progress()
                continue
            if (wait := paused_until - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            try:
                bars = await fetch_daily_bars(client, item, count, base_date=last_day)
            except KisError as e:
                consecutive_failures += 1
                outage = consecutive_failures >= _OUTAGE_FAILURES
                if outage and not aborted and time.monotonic() >= paused_until:
                    outage_pauses += 1
                    if outage_pauses > _MAX_OUTAGE_PAUSES:
                        aborted = True
                        log.error("서버 장애가 계속되어 수집을 중단합니다. 나중에 다시 실행하면 남은 종목부터 이어서 받습니다")
                    else:
                        paused_until = time.monotonic() + _OUTAGE_PAUSE_SEC
                        log.warning("연속 %d종목 실패 → 서버 장애로 보고 %.0f초 대기 후 이어서 진행 (%d/%d)",
                                    consecutive_failures, _OUTAGE_PAUSE_SEC, outage_pauses, _MAX_OUTAGE_PAUSES)
                if not aborted and (outage or attempt < _ITEM_ATTEMPTS):
                    # 장애 중 실패는 종목 탓이 아니므로 시도 횟수를 깎지 않는다
                    queue.put_nowait((item, attempt if outage else attempt + 1))
                    continue
                stats.failed += 1
                log.warning("[%s] 일봉 수집 실패: %s", item.symbol, e)
                progress()
                continue
            consecutive_failures = outage_pauses = 0
            bars = [b for b in bars if b.date <= last_day]
            if bars:
                save_daily_csv(path, bars)
                stats.fetched += 1
            else:
                stats.empty += 1
            progress()

    await asyncio.gather(*(worker() for _ in range(workers)))
    log.info(f"일봉 수집 완료: {time.monotonic() - started:,.0f}초, 저장 위치 {root}")
    return stats
