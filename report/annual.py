"""연간 정산: 수수료·세금을 반영한 해외주식 양도소득 추정.

국내 거주자 해외주식 양도소득세 (추정용 — 실제 신고는 증권사 양도세 계산 자료·세무 전문가 확인)
- 연도 구분: 결제일 기준 (미국 주식은 체결일 + 1영업일, T+1). 12월 마지막 거래일 매도는 다음 해로 넘어간다
- 양도차익(원화) = 양도가액 − 취득가액 − 필요경비(수수료 등). 매도는 매도 결제일 환율, 매수는 매수 결제일 환율로 환산
- 같은 해 손익은 서로 상계(손실이 난 매도는 이익을 줄인다). 다음 해로 이월되지 않는다
- 기본공제 연 250만원, 세율 22% (양도소득세 20% + 지방소득세 2%)
- 취득가액 계산: 선입선출(fifo, 기본) 또는 이동평균(average) — [tax] cost_method
- 다른 증권사 계좌의 해외주식 손익, 배당소득, 환전 수수료(환율 우대 차이)는 포함하지 않는다

수수료 (추정): [fees] 의 비율로 계산한다. 증권사 등급·이벤트·시기에 따라 다르므로 실제 값으로 바꿔 쓴다
- 매매 수수료: 매수·매도 금액 × commission_pct
- SEC 수수료(매도만): 매도 금액 × sec_fee_per_million / 1,000,000
- FINRA TAF(매도만): 매도 수량 × finra_taf_per_share (주문 1건당 최대 finra_taf_max)

환율: 거래일마다 정산 보고서에 남긴 KIS 환율(매수가능금액 조회의 exrt)을 쓴다. 그날 기록이 없으면 가장 가까운 날의
기록으로 추정하고 표시한다.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass, fields
from datetime import date, timedelta
from pathlib import Path

from market.clock import MarketClock
from report.settlement import SettlementStore, _num

log = logging.getLogger(__name__)

_COST_METHODS = ("fifo", "average")


@dataclass(frozen=True)
class FeeParams:
    commission_pct: float = 0.25           # 매매 수수료 (매수·매도 각각, %)
    sec_fee_per_million: float = 27.8      # SEC 수수료: 매도 금액 $1,000,000 당 $ (매도만)
    finra_taf_per_share: float = 0.000166  # FINRA TAF: 매도 1주당 $ (매도만)
    finra_taf_max: float = 8.30            # FINRA TAF 주문 1건당 상한 $

    @classmethod
    def from_toml(cls, path: Path) -> FeeParams:
        with path.open("rb") as f:
            return cls.from_dict(tomllib.load(f).get("fees", {}))

    @classmethod
    def from_dict(cls, raw: dict) -> FeeParams:
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"[fees]: 알 수 없는 설정 {sorted(unknown)}")
        p = cls(**raw)
        if not 0 <= p.commission_pct < 5:
            raise ValueError("[fees] commission_pct 는 0 이상 5 미만이어야 합니다")
        if min(p.sec_fee_per_million, p.finra_taf_per_share, p.finra_taf_max) < 0:
            raise ValueError("[fees] 수수료 값은 0 이상이어야 합니다")
        return p

    def of(self, side: str, amount: float, qty: float) -> float:
        fee = amount * self.commission_pct / 100
        if side == "SELL":
            fee += amount * self.sec_fee_per_million / 1_000_000
            fee += min(qty * self.finra_taf_per_share, self.finra_taf_max)
        return fee


@dataclass(frozen=True)
class TaxParams:
    cost_method: str = "fifo"
    basic_deduction_krw: float = 2_500_000
    tax_rate_pct: float = 22.0

    @classmethod
    def from_toml(cls, path: Path) -> TaxParams:
        with path.open("rb") as f:
            return cls.from_dict(tomllib.load(f).get("tax", {}))

    @classmethod
    def from_dict(cls, raw: dict) -> TaxParams:
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"[tax]: 알 수 없는 설정 {sorted(unknown)}")
        p = cls(**raw)
        if p.cost_method not in _COST_METHODS:
            raise ValueError(f"[tax] cost_method 는 {' / '.join(_COST_METHODS)} 중 하나여야 합니다")
        if p.basic_deduction_krw < 0 or not 0 <= p.tax_rate_pct <= 100:
            raise ValueError("[tax] basic_deduction_krw >= 0, 0 <= tax_rate_pct <= 100 이어야 합니다")
        return p


def settlement_date(trade_date: date) -> date:
    """미국 주식 결제일 = 체결일 다음 영업일 (T+1)."""
    d = trade_date + timedelta(days=1)
    while not MarketClock.is_trading_day(d):
        d += timedelta(days=1)
    return d


class RateBook:
    """날짜별 환율 (정산 보고서에 남긴 값). 그날 값이 없으면 가장 가까운 앞날 → 뒷날 → 현재 환율 순으로 추정."""

    def __init__(self, rates: dict[date, float], fallback: float | None = None):
        self._days = sorted(d for d, r in rates.items() if r and r > 0)
        self._rates = {d: rates[d] for d in self._days}
        self._fallback = fallback if fallback and fallback > 0 else None

    @classmethod
    def from_store(cls, store: SettlementStore, fallback: float | None = None) -> RateBook:
        rates = {}
        for path in sorted(store.report_path(date.today()).parent.glob("*.json")):
            try:
                rates[date.fromisoformat(path.stem)] = _num(json.loads(path.read_text(encoding="utf-8")).get("exchange_rate"))
            except (OSError, ValueError, AttributeError):
                continue
        return cls(rates, fallback)

    def rate(self, d: date) -> tuple[float | None, str]:
        """(환율, 출처) — 출처: exact(그날 기록) / near(가까운 날) / current(현재 환율) / none."""
        if d in self._rates:
            return self._rates[d], "exact"
        before = [x for x in self._days if x < d]
        if before:
            return self._rates[before[-1]], "near"
        if self._days:
            return self._rates[self._days[0]], "near"
        if self._fallback:
            return self._fallback, "current"
        return None, "none"


@dataclass
class _Lot:
    qty: float
    usd: float    # 수수료 포함 취득가 (USD)
    krw: float    # 수수료 포함 취득가 (원화, 매수 결제일 환율)


def load_fills(store: SettlementStore) -> list[dict]:
    """모든 날짜의 체결 기록 (시간순). 같은 날 같은 주문번호가 여러 번 기록됐으면(재시작) 마지막 기록만."""
    folder = store.trades_path(date.today()).parent
    fills: list[dict] = []
    for path in sorted(folder.glob("*.csv")):
        try:
            d = date.fromisoformat(path.stem)
        except ValueError:
            continue
        rows = store.load_trades(d)
        latest: dict[str, dict] = {}
        anonymous = []
        for r in rows:
            if r.get("odno"):
                latest[r["odno"].lstrip("0")] = r
            else:
                anonymous.append(r)
        for r in [*latest.values(), *anonymous]:
            if _num(r.get("filled_qty")) > 0 and _num(r.get("avg_fill_price")) > 0 and r.get("side") in ("BUY", "SELL"):
                fills.append(r)
    fills.sort(key=lambda r: (r["date"], r.get("placed_at", "")))
    return fills


def compute_annual(store: SettlementStore, year: int, fees: FeeParams, tax: TaxParams,
                   current_rate: float | None = None) -> dict:
    """year 에 결제된 매도의 양도차익·수수료·예상 세금. 매수 기록은 해와 상관없이 모두 읽어 취득가를 맞춘다."""
    rates = RateBook.from_store(store, current_rate)
    lots: dict[str, deque[_Lot]] = defaultdict(deque)
    sells: list[dict] = []
    fees_in_year = 0.0
    buy_amount = sell_amount = 0.0
    rate_flags: set[str] = set()
    missing_cost: list[str] = []

    for r in load_fills(store):
        trade_day = date.fromisoformat(r["date"])
        settle = settlement_date(trade_day)
        qty = _num(r["filled_qty"])
        price = _num(r["avg_fill_price"])
        amount = qty * price
        fee = fees.of(r["side"], amount, qty)
        rate, src = rates.rate(settle)
        in_year = settle.year == year
        if in_year:
            fees_in_year += fee
            if src != "exact":
                rate_flags.add(src)

        if r["side"] == "BUY":
            if in_year:
                buy_amount += amount
            lot = _Lot(qty, amount + fee, (amount + fee) * (rate or 0))
            if tax.cost_method == "average" and lots[r["symbol"]]:
                cur = lots[r["symbol"]][0]
                cur.qty, cur.usd, cur.krw = cur.qty + lot.qty, cur.usd + lot.usd, cur.krw + lot.krw
            else:
                lots[r["symbol"]].append(lot)
            continue

        # ── 매도: 보유 기록에서 취득가를 꺼낸다 ──
        cost_usd = cost_krw = 0.0
        need = qty
        queue = lots[r["symbol"]]
        while need > 1e-9 and queue:
            lot = queue[0]
            take = min(need, lot.qty)
            share = take / lot.qty
            cost_usd += lot.usd * share
            cost_krw += lot.krw * share
            lot.qty -= take
            lot.usd -= lot.usd * share
            lot.krw -= lot.krw * share
            need -= take
            if lot.qty <= 1e-9:
                queue.popleft()
        note = ""
        if need > 1e-9:   # 프로그램 밖(HTS·이전 기록)에서 산 수량 → 매도 시점 매입평균가로 추정
            basis = _num(r.get("cost_basis"))
            cost_usd += need * basis
            cost_krw += need * basis * (rate or 0)
            note = f"취득 기록 없는 {need:g}주는 매입평균가 ${basis:,.2f}·매도일 환율로 추정"
            if in_year:
                missing_cost.append(f"{r['date']} {r['symbol']} {need:g}주")
        if not in_year:
            continue
        sell_amount += amount
        proceeds_usd = amount - fee
        proceeds_krw = proceeds_usd * (rate or 0)
        sells.append({
            "trade_date": r["date"], "settle_date": settle.isoformat(), "symbol": r["symbol"], "qty": qty,
            "price": price, "sell_amount": round(amount, 4), "sell_fee": round(fee, 4),
            "cost_usd": round(cost_usd, 4), "gain_usd": round(proceeds_usd - cost_usd, 4),
            "rate": rate, "rate_source": src, "proceeds_krw": round(proceeds_krw), "cost_krw": round(cost_krw),
            "gain_krw": round(proceeds_krw - cost_krw), "note": note,
        })

    gain_krw = sum(s["gain_krw"] for s in sells)
    gain_usd = sum(s["gain_usd"] for s in sells)
    taxable = max(0.0, gain_krw - tax.basic_deduction_krw)
    tax_krw = round(taxable * tax.tax_rate_pct / 100)
    monthly = []
    for m in range(1, 13):
        ms = [s for s in sells if int(s["settle_date"][5:7]) == m]
        monthly.append({"month": m, "sells": len(ms), "gain_usd": round(sum(s["gain_usd"] for s in ms), 2),
                        "gain_krw": sum(s["gain_krw"] for s in ms), "fees_usd": round(sum(s["sell_fee"] for s in ms), 2)})
    open_lots = [{"symbol": sym, "qty": round(sum(l.qty for l in q), 6), "cost_usd": round(sum(l.usd for l in q), 2),
                  "cost_krw": round(sum(l.krw for l in q))}
                 for sym, q in sorted(lots.items()) if sum(l.qty for l in q) > 1e-9]

    warnings = []
    if store.env != "real":
        warnings.append("모의투자 계좌입니다. 실제 세금은 없고 계산 방식 확인용입니다.")
    if "near" in rate_flags:
        warnings.append("일부 거래는 결제일 환율 기록이 없어 가장 가까운 날의 환율로 계산했습니다.")
    if "current" in rate_flags:
        warnings.append("환율 기록이 없어 현재 환율로 계산한 거래가 있습니다.")
    if "none" in rate_flags:
        warnings.append("환율을 알 수 없어 원화 금액이 0으로 계산된 거래가 있습니다. 프로그램을 한 번 실행해 환율을 기록하세요.")
    if missing_cost:
        warnings.append(f"매수 기록이 없는 매도 {len(missing_cost)}건은 매도 시점 매입평균가로 취득가를 추정했습니다: "
                        + ", ".join(missing_cost[:5]) + (" 외" if len(missing_cost) > 5 else ""))

    return {
        "year": year, "env": store.env, "cost_method": tax.cost_method,
        "params": {"commission_pct": fees.commission_pct, "sec_fee_per_million": fees.sec_fee_per_million,
                   "finra_taf_per_share": fees.finra_taf_per_share, "basic_deduction_krw": tax.basic_deduction_krw,
                   "tax_rate_pct": tax.tax_rate_pct},
        "sell_count": len(sells), "wins": sum(s["gain_krw"] > 0 for s in sells),
        "losses": sum(s["gain_krw"] < 0 for s in sells),
        "buy_amount": round(buy_amount, 2), "sell_amount": round(sell_amount, 2),
        "fees_usd": round(fees_in_year, 2), "gain_usd": round(gain_usd, 2),
        "gain_krw": gain_krw, "deduction_krw": tax.basic_deduction_krw,
        "taxable_krw": round(taxable), "tax_krw": tax_krw, "after_tax_krw": gain_krw - tax_krw,
        "deduction_left_krw": round(max(0.0, tax.basic_deduction_krw - max(gain_krw, 0))),
        "filing": f"{year + 1}년 5월 1일 ~ 5월 31일 양도소득세 확정신고",
        "monthly": monthly, "sells": sells, "open_lots": open_lots, "warnings": warnings,
    }


def available_years(store: SettlementStore, today: date | None = None) -> list[int]:
    years = {settlement_date(date.fromisoformat(r["date"])).year for r in load_fills(store)}
    years.add((today or date.today()).year)
    return sorted(years, reverse=True)


SELL_CSV_FIELDS = [("trade_date", "체결일"), ("settle_date", "결제일"), ("symbol", "종목"), ("qty", "수량"),
                   ("price", "체결가(USD)"), ("sell_amount", "매도금액(USD)"), ("sell_fee", "매도 수수료(USD)"),
                   ("cost_usd", "취득가(USD, 수수료 포함)"), ("gain_usd", "손익(USD)"), ("rate", "적용 환율"),
                   ("rate_source", "환율 출처"), ("proceeds_krw", "양도가액(원)"), ("cost_krw", "취득가액(원)"),
                   ("gain_krw", "양도차익(원)"), ("note", "비고")]


def sells_csv(result: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in SELL_CSV_FIELDS])
    for s in result["sells"]:
        w.writerow([s[k] if s[k] is not None else "" for k, _ in SELL_CSV_FIELDS])
    return "﻿" + buf.getvalue()   # 엑셀 한글 깨짐 방지 (UTF-8 BOM)


def save_annual(store: SettlementStore, result: dict) -> Path:
    folder = store.root / "annual"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{result['year']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    path = folder / f"{result['year']}_sells.csv"
    path.write_bytes(sells_csv(result).encode("utf-8"))
    return path


def log_annual(r: dict) -> None:
    log.info(f"===== {r['year']}년 연간 정산 ({r['env']}, 취득가 {'선입선출' if r['cost_method'] == 'fifo' else '이동평균'}) =====")
    log.info(f"매도 {r['sell_count']}건 (이익 {r['wins']} / 손실 {r['losses']}), 매수 ${r['buy_amount']:,.2f} / 매도 ${r['sell_amount']:,.2f}")
    log.info(f"수수료(추정): ${r['fees_usd']:,.2f}  →  수수료 차감 손익 ${r['gain_usd']:+,.2f}")
    log.info(f"원화 양도차익: {r['gain_krw']:+,.0f}원 − 기본공제 {r['deduction_krw']:,.0f}원 = 과세표준 {r['taxable_krw']:,.0f}원")
    log.info(f"예상 세금 ({r['params']['tax_rate_pct']:g}%): {r['tax_krw']:,.0f}원  →  세후 손익 {r['after_tax_krw']:+,.0f}원")
    log.info(f"신고: {r['filing']}")
    for w in r["warnings"]:
        log.warning("참고: %s", w)
