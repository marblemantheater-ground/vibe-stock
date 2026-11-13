"""미국 전체 종목 목록: 한국투자증권 공식 종목 마스터 파일(나스닥·뉴욕·아멕스)을 받아 파싱한다.

키움의 GetCodeListByMarket 에 해당. 파일은 하루 한 번만 내려받고 data/universe/ 에 보관하며,
다운로드에 실패하면 마지막으로 받아 둔 파일을 쓴다.
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, datetime
from pathlib import Path

import httpx

from market.watchlist import WatchItem

log = logging.getLogger(__name__)

_MASTER_URL = "https://new.real.download.dws.co.kr/common/master/{}mst.cod.zip"
_MARKETS = {"nas": "NASD", "nys": "NYSE", "ams": "AMEX"}   # 파일 접두어 → 주문용 거래소 코드

# 마스터 파일 열 위치 (탭 구분, cp949)
_COL_SYMBOL, _COL_KOR_NAME, _COL_ENG_NAME, _COL_TYPE, _COL_BASE_PRICE = 4, 6, 7, 8, 12
_TYPE_STOCK, _TYPE_ETP = "2", "3"   # 2: 주식, 3: ETF·ETN 등


async def _download(prefix: str, path: Path) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(_MASTER_URL.format(prefix))
        resp.raise_for_status()
    zipfile.ZipFile(io.BytesIO(resp.content)).testzip()   # 깨진 파일이면 예외 → 기존 파일 유지
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(path)


async def _master_lines(prefix: str, cache_dir: Path, refresh: bool) -> list[str]:
    path = cache_dir / f"{prefix}mst.cod.zip"
    fresh = path.exists() and datetime.fromtimestamp(path.stat().st_mtime).date() == date.today()
    if refresh or not fresh:
        try:
            await _download(prefix, path)
        except (httpx.HTTPError, zipfile.BadZipFile, OSError) as e:
            if not path.exists():
                raise
            log.warning("%s 종목 마스터 다운로드 실패 → 이전 파일 사용: %s", _MARKETS[prefix], e)
    with zipfile.ZipFile(path) as z:
        return z.read(z.namelist()[0]).decode("cp949", errors="replace").splitlines()


async def load_universe(cache_dir: Path, *, include_etf: bool = False, min_price: float = 1.0,
                        refresh: bool = False) -> list[WatchItem]:
    """미국 3개 거래소 종목 (기본: 주식만, 기준가 min_price 달러 이상)."""
    types = {_TYPE_STOCK, _TYPE_ETP} if include_etf else {_TYPE_STOCK}
    items: list[WatchItem] = []
    for prefix, exchange in _MARKETS.items():
        kept = total = 0
        for line in await _master_lines(prefix, cache_dir, refresh):
            cols = line.split("\t")
            if len(cols) <= _COL_BASE_PRICE:
                continue
            total += 1
            symbol = cols[_COL_SYMBOL].strip()
            try:
                price = float(cols[_COL_BASE_PRICE] or 0)
            except ValueError:
                price = 0.0
            if not symbol or cols[_COL_TYPE] not in types or price < min_price:
                continue
            name = cols[_COL_KOR_NAME].strip() or cols[_COL_ENG_NAME].strip()
            items.append(WatchItem(symbol, exchange, name))
            kept += 1
        log.info("%s 종목 %d개 중 %d개 선택", exchange, total, kept)
    return items
