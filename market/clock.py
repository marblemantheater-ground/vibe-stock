"""미국 주식시장 세션 판별.

기준 시각을 America/New_York 으로 계산하므로 서머타임 전환이 자동 반영된다.
(ET 04:00 프리마켓 = 한국 17:00(서머타임) / 18:00(표준시))

휴장일·조기폐장일은 NYSE 공지를 기준으로 연 1회 갱신해야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

PRE_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
AFTER_CLOSE = time(20, 0)
EARLY_REGULAR_CLOSE = time(13, 0)
EARLY_AFTER_CLOSE = time(17, 0)

# NYSE 휴장일
HOLIDAYS = frozenset({
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
})
# 정규장 13:00 ET 조기폐장일
EARLY_CLOSES = frozenset({
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
})
COVERED_YEARS = frozenset({2026, 2027})


class Session(str, Enum):
    CLOSED = "CLOSED"
    PRE = "PRE"
    REGULAR = "REGULAR"
    AFTER = "AFTER"

    @property
    def label(self) -> str:
        return {"CLOSED": "휴장", "PRE": "프리마켓", "REGULAR": "정규장", "AFTER": "애프터마켓"}[self.value]


@dataclass(frozen=True)
class SessionWindow:
    session: Session
    start: datetime   # ET aware
    end: datetime

    def __str__(self) -> str:
        return f"{self.session.label} {fmt_kst(self.start)} ~ {fmt_kst(self.end)}"


def fmt_kst(dt: datetime) -> str:
    k, e = dt.astimezone(KST), dt.astimezone(ET)
    return f"{k:%m/%d %H:%M} KST ({e:%H:%M} ET)"


class MarketClock:
    def now(self) -> datetime:
        return datetime.now(ET)

    @staticmethod
    def is_covered(d: date) -> bool:
        return d.year in COVERED_YEARS

    @staticmethod
    def is_trading_day(d: date) -> bool:
        return d.weekday() < 5 and d not in HOLIDAYS

    def windows(self, d: date) -> list[SessionWindow]:
        """ET 날짜 d 의 세션 목록 (휴장일이면 빈 리스트)."""
        if not self.is_trading_day(d):
            return []
        early = d in EARLY_CLOSES
        at = lambda t: datetime.combine(d, t, ET)  # noqa: E731
        return [
            SessionWindow(Session.PRE, at(PRE_OPEN), at(REGULAR_OPEN)),
            SessionWindow(Session.REGULAR, at(REGULAR_OPEN), at(EARLY_REGULAR_CLOSE if early else REGULAR_CLOSE)),
            SessionWindow(Session.AFTER, at(EARLY_REGULAR_CLOSE if early else REGULAR_CLOSE),
                          at(EARLY_AFTER_CLOSE if early else AFTER_CLOSE)),
        ]

    def current(self, dt: datetime | None = None) -> SessionWindow | None:
        dt = (dt or self.now()).astimezone(ET)
        for w in self.windows(dt.date()):
            if w.start <= dt < w.end:
                return w
        return None

    def session(self, dt: datetime | None = None) -> Session:
        w = self.current(dt)
        return w.session if w else Session.CLOSED

    def last_closed_trading_day(self, dt: datetime | None = None) -> date:
        """정규장이 마감된 가장 최근 거래일 (그날 일봉이 확정된 마지막 날)."""
        dt = (dt or self.now()).astimezone(ET)
        ws = self.windows(dt.date())
        if ws and dt >= ws[1].end:
            return dt.date()
        d = dt.date() - timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def trading_day_end(self, dt: datetime | None = None) -> datetime | None:
        """dt 가 속한 거래일의 마지막 세션 종료 시각 (거래일이 아니면 None)."""
        dt = (dt or self.now()).astimezone(ET)
        ws = self.windows(dt.date())
        return ws[-1].end if ws else None

    def next_window(self, dt: datetime | None = None, session: Session | None = None) -> SessionWindow:
        """dt 이후 처음 시작하는 세션 (session 지정 시 해당 종류만)."""
        dt = (dt or self.now()).astimezone(ET)
        for offset in range(15):
            for w in self.windows(dt.date() + timedelta(days=offset)):
                if w.start > dt and (session is None or w.session == session):
                    return w
        raise RuntimeError("15일 내 거래 세션이 없습니다. 휴장일 목록을 확인하세요.")
