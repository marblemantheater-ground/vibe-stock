"""모의투자 프리마켓 가상 매매 (시뮬레이션).

KIS 모의 서버는 프리마켓 주문을 받지 않는다 (40570000). 그래서 모의투자일 때 프리마켓에는
실제 전략(지금 켜진 거래 옵션 + 급등주 선정)이 낸 신호를 주문 대신 가상 계좌에서 체결한다.

- 가상 계좌 시작 금액 = 거래일 시작 때 실제 모의 계좌의 주문가능 USD
- 체결 가격: 매수는 매도호가, 매도는 매수호가 기준에서 slippage_pct 만큼 불리하게 (호가가 없으면 현재가)
- 수수료는 [fees] 설정으로 매수·매도 모두 반영
- 프리마켓이 끝나면(정규장 시작) 남은 보유 종목은 마지막 가격(−slippage)으로 모두 판 것으로 정리
- 실제 모의 계좌·손실 한도·하루 목표·정산과는 섞이지 않는다. 기록: data/sim/{env}/YYYY-MM-DD.json

설정: config/strategy.toml [premarket_sim] (대시보드 설정 '프리마켓 시뮬레이션')
"""
from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass, fields
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from broker.account import Holding
from core.ws_client import Tick
from market.clock import KST
from report.annual import FeeParams
from strategy.base import Side, Signal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SimParams:
    enabled: bool = True          # 모의투자 프리마켓에 가상 매매
    slippage_pct: float = 0.2     # 체결 가격을 호가보다 이만큼(%) 불리하게 (매수는 비싸게, 매도는 싸게)

    @classmethod
    def from_toml(cls, path: Path) -> SimParams:
        with path.open("rb") as f:
            return cls.from_dict(tomllib.load(f).get("premarket_sim", {}))

    @classmethod
    def from_dict(cls, raw: dict) -> SimParams:
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"[premarket_sim]: 알 수 없는 설정 {sorted(unknown)}")
        p = cls(**raw)
        if not 0 <= p.slippage_pct <= 10:
            raise ValueError("[premarket_sim] slippage_pct 는 0~10 % 여야 합니다")
        return p


def _now() -> datetime:
    return datetime.now(KST)


