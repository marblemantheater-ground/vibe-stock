"""매매 대상 종목 리스트 로더 (config/watchlist.csv)."""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 주문/잔고용 거래소 코드 -> 시세(실시간)용 거래소 코드
QUOTE_EXCHANGE = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
MAX_SYMBOLS = 40   # KIS 웹소켓 세션당 구독 한도(41) 이내


@dataclass(frozen=True)
class WatchItem:
    symbol: str
    exchange: str          # NASD / NYSE / AMEX
    name: str = ""

    @property
    def quote_exchange(self) -> str:
        return QUOTE_EXCHANGE[self.exchange]


def load_watchlist(path: Path) -> list[WatchItem]:
    items: dict[str, WatchItem] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for lineno, row in enumerate(csv.DictReader(f), start=2):
            symbol = (row.get("symbol") or "").strip().upper()
            exchange = (row.get("exchange") or "").strip().upper()
            if not symbol or symbol.startswith("#"):
                continue
            if exchange not in QUOTE_EXCHANGE:
                log.warning("%s:%d 거래소 코드 오류 (%s) → 건너뜀", path.name, lineno, exchange)
                continue
            if symbol in items:
                continue
            items[symbol] = WatchItem(symbol, exchange, (row.get("name") or "").strip())

    result = list(items.values())
    if len(result) > MAX_SYMBOLS:
        log.warning("종목이 %d개 → 웹소켓 구독 한도로 앞 %d개만 사용", len(result), MAX_SYMBOLS)
        result = result[:MAX_SYMBOLS]
    return result
