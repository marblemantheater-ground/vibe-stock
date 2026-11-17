"""백테스트 실행·기록: data/backtest/{run_id}.json 에 결과를 남겨 화면에서 비교한다."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

from backtest.engine import Backtest, BacktestParams
from config.live import FIELDS, LiveConfig, _jsonable
from market.clock import ET, KST
from market.minute import MinuteStore

log = logging.getLogger(__name__)

# 결과 목록에 함께 보여 줄 전략 값 (실행마다 무엇을 바꿨는지 비교용)
KEY_FIELDS = ("ma_cross.bar_minutes", "ma_cross.short_window", "ma_cross.long_window", "ma_cross.min_change_rate",
              "ma_cross.max_change_rate", "ma_cross.take_profit_pct", "ma_cross.stop_loss_pct",
              "ma_cross.capital_usage_pct", "ma_cross.max_positions", "ma_cross.sessions")


class RunStore:
    def __init__(self, root: Path):
        self.root = root

    def save(self, result: dict) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
        result["id"] = run_id
        path = self.root / f"{run_id}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return run_id

    def load(self, run_id: str) -> dict | None:
        if not run_id.replace("-", "").isdigit():
            return None
        try:
            return json.loads((self.root / f"{run_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list(self, limit: int = 30) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for path in sorted(self.root.glob("*.json"), reverse=True)[:limit]:
            try:
                r = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({k: r.get(k) for k in ("id", "created_at", "period", "return_pct", "benchmark_return_pct",
                                               "max_drawdown_pct", "trades", "win_rate", "profit_factor", "fees",
                                               "realized_pnl", "settings", "overrides")}
                       | {"symbol_count": len(r.get("symbols") or [])})
        return out


def data_coverage(store: MinuteStore) -> dict:
    """저장된 분봉 현황 (종목별 기간)."""
    items = []
    for s in store.symbols():
        info = store.info(s)
        if info:
            items.append({"symbol": s, "first": info.first.astimezone(ET).date().isoformat(),
                          "last": info.last.astimezone(ET).date().isoformat(), "days": info.days, "bars": info.bars})
    first = min((i["first"] for i in items), default=None)
    last = max((i["last"] for i in items), default=None)
    return {"symbols": items, "first": first, "last": last}


def run_backtest(cfg: LiveConfig, store: MinuteStore, runs: RunStore, *, start: date, end: date,
                 symbols: list[str] | None = None, overrides: dict | None = None, start_cash: float = 100_000,
                 fill_slippage_pct: float = 0.05, progress=None) -> dict:
    """현재 설정 + overrides(설정 화면과 같은 형식, 파일에는 쓰지 않음)로 백테스트하고 결과를 저장한다."""
    if start > end:
        raise ValueError("시작일이 종료일보다 늦습니다")
    if not 1_000 <= start_cash <= 100_000_000:
        raise ValueError("시작 자금은 $1,000 ~ $100,000,000")
    if not 0 <= fill_slippage_pct <= 2:
        raise ValueError("체결 미끄러짐은 0~2%")
    params = cfg.preview(overrides or {})
    available = set(store.symbols())
    chosen = [s.upper() for s in (symbols or sorted(available))]
    missing = [s for s in chosen if s not in available]
    if missing:
        raise ValueError(f"분봉이 없는 종목: {', '.join(missing[:10])}")
    if not chosen:
        raise ValueError("분봉이 저장된 종목이 없습니다. 먼저 분봉을 수집하세요 (--collect-minute)")
    bt = BacktestParams(start, end, tuple(chosen), float(start_cash), float(fill_slippage_pct))
    result = Backtest(bt, params["ma_cross"], params["order"], params["risk"], params["fees"], store, progress).run()
    by_id = {f.id: f for f in FIELDS}
    result["settings"] = {fid: _jsonable(getattr(params[by_id[fid].section], by_id[fid].key)) for fid in KEY_FIELDS}
    result["overrides"] = {k: v for k, v in (overrides or {}).items()
                           if k in by_id and _jsonable(getattr(cfg.params(by_id[k].section), by_id[k].key)) != v}
    result["created_at"] = datetime.now(KST).isoformat(timespec="seconds")
    runs.save(result)
    log.info("[백테스트] %s ~ %s, %d종목: 수익률 %+.2f%% (단순 보유 %+.2f%%), 최대 낙폭 %.2f%%, 거래 %d건, 승률 %s",
             start, end, len(chosen), result["return_pct"], result["benchmark_return_pct"], result["max_drawdown_pct"],
             result["trades"], f"{result['win_rate']:.0f}%" if result["win_rate"] is not None else "-")
    return result