class SimStore:
    """data/sim/{env}/YYYY-MM-DD.json — 그날 가상 계좌 상태와 체결 내역."""

    def __init__(self, root: Path, env: str):
        self.dir = root / env
        self.env = env

    def path(self, d: date) -> Path:
        return self.dir / f"{d.isoformat()}.json"

    def load(self, d: date) -> dict | None:
        try:
            return json.loads(self.path(d).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("[시뮬] 기록 읽기 실패 (%s): %s", self.path(d).name, e)
            return None

    def save(self, d: date, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path(d)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def days(self) -> list[dict]:
        """날짜순 요약 목록."""
        out = []
        for p in sorted(self.dir.glob("*.json")) if self.dir.exists() else []:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append(summary(data))
        return out


def summary(data: dict) -> dict:
    trades = data.get("trades", [])
    sells = [t for t in trades if t["side"] == "SELL"]
    pnl = sum(t.get("pnl") or 0 for t in sells)
    fees = sum(t.get("fee") or 0 for t in trades)
    start = data.get("start_cash") or 0
    hold_value = sum(p["qty"] * p["last_price"] for p in data.get("positions", {}).values())
    end = (data.get("cash") or 0) + hold_value
    return {
        "date": data["date"], "closed": bool(data.get("closed")), "start_cash": round(start, 2),
        "end_value": round(end, 2), "pnl": round(end - start, 2), "realized_pnl": round(pnl, 2),
        "return_pct": round((end / start - 1) * 100, 3) if start else None, "fees": round(fees, 2),
        "buys": sum(1 for t in trades if t["side"] == "BUY"), "sells": len(sells),
        "wins": sum(1 for t in sells if (t.get("pnl") or 0) > 0), "losses": sum(1 for t in sells if (t.get("pnl") or 0) <= 0),
        "slippage_pct": data.get("slippage_pct"), "open_positions": len(data.get("positions", {})),
    }


class PremarketSim:
    """가상 계좌. 전략에는 StrategyContext 로 쓰인다 (position·position_count·buys_today·last_sell_at·entry_time)."""

    def __init__(self, store: SimStore, trading_date: date, start_cash: float,
                 fees: Callable[[], FeeParams], params: Callable[[], SimParams]):
        self.store, self.date = store, trading_date
        self._fees, self._params = fees, params
        saved = store.load(trading_date)
        if saved:   # 같은 거래일에 다시 켰으면 이어서
            self.data = saved
            log.info("[시뮬] %s 가상 계좌 이어서: 현금 $%s, 보유 %d종목%s", trading_date,
                     f"{saved['cash']:,.2f}", len(saved["positions"]), " (프리마켓 정리 끝남)" if saved.get("closed") else "")
        else:
            self.data = {"date": trading_date.isoformat(), "start_cash": round(start_cash, 2), "cash": start_cash,
                         "positions": {}, "trades": [], "closed": False, "slippage_pct": params().slippage_pct,
                         "updated_at": None}
        self.started = bool(saved)   # 프리마켓 틱을 한 번이라도 처리했나 (파일은 그때부터 만든다)

    # ── 상태 ────────────────────────────────────────────────
    @property
    def closed(self) -> bool:
        return bool(self.data.get("closed"))

    @property
    def positions(self) -> dict[str, dict]:
        return self.data["positions"]

    def holds(self, symbol: str) -> bool:
        return symbol in self.positions

    def _save(self) -> None:
        self.data["updated_at"] = _now().isoformat(timespec="seconds")
        self.store.save(self.date, self.data)

    # ── StrategyContext ────────────────────────────────────
    def position(self, symbol: str) -> Holding | None:
        p = self.positions.get(symbol)
        if not p:
            return None
        cost, value = p["qty"] * p["avg_price"], p["qty"] * p["last_price"]
        return Holding(symbol, p.get("name", ""), p["exchange"], p["qty"], p["qty"], p["avg_price"], p["last_price"],
                       cost, value, value - cost, (value / cost - 1) * 100 if cost else 0.0)

    def position_count(self) -> int:
        return len(self.positions)

    def buys_today(self) -> int:
        return sum(1 for t in self.data["trades"] if t["side"] == "BUY")

    def last_sell_at(self, symbol: str) -> datetime | None:
        for t in reversed(self.data["trades"]):
            if t["symbol"] == symbol and t["side"] == "SELL":
                return datetime.fromisoformat(t["time"])
        return None

    def entry_time(self, symbol: str) -> datetime | None:
        p = self.positions.get(symbol)
        return datetime.fromisoformat(p["bought_at"]) if p else None

    # ── 시세·체결 ───────────────────────────────────────────
    def on_tick(self, tick: Tick) -> None:
        self.started = True
        p = self.positions.get(tick.symbol)
        if p and tick.price > 0:
            p["last_price"] = tick.price

    def _slip(self) -> float:
        return self._params().slippage_pct / 100

    def execute(self, signal: Signal, tick: Tick) -> dict | None:
        """신호를 가상 체결한다. 체결 기록(dict) 또는 None (현금 부족·보유 없음)."""
        if self.closed:
            return None
        now = tick.kst_time or _now()
        meta = signal.meta or {}
        if signal.side is Side.BUY:
            quote = tick.ask if tick.ask > 0 else tick.price
            price = quote * (1 + self._slip())
            qty = int(signal.qty)
            fees = self._fees()
            while qty > 0 and qty * price + fees.of("BUY", qty * price, qty) > self.data["cash"]:
                qty -= 1
            if qty <= 0:
                log.info("[시뮬] %s 매수 신호지만 가상 현금 부족 ($%s) → 건너뜀", signal.symbol, f"{self.data['cash']:,.2f}")
                return None
            amount = qty * price
            fee = fees.of("BUY", amount, qty)
            self.data["cash"] -= amount + fee
            old = self.positions.get(signal.symbol)
            if old:   # 추가 매수: 평균 단가
                total = old["qty"] + qty
                old["avg_price"] = (old["avg_price"] * old["qty"] + amount) / total
                old["cost"] += amount + fee
                old["qty"] = total
                old["last_price"] = tick.price
            else:
                self.positions[signal.symbol] = {
                    "exchange": signal.exchange, "name": "", "qty": qty, "avg_price": price, "cost": amount + fee,
                    "last_price": tick.price, "bought_at": now.isoformat(timespec="seconds"),
                    "preset_name": meta.get("preset_name", "")}
            rec = self._record(now, signal.symbol, "BUY", qty, signal.price, quote, price, amount, fee, None,
                               signal.reason, meta.get("preset_name", ""))
        else:
            p = self.positions.get(signal.symbol)
            if not p:
                return None
            quote = tick.bid if tick.bid > 0 else tick.price
            rec = self._sell(now, signal.symbol, quote, signal.price, signal.reason)
        self._save()
        return rec

    def _sell(self, now: datetime, symbol: str, quote: float, signal_price: float, reason: str) -> dict:
        p = self.positions.pop(symbol)
        qty = p["qty"]
        price = quote * (1 - self._slip())
        amount = qty * price
        fee = self._fees().of("SELL", amount, qty)
        self.data["cash"] += amount - fee
        pnl = amount - fee - p["cost"]   # 매수·매도 수수료 모두 뺀 손익
        return self._record(now, symbol, "SELL", qty, signal_price, quote, price, amount, fee, pnl, reason,
                            p.get("preset_name", ""))

    def _record(self, now: datetime, symbol: str, side: str, qty: int, signal_price: float, quote: float,
                price: float, amount: float, fee: float, pnl: float | None, reason: str, preset: str) -> dict:
        rec = {"time": now.isoformat(timespec="seconds"), "symbol": symbol, "side": side, "qty": qty,
               "signal_price": round(signal_price, 4), "quote": round(quote, 4), "price": round(price, 4),
               "amount": round(amount, 2), "fee": round(fee, 4), "pnl": None if pnl is None else round(pnl, 2),
               "reason": reason, "preset": preset}
        self.data["trades"].append(rec)
        tail = f" 손익 {'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}" if pnl is not None else ""
        log.info("[시뮬 체결] %s %s %d주 @ $%s (호가 $%s, 불리하게 %g%%) 수수료 $%.2f%s — %s",
                 "매수" if side == "BUY" else "매도", symbol, qty, f"{price:,.4f}", f"{quote:,.4f}",
                 self.data["slippage_pct"], fee, tail, reason)
        return rec

    def close_all(self, why: str) -> None:
        """프리마켓 끝: 남은 보유 종목을 마지막 가격(−slippage)으로 모두 판 것으로 정리하고 그날 기록을 마감한다."""
        if self.closed or not self.started:
            return
        now = _now()
        for sym in list(self.positions):
            last = self.positions[sym]["last_price"]
            self._sell(now, sym, last, last, f"{why} → 마지막 가격으로 정리")
        self.data["closed"] = True
        self._save()
        s = summary(self.data)
        log.info("[시뮬 마감] %s 프리마켓 가상 매매: 시작 $%s → $%s (%s$%s, %s%%) 매수 %d · 매도 %d · 수수료 $%.2f",
                 self.date, f"{s['start_cash']:,.2f}", f"{s['end_value']:,.2f}", "+" if s["pnl"] >= 0 else "−",
                 f"{abs(s['pnl']):,.2f}", f"{s['return_pct']:+.2f}" if s["return_pct"] is not None else "–",
                 s["buys"], s["sells"], s["fees"])

    def checkpoint(self) -> None:
        """도중에 꺼질 때: 정리하지 않고 지금 상태만 저장 (같은 거래일에 다시 켜면 이어서)."""
        if self.started and not self.closed:
            self._save()

    def view(self) -> dict:
        return self.view_of(self.data)

    @staticmethod
    def view_of(data: dict) -> dict:
        """화면용: 요약 + 현금 + 가상 보유(평가손익은 수수료 포함 원가 기준) + 체결 내역."""
        return {**summary(data), "cash": round(data["cash"], 2), "updated_at": data.get("updated_at"),
                "positions": [{"symbol": k, **{f: v[f] for f in ("exchange", "qty", "avg_price", "last_price", "bought_at")},
                               "pnl": round(v["qty"] * v["last_price"] - v["cost"], 2)}
                              for k, v in data.get("positions", {}).items()],
                "trades": data.get("trades", [])}
