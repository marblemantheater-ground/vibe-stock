"""1분봉 저장소: 백테스트용 과거 분봉을 모아 data/minute/{SYMBOL}.csv 에 쌓는다.

KIS 해외주식 분봉 조회(HHDFS76950200)는 한 번에 120봉, NEXT=1 + KEYB=가장 오래된 봉 시각(현지 YYYYMMDDHHMMSS)으로
과거 페이지를 이어 받는다. 프리마켓·정규장·애프터마켓(04:00~20:00 ET)이 모두 들어 있어 하루 약 960봉(8페이지).
서버가 주는 과거 범위가 한정돼 있으므로, 이미 저장한 시점까지만 거슬러 올라가는 방식으로 매일 조금씩 이어 붙인다
→ 오래 쓸수록 백테스트 기간이 길어진다.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from core.api_client import KisClient
from core.exceptions import KisError
from market.bars import Bar
from market.clock import ET, KST
from market.watchlist import WatchItem

log = logging.getLogger(__name__)

_PATH = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
_PAGES_PER_DAY = 9          # 하루 960봉 ÷ 120 + 여유
_FIELDS = ["kst", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class MinuteRange:
    symbol: str
    first: datetime
    last: datetime
    bars: int
    days: int


class MinuteStore:
    def __init__(self, root: Path):
        self.root = root

    def path(self, symbol: str) -> Path:
        return self.root / f"{symbol.upper()}.csv"

    def symbols(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.csv")) if self.root.exists() else []

    def load(self, symbol: str, start: datetime | None = None, end: datetime | None = None) -> list[Bar]:
        path = self.path(symbol)
        if not path.exists():
            return []
        bars = []
        with path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    t = datetime.strptime(r["kst"], "%Y%m%d%H%M").replace(tzinfo=KST)
                    if (start and t < start) or (end and t >= end):
                        continue
                    bars.append(Bar(t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                                    float(r["volume"] or 0)))
                except (KeyError, ValueError):
                    continue
        return bars

    def info(self, symbol: str) -> MinuteRange | None:
        bars = self.load(symbol)
        if not bars:
            return None
        days = len({b.start.astimezone(ET).date() for b in bars})
        return MinuteRange(symbol, bars[0].start, bars[-1].start, len(bars), days)

    def merge(self, symbol: str, new: list[Bar]) -> int:
        """새 봉을 합쳐 저장 (같은 시각이면 새 값). 늘어난 봉 수를 반환."""
        old = {b.start: b for b in self.load(symbol)}
        before = len(old)
        for b in new:
            old[b.start] = b
        rows = sorted(old.values(), key=lambda b: b.start)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(symbol)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(_FIELDS)
            for b in rows:
                w.writerow([b.start.strftime("%Y%m%d%H%M"), b.open, b.high, b.low, b.close, b.volume])
        os.replace(tmp, path)
        return len(rows) - before


async def _page(client: KisClient, item: WatchItem, keyb: str) -> list[tuple[str, Bar]]:
    resp = await client.get(_PATH, client.settings.tr_id("overseas_minute_chart"), {
        "AUTH": "", "EXCD": item.quote_exchange, "SYMB": item.symbol, "NMIN": "1", "PINC": "1",
        "NEXT": "1" if keyb else "", "NREC": "120", "FILL": "", "KEYB": keyb,
    })
    out = []
    for r in resp.body.get("output2") or []:
        try:
            start = datetime.strptime(r["kymd"] + r["khms"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
            bar = Bar(start, float(r["open"]), float(r["high"]), float(r["low"]), float(r["last"]),
                      float(r.get("evol") or 0))
        except (KeyError, ValueError):
            continue
        if bar.close > 0:
            out.append((r["xymd"] + r["xhms"], bar))
    return out


async def collect_symbol(client: KisClient, store: MinuteStore, item: WatchItem, days: int) -> int:
    """오늘부터 과거로 days 거래일치 (이미 저장된 구간에 닿으면 멈춤). 진행 중인 봉은 저장하지 않는다."""
    have = store.info(item.symbol)
    stop_at = have.last if have else None
    cutoff = datetime.now(KST) - timedelta(days=days * 7 / 5 + 3)
    now_minute = datetime.now(KST).replace(second=0, microsecond=0)
    bars: list[Bar] = []
    keyb, seen = "", set()
    for _ in range(days * _PAGES_PER_DAY):
        rows = await _page(client, item, keyb)
        fresh = [(k, b) for k, b in rows if b.start not in seen]
        if not fresh:
            break
        seen.update(b.start for _, b in fresh)
        bars.extend(b for _, b in fresh if b.start < now_minute)
        oldest_key, oldest = min(fresh, key=lambda kb: kb[1].start)
        if (stop_at and oldest.start <= stop_at) or oldest.start < cutoff:
            break
        keyb = oldest_key
    return store.merge(item.symbol, bars)


async def collect_minutes(client: KisClient, store: MinuteStore, items: list[WatchItem], days: int = 10) -> dict:
    """여러 종목 분봉 수집. 종목 하나가 실패해도 계속한다."""
    added, failed = 0, []
    for i, item in enumerate(items, 1):
        try:
            n = await collect_symbol(client, store, item, days)
        except KisError as e:
            failed.append(item.symbol)
            log.warning("[분봉 수집] %s 실패: %s", item.symbol, e)
            continue
        added += n
        info = store.info(item.symbol)
        log.info("[분봉 수집] (%d/%d) %s +%d봉 → %s ~ %s (%d일)", i, len(items), item.symbol, n,
                 info.first.astimezone(ET).date() if info else "-", info.last.astimezone(ET).date() if info else "-",
                 info.days if info else 0)
        await asyncio.sleep(0)
    return {"symbols": len(items), "added": added, "failed": failed}
