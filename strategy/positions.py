"""보유 종목별 매도 기준 기록: 산 때 켜져 있던 거래 옵션의 익절·손절·데드크로스 기준을 다 팔 때까지 유지한다.

- 프로그램이 낸 매수가 체결되면 그 주문을 만든 옵션의 기준(ExitRule)을 data/positions/{env}.json 에 남긴다
- 나중에 옵션을 바꾸거나 옵션 값을 고쳐도, 이미 산 종목은 기록된 기준으로 판다
- 기록이 없는 보유 종목(HTS 직접 매수, 이 기능 이전 매수)은 지금 켜진 옵션 기준을 따른다
- 잔고에서 사라진 종목(전량 매도)은 기록을 지운다
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from market.clock import KST

log = logging.getLogger(__name__)

_FORGET_AFTER_SEC = 180.0   # 체결 직후 잔고 반영이 늦을 수 있어, 기록 후 이 시간 안에는 지우지 않는다


@dataclass(frozen=True)
class ExitRule:
    preset_id: str
    preset_name: str
    take_profit_pct: float
    stop_loss_pct: float
    bar_minutes: int
    short_window: int
    long_window: int
    # 스캘핑 청산 (예전 기록에는 없으므로 기본값 = 끔)
    entry_mode: str = "ma_cross"          # ma_cross 일 때만 데드크로스 매도
    trailing_activate_pct: float = 0.0
    trailing_pct: float = 0.0
    max_hold_min: float = 0.0
    flatten_before_close_min: float = 0.0
    dead_cross: bool = True               # 데드크로스 매도 켬/끔 (익절·손절 0 = 그 기준 끔)

    @classmethod
    def from_params(cls, p, preset_id: str = "", preset_name: str = "") -> ExitRule:
        p = p.effective()   # 꺼진 기능은 0 으로 기록
        return cls(preset_id, preset_name, p.take_profit_pct, p.stop_loss_pct, p.bar_minutes, p.short_window,
                   p.long_window, p.entry_mode, p.trailing_activate_pct, p.trailing_pct, p.max_hold_min,
                   p.flatten_before_close_min, p.uses("dead_cross"))

    @classmethod
    def from_dict(cls, d: dict) -> ExitRule:
        return cls(str(d.get("preset_id", "")), str(d.get("preset_name", "")), float(d["take_profit_pct"]),
                   float(d["stop_loss_pct"]), int(d["bar_minutes"]), int(d["short_window"]), int(d["long_window"]),
                   str(d.get("entry_mode", "ma_cross")), float(d.get("trailing_activate_pct", 0)),
                   float(d.get("trailing_pct", 0)), float(d.get("max_hold_min", 0)),
                   float(d.get("flatten_before_close_min", 0)), bool(d.get("dead_cross", True)))

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        parts = [f"{self.bar_minutes}분봉 MA{self.short_window}/MA{self.long_window}" if self.entry_mode == "ma_cross"
                 else f"{self.bar_minutes}분봉 스캘핑",
                 f"익절 {self.take_profit_pct:g}%" if self.take_profit_pct else "익절 끔",
                 f"손절 {self.stop_loss_pct:g}%" if self.stop_loss_pct else "손절 끔"]
        if self.entry_mode == "ma_cross" and not self.dead_cross:
            parts.append("데드크로스 끔")
        if self.trailing_activate_pct:
            parts.append(f"추적 +{self.trailing_activate_pct:g}%/−{self.trailing_pct:g}%")
        if self.max_hold_min:
            parts.append(f"최대 {self.max_hold_min:g}분")
        if self.flatten_before_close_min:
            parts.append(f"마감 {self.flatten_before_close_min:g}분 전 정리")
        return " · ".join(parts)


class PositionBook:
    def __init__(self, root: Path, env: str):
        self.path = root / f"{env}.json"
        self._data: dict[str, dict] = self._load()
        self._recorded: dict[str, float] = {}   # 이번 실행에서 기록한 시각 (monotonic)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.error("보유 종목 매도 기준 기록을 읽지 못함 (%s) → 현재 옵션 기준으로 매도: %s", self.path, e)
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, symbol: str) -> ExitRule | None:
        d = self._data.get(symbol)
        if not d:
            return None
        try:
            return ExitRule.from_dict(d["rule"])
        except (KeyError, ValueError, TypeError):
            return None

    def entries(self) -> dict[str, dict]:
        return {s: dict(d) for s, d in self._data.items()}

    def record(self, symbol: str, rule: ExitRule, bought_at: str) -> None:
        self._data[symbol] = {"rule": rule.to_dict(), "bought_at": bought_at,
                              "recorded_at": datetime.now(KST).isoformat(timespec="seconds")}
        self._recorded[symbol] = time.monotonic()
        self._save()
        log.info("[거래 옵션] %s 매수 체결 → '%s' 기준으로 매도 (%s)", symbol, rule.preset_name or "현재 옵션", rule.summary())

    def sync(self, orders: list, snapshot) -> None:
        """체결된 매수의 기준을 기록하고, 다 판 종목의 기록을 지운다. 잔고가 새로 조회될 때 부른다."""
        changed = False
        for o in orders:
            meta = getattr(o.signal, "meta", None) or {}
            if (o.side.value != "BUY" or o.adopted or o.filled_qty <= 0 or "exit_rule" not in meta
                    or o.symbol in self._data):
                continue
            try:
                rule = ExitRule.from_dict(meta["exit_rule"])
            except (KeyError, ValueError, TypeError):
                continue
            self.record(o.symbol, rule, o.placed_at.astimezone(KST).isoformat(timespec="seconds"))
        if snapshot is None:
            return
        held = {s for s, h in snapshot.holdings.items() if h.quantity > 0}
        pending_buys = {o.symbol for o in orders if o.side.value == "BUY" and o.status.is_open}
        now = time.monotonic()
        for sym in list(self._data):
            if sym in held or sym in pending_buys:
                continue
            if now - self._recorded.get(sym, -1e9) < _FORGET_AFTER_SEC:
                continue
            name = self._data[sym].get("rule", {}).get("preset_name", "")
            del self._data[sym]
            changed = True
            log.info("[거래 옵션] %s 보유 0주 → '%s' 매도 기준 기록 삭제", sym, name)
        if changed:
            self._save()
