"""급등주 선정: KIS 해외주식 상승률 순위(나스닥·뉴욕·아멕스)에서 조건에 맞는 상위 종목을 고른다.

- 프리마켓 시작부터 일정 간격(refresh_min)으로 다시 고른다. 순위는 프리마켓 가격을 실시간 반영한다 (2026-11-19 확인).
  애프터마켓에는 순위가 정규장 종가 기준이라 다시 고르지 않고 마지막 목록을 유지한다.
- 거르개: 가격 하한, 오늘 누적 거래량 하한, 등락률 하한, ETF·ETN 제외(종목 마스터 구분 3),
  워런트·유닛·권리주 제외 (마스터에서 보통주와 같은 구분이라 영문명·원래 종목+꼬리 글자 관계로 판별)
- 순위 API 는 거래소마다 등락률 높은 순 100개를 준다 (거래량 10만 주 이상 범위로 조회).

설정: config/strategy.toml [gainers] (대시보드 설정 화면 '급등주 선정'). 선정 기록: data/gainers/{env}/YYYY-MM-DD.csv
"""
from __future__ import annotations

import csv
import logging
import re
import tomllib
import zipfile
from dataclasses import dataclass, fields
from datetime import date, datetime
from pathlib import Path

from core.exceptions import KisError
from market.clock import KST
from market.watchlist import WatchItem

log = logging.getLogger(__name__)

RANK_PATH = "/uapi/overseas-stock/v1/ranking/updown-rate"
RANK_TR = "HHDFS76290000"
_RANK_EXCHANGES = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}   # 시세용 → 주문용 거래소 코드
_VOL_RANGE = "4"   # 순위 조회 거래량 범위: 4 = 10만 주 이상 (최소 거래량 거르개는 아래에서 다시 적용)

# 종목 마스터 (market/universe.py 와 같은 파일): 열 위치, 구분 2 = 주식, 3 = ETF·ETN
_MASTER_FILES = {"nas": "NASD", "nys": "NYSE", "ams": "AMEX"}
_COL_SYMBOL, _COL_KOR_NAME, _COL_ENG_NAME, _COL_TYPE = 4, 6, 7, 8
_TYPE_ETP = "3"
_DERIVATIVE_NAME = re.compile(r"\b(WTS?|WARRANTS?|UNITS?|RIGHTS?|RTS)\b|C/WTS|\bONE CL A ORD\b", re.I)
_DERIVATIVE_SUFFIX = ("WS", "W", "U", "R")


@dataclass(frozen=True)
class GainersParams:
    enabled: bool = False           # 켜면 급등주가 매수 대상이 된다
    top_n: int = 10                 # 고를 종목 수
    min_price: float = 1.0          # 현재가(USD) 하한
    min_volume: int = 500_000       # 오늘 누적 거래량(주) 하한
    min_change_rate: float = 30.0   # 전일 종가 대비 등락률(%) 하한
    exclude_etf: bool = True        # ETF·ETN(레버리지 포함) 제외
    refresh_min: int = 15           # 다시 고르는 간격(분)
    include_watchlist: bool = False # 관심 종목(watchlist.csv)·종목 선정 결과도 함께 매수 대상으로

    @classmethod
    def from_toml(cls, path: Path) -> GainersParams:
        with path.open("rb") as f:
            return cls.from_dict(tomllib.load(f).get("gainers", {}))

    @classmethod
    def from_dict(cls, raw: dict) -> GainersParams:
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"[gainers]: 알 수 없는 설정 {sorted(unknown)}")
        p = cls(**raw)
        if not 1 <= p.top_n <= 30:
            raise ValueError("[gainers] top_n 은 1~30 이어야 합니다 (실시간 구독 한도 40종목)")
        if p.min_price < 0 or p.min_volume < 0:
            raise ValueError("[gainers] 가격·거래량 하한은 0 이상이어야 합니다")
        if not 1 <= p.refresh_min <= 240:
            raise ValueError("[gainers] refresh_min 은 1~240 분이어야 합니다")
        return p


@dataclass(frozen=True)
class Gainer:
    symbol: str
    exchange: str      # 주문용 NASD / NYSE / AMEX
    name: str
    price: float
    rate: float        # 전일 대비 등락률 %
    volume: int        # 오늘 누적 거래량

    @property
    def item(self) -> WatchItem:
        return WatchItem(self.symbol, self.exchange, self.name)


@dataclass(frozen=True)
class MasterInfo:
    etp: set[str]                  # ETF·ETN
    derivative: set[str]           # 워런트·유닛·권리주


