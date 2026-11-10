"""분봉: 실시간 틱을 N분봉으로 집계하고, 시작 시 과거 분봉으로 채운다."""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from core.api_client import KisClient
from core.ws_client import Tick
from market.clock import KST
from market.watchlist import WatchItem

log = logging.getLogger(__name__)

_MINUTE_CHART_PATH = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
MAX_HISTORY_BARS = 120   # 분봉 조회 API 1회 최대 건수


@dataclass
class Bar:
    start: datetime   # 봉 시작 시각 (KST aware)
    open: float
    high: float
    low: float
    close: float
    volume: float


def _floor(dt: datetime, minutes: int) -> datetime:
    return dt.replace(minute=dt.minute - dt.minute % minutes, second=0, microsecond=0)


class BarBuilder:
    """한 종목의 N분봉 집계기. update() 는 봉이 마감되면 마감된 봉을 반환한다."""

    def __init__(self, minutes: int = 1, maxlen: int = 500):
        self.minutes = minutes
        self.closed: deque[Bar] = deque(maxlen=maxlen)
        self.current: Bar | None = None

    def seed(self, bars: list[Bar]) -> None:
        """과거 봉으로 채운다 (오래된 순). 이미 쌓인 봉보다 이전 것만 앞에 붙인다."""
        first = self.closed[0].start if self.closed else None
        older = [b for b in bars if first is None or b.start < first]
        self.closed.extendleft(reversed(older))

    def closes(self) -> list[float]:
        return [b.close for b in self.closed]

    def update(self, tick: Tick) -> Bar | None:
        if tick.kst_time is None or tick.price <= 0:
            return None
        start = _floor(tick.kst_time, self.minutes)
        cur = self.current
        if cur is None or start > cur.start:
            self.current = Bar(start, tick.price, tick.price, tick.price, tick.price, tick.volume)
            if cur is not None:
                self.closed.append(cur)
                return cur
            return None
        if start < cur.start:
            return None   # 늦게 도착한 이전 봉의 틱은 무시
        cur.high = max(cur.high, tick.price)
        cur.low = min(cur.low, tick.price)
        cur.close = tick.price
        cur.volume += tick.volume
        return None


async def fetch_minute_bars(client: KisClient, item: WatchItem, minutes: int = 1,
                            count: int = MAX_HISTORY_BARS) -> list[Bar]:
    """최근 N분봉 (오래된 순). 현재 진행 중인 봉은 제외한다."""
    resp = await client.get(
        _MINUTE_CHART_PATH,
        client.settings.tr_id("overseas_minute_chart"),
        {
            "AUTH": "", "EXCD": item.quote_exchange, "SYMB": item.symbol,
            "NMIN": str(minutes), "PINC": "1",   # PINC=1: 전일 포함
            "NEXT": "", "NREC": str(min(count, MAX_HISTORY_BARS)), "FILL": "", "KEYB": "",
        },
    )
    bars: list[Bar] = []
    for r in resp.body.get("output2") or []:
        try:
            start = datetime.strptime(r["kymd"] + r["khms"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
            bars.append(Bar(start, float(r["open"]), float(r["high"]), float(r["low"]),
                            float(r["last"]), float(r.get("evol") or 0)))
        except (KeyError, ValueError):
            continue
    bars.sort(key=lambda b: b.start)
    now_start = _floor(datetime.now(KST), minutes)
    return [b for b in bars if b.start < now_start]
