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


def load_screen_result(path: Path) -> list[WatchItem]:
    """종목 선정 결과 CSV (strategy/screener.py 가 저장) → 종목 목록 (거래대금 큰 순서 유지)."""
    if not path.exists():
        return []
    items: list[WatchItem] = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                symbol = (row.get("symbol") or "").strip().upper()
                exchange = (row.get("exchange") or "").strip().upper()
                if symbol and exchange in QUOTE_EXCHANGE:
                    items.append(WatchItem(symbol, exchange, (row.get("name") or "").strip()))
    except (OSError, csv.Error) as e:
        log.warning("종목 선정 결과를 읽지 못함 (%s): %s", path, e)
    return items


def merge_watchlists(groups: list[tuple[str, list[WatchItem]]], limit: int = MAX_SYMBOLS) -> list[WatchItem]:
    """여러 출처의 종목을 우선순위(앞쪽 그룹 우선) 순서로 중복 없이 합치고 limit 개로 자른다."""
    merged: dict[str, WatchItem] = {}
    summary, dropped = [], 0
    for source, items in groups:
        added = 0
        for item in items:
            if item.symbol in merged:
                continue
            if len(merged) >= limit:
                dropped += 1
                continue
            merged[item.symbol] = item
            added += 1
        if items:
            summary.append(f"{source} {added}")
    log.info("실시간 감시 종목 %d개 (%s)", len(merged), ", ".join(summary) or "없음")
    if dropped:
        log.warning("웹소켓 구독 한도(%d종목)를 넘어 %d종목은 제외 — 우선순위: 보유 > 미체결 > 관심 > 선정",
                    limit, dropped)
    return list(merged.values())