def load_master_info(cache_dir: Path) -> MasterInfo | None:
    """data/universe/ 의 종목 마스터로 ETF·워런트 등을 가린다. 파일이 없으면 None (이름 규칙만 적용)."""
    etp: set[str] = set()
    derivative: set[str] = set()
    names: dict[str, str] = {}
    found = False
    for prefix in _MASTER_FILES:
        path = cache_dir / f"{prefix}mst.cod.zip"
        if not path.exists():
            continue
        found = True
        try:
            with zipfile.ZipFile(path) as z:
                lines = z.read(z.namelist()[0]).decode("cp949", errors="replace").splitlines()
        except (OSError, zipfile.BadZipFile) as e:
            log.warning("[급등주] 종목 마스터 읽기 실패 (%s): %s", path.name, e)
            continue
        for line in lines:
            cols = line.split("\t")
            if len(cols) <= _COL_TYPE:
                continue
            sym, eng = cols[_COL_SYMBOL].strip().upper(), cols[_COL_ENG_NAME].strip()
            if not sym:
                continue
            names[sym] = eng
            if cols[_COL_TYPE] == _TYPE_ETP:
                etp.add(sym)
            elif _DERIVATIVE_NAME.search(eng) or any(ch in sym for ch in "/. "):
                derivative.add(sym)
    if not found:
        return None
    for sym, eng in names.items():   # 원래 종목 + W/U/R 꼬리 (예: MACI → MACIU) 이고 회사명 첫 단어가 같으면 파생 증권
        for suf in _DERIVATIVE_SUFFIX:
            base = sym[: -len(suf)]
            if sym.endswith(suf) and base and base in names and _first_word(names[base]) == _first_word(eng):
                derivative.add(sym)
                break
    return MasterInfo(etp, derivative)


def _first_word(name: str) -> str:
    return (name.split() or [""])[0].upper()


def _num(v: object) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def why_excluded(row: dict, p: GainersParams, master: MasterInfo | None) -> str | None:
    """순위 한 행이 거르개에 걸리는 이유 (통과하면 None)."""
    sym = str(row.get("symb") or "").strip().upper()
    price, rate, vol = _num(row.get("last")), _num(row.get("rate")), _num(row.get("tvol"))
    if not sym:
        return "종목코드 없음"
    if rate < p.min_change_rate:
        return f"등락률 {rate:+.1f}% < {p.min_change_rate:g}%"
    if price < p.min_price:
        return f"가격 ${price:g} < ${p.min_price:g}"
    if vol < p.min_volume:
        return f"거래량 {vol:,.0f} < {p.min_volume:,}"
    if master and p.exclude_etf and sym in master.etp:
        return "ETF·ETN"
    if (master and sym in master.derivative) or _DERIVATIVE_NAME.search(str(row.get("ename") or "")) \
            or any(ch in sym for ch in "/. "):
        return "워런트·유닛·권리주"
    if str(row.get("e_ordyn") or "○").strip() not in ("○", "Y", ""):
        return "주문 불가 종목"
    return None


def pick(rows_by_excd: dict[str, list[dict]], p: GainersParams, master: MasterInfo | None) -> list[Gainer]:
    """거래소별 순위 행 → 거르개 통과 종목을 등락률 높은 순으로 top_n 개."""
    picked: dict[str, Gainer] = {}
    for excd, rows in rows_by_excd.items():
        for row in rows:
            if why_excluded(row, p, master):
                continue
            sym = str(row["symb"]).strip().upper()
            g = Gainer(sym, _RANK_EXCHANGES[excd], str(row.get("name") or row.get("ename") or "").strip(),
                       _num(row.get("last")), _num(row.get("rate")), int(_num(row.get("tvol"))))
            if sym not in picked or g.rate > picked[sym].rate:
                picked[sym] = g
    return sorted(picked.values(), key=lambda g: g.rate, reverse=True)[: p.top_n]


async def fetch_rank_rows(client) -> dict[str, list[dict]]:
    """3개 거래소 상승률 순위 (오늘, 거래량 10만 주 이상). 실패한 거래소는 빠진다."""
    out: dict[str, list[dict]] = {}
    for excd in _RANK_EXCHANGES:
        try:
            r = await client.get(RANK_PATH, RANK_TR, {"EXCD": excd, "NDAY": "0", "GUBN": "1", "VOL_RANG": _VOL_RANGE,
                                                      "AUTH": "", "KEYB": ""})
        except KisError as e:
            log.warning("[급등주] %s 상승률 순위 조회 실패: %s", excd, e)
            continue
        out[excd] = r.body.get("output2") or []
    return out


class GainerLog:
    """선정 기록: data/gainers/{env}/YYYY-MM-DD.csv (선정할 때마다 종목별 1행). 장 마감 후 분봉 수집 대상으로도 쓴다."""

    FIELDS = ["picked_at", "rank", "symbol", "exchange", "name", "price", "rate", "volume"]

    def __init__(self, data_dir: Path, env: str):
        self.root = data_dir / "gainers" / env

    def path(self, d: date) -> Path:
        return self.root / f"{d.isoformat()}.csv"

    def append(self, d: date, gainers: list[Gainer]) -> None:
        if not gainers:
            return
        path = self.path(d)
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        now = datetime.now(KST).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS)
            if new:
                w.writeheader()
            for i, g in enumerate(gainers, 1):
                w.writerow({"picked_at": now, "rank": i, "symbol": g.symbol, "exchange": g.exchange, "name": g.name,
                            "price": g.price, "rate": g.rate, "volume": g.volume})

    def symbols(self, d: date) -> list[WatchItem]:
        """그날 한 번이라도 고른 종목 (처음 고른 순서)."""
        path = self.path(d)
        if not path.exists():
            return []
        items: dict[str, WatchItem] = {}
        try:
            with path.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    sym = row.get("symbol", "")
                    if sym and sym not in items and row.get("exchange") in _RANK_EXCHANGES.values():
                        items[sym] = WatchItem(sym, row["exchange"], row.get("name", ""))
        except (OSError, csv.Error) as e:
            log.warning("[급등주] 선정 기록 읽기 실패 (%s): %s", path, e)
        return list(items.values())
