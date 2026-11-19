"""대시보드 스캘핑 탭: 템플릿 · 스캘핑 옵션 · 오늘의 왕복 매매 · 보유 종목 청산 상태 · 감시 종목 지표."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from market.clock import ET, KST, MarketClock
from report.annual import FeeParams
from strategy.ma_cross import ENTRY_MODES, MaCrossParams
from strategy.positions import ExitRule

# 스캘핑 템플릿: 조사한 일반적인 규칙을 출발점으로 둔 값 (그대로 쓰지 말고 백테스트로 조정)
TEMPLATES: dict[str, dict] = {
    "breakout": {
        "name": "스캘핑·박스 돌파", "description": "1분봉 20봉 고가를 거래량 2배로 돌파, 익절 1%·손절 0.5%",
        "values": {"entry_mode": "breakout", "bar_minutes": 1, "breakout_bars": 20, "volume_mult": 2.0,
                   "take_profit_pct": 1.0, "stop_loss_pct": 0.5, "trailing_activate_pct": 0.6, "trailing_pct": 0.3,
                   "max_hold_min": 15, "flatten_before_close_min": 10, "cooldown_sec": 60, "rebuy_wait_sec": 300,
                   "max_positions": 3, "max_buys_per_day": 10, "min_change_rate": 0.0, "max_change_rate": 10.0,
                   "min_strength": 110.0, "max_spread_pct": 0.1, "sessions": ["REGULAR"]},
    },
    "vwap": {
        "name": "스캘핑·VWAP 눌림", "description": "MA9 > MA20 상승 추세에서 VWAP 눌림 반등, 익절 0.8%·손절 0.4%",
        "values": {"entry_mode": "vwap", "bar_minutes": 1, "short_window": 9, "long_window": 20,
                   "take_profit_pct": 0.8, "stop_loss_pct": 0.4, "trailing_activate_pct": 0.5, "trailing_pct": 0.25,
                   "max_hold_min": 15, "flatten_before_close_min": 10, "cooldown_sec": 60, "rebuy_wait_sec": 300,
                   "max_positions": 3, "max_buys_per_day": 10, "min_change_rate": -5.0, "max_change_rate": 10.0,
                   "min_strength": 100.0, "max_spread_pct": 0.1, "sessions": ["REGULAR"]},
    },
    "orb": {
        "name": "스캘핑·시가 범위 돌파", "description": "정규장 첫 15분 고가를 거래량 1.5배로 돌파, 익절 1.5%·손절 0.7%",
        "values": {"entry_mode": "orb", "bar_minutes": 1, "orb_minutes": 15, "volume_mult": 1.5,
                   "take_profit_pct": 1.5, "stop_loss_pct": 0.7, "trailing_activate_pct": 1.0, "trailing_pct": 0.5,
                   "max_hold_min": 60, "flatten_before_close_min": 10, "cooldown_sec": 60, "rebuy_wait_sec": 600,
                   "max_positions": 3, "max_buys_per_day": 6, "min_change_rate": 0.0, "max_change_rate": 15.0,
                   "min_strength": 0.0, "max_spread_pct": 0.1, "sessions": ["REGULAR"]},
    },
}

SCALP_KEYS = ("entry_mode", "bar_minutes", "breakout_bars", "orb_minutes", "volume_mult", "take_profit_pct",
              "stop_loss_pct", "trailing_activate_pct", "trailing_pct", "max_hold_min", "flatten_before_close_min",
              "min_strength", "max_spread_pct", "cooldown_sec", "rebuy_wait_sec", "max_buys_per_day", "max_positions")


def round_trip_fee_pct(fees: FeeParams) -> float:
    """매수 + 매도 수수료 합 (%, FINRA 주당 수수료는 작아 제외)."""
    return fees.commission_pct * 2 + fees.sec_fee_per_million / 10_000


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(KST).isoformat(timespec="seconds") if t else None


def round_trips(orders: list, fees: FeeParams) -> list[dict]:
    """오늘 주문에서 매수 체결 → 매도 체결 짝을 찾는다 (종목별 순서대로)."""
    out, open_buy = [], {}
    for o in sorted(orders, key=lambda o: o.filled_at or o.placed_at):
        if o.filled_qty <= 0:
            continue
        meta = getattr(o.signal, "meta", None) or {}
        if o.side.value == "BUY":
            open_buy[o.symbol] = o
            continue
        b = open_buy.pop(o.symbol, None)
        entry_t = (b.filled_at or b.placed_at) if b else None
        exit_t = o.filled_at or o.placed_at
        entry_px = b.avg_fill_price if b else (o.cost_basis or 0)
        qty = o.filled_qty
        gross = (o.avg_fill_price - entry_px) * qty if entry_px else 0.0
        fee = fees.of("SELL", o.avg_fill_price * qty, qty) + (fees.of("BUY", entry_px * qty, qty) if entry_px else 0)
        bmeta = (getattr(b.signal, "meta", None) or {}) if b else {}
        mode = (bmeta.get("exit_rule") or {}).get("entry_mode", "ma_cross" if b else "")
        out.append({
            "symbol": o.symbol, "qty": qty, "entry_time": _iso(entry_t), "exit_time": _iso(exit_t),
            "hold_sec": round((exit_t - entry_t).total_seconds()) if entry_t else None,
            "entry_price": round(entry_px, 4), "exit_price": round(o.avg_fill_price, 4),
            "gross": round(gross, 2), "fees": round(fee, 2), "net": round(gross - fee, 2),
            "net_pct": round((gross - fee) / (entry_px * qty) * 100, 3) if entry_px else None,
            "exit": (o.signal.reason.split() or ["기타"])[0], "reason": o.signal.reason,
            "preset": meta.get("preset_name") or bmeta.get("preset_name", ""), "mode": mode,
            "entry_reason": b.signal.reason if b else "프로그램 밖에서 산 종목",
        })
    return out


def summarize(trips: list[dict]) -> dict:
    n = len(trips)
    wins = [t for t in trips if t["net"] > 0]
    holds = [t["hold_sec"] for t in trips if t["hold_sec"] is not None]
    gross, fee = sum(t["gross"] for t in trips), sum(t["fees"] for t in trips)
    return {"trades": n, "wins": len(wins), "win_rate": round(len(wins) / n * 100, 1) if n else None,
            "gross": round(gross, 2), "fees": round(fee, 2), "net": round(gross - fee, 2),
            "avg_hold_sec": round(sum(holds) / len(holds)) if holds else None,
            "fee_share_pct": round(fee / abs(gross) * 100) if gross else None,
            "best": max((t["net"] for t in trips), default=None), "worst": min((t["net"] for t in trips), default=None)}


def scalp_view(state, presets_items: list[dict]) -> dict:
    cfg = state.config
    fees = cfg.fees if cfg else FeeParams()
    p: MaCrossParams = state.ma_params
    rt = round_trip_fee_pct(fees)
    order_p = cfg.order if cfg else None
    warnings = []
    if p.is_scalp and p.take_profit_pct and p.take_profit_pct <= rt * 2:
        warnings.append(f"익절 {p.take_profit_pct:g}% 가 왕복 수수료 {rt:.2f}%의 2배 이하입니다. 이겨도 수수료로 대부분 사라집니다.")
    if order_p and order_p.max_buys_per_symbol == 1:
        warnings.append("주문 설정의 '종목당 하루 매수 횟수'가 1회라 같은 종목을 같은 날 다시 살 수 없습니다 "
                        "(스캘핑이면 설정 탭 > 주문에서 0 또는 2 이상).")
    if p.is_scalp and p.cooldown_sec >= 300:
        warnings.append(f"신호 간격이 {p.cooldown_sec:g}초입니다. 스캘핑 보유 종목의 청산은 막지 않지만, 같은 종목 재진입이 늦어집니다.")

    orders = state.orders.orders if state.orders else []
    trips = round_trips(orders, fees)
    now = datetime.now(KST)

    # 보유 종목 청산 상태
    positions = []
    snap = state.account.snapshot if state.account else None
    strat = state.strategy
    clock = MarketClock()
    for sym, h in sorted((snap.holdings if snap else {}).items()):
        rule = state.book.get(sym) if state.book else None
        r = rule or ExitRule.from_params(p)
        price = state._price(sym, h.current_price)
        peak = strat._peak.get(sym) if strat else None
        trail_on = bool(r.trailing_activate_pct and peak and (peak / h.avg_price - 1) * 100 >= r.trailing_activate_pct)
        bought = state.orders.last_buy_at(sym) if state.orders else None
        if bought is None and state.book and (rec := state.book.entries().get(sym)):
            try:
                bought = datetime.fromisoformat(rec["bought_at"])
            except (KeyError, ValueError):
                bought = None
        held = (now - bought).total_seconds() if bought else None
        ws = [w for w in clock.windows(now.astimezone(ET).date()) if w.session in p.sessions]
        flatten_at = (ws[-1].end - timedelta(minutes=r.flatten_before_close_min)) if ws and r.flatten_before_close_min else None
        positions.append({
            "symbol": sym, "qty": h.quantity, "avg_price": round(h.avg_price, 4), "price": round(price, 4),
            "pnl_pct": round((price / h.avg_price - 1) * 100, 3) if h.avg_price else None,
            "preset": rule.preset_name if rule else (state.presets.active_name if state.presets else ""),
            "mode": r.entry_mode, "mode_label": ENTRY_MODES.get(r.entry_mode, r.entry_mode),
            "tp_price": round(h.avg_price * (1 + r.take_profit_pct / 100), 4) if r.take_profit_pct else None,   # 끔 = None
            "sl_price": round(h.avg_price * (1 - r.stop_loss_pct / 100), 4) if r.stop_loss_pct else None,
            "peak": round(peak, 4) if peak else None, "trailing_on": trail_on,
            "trailing_stop": round(peak * (1 - r.trailing_pct / 100), 4) if trail_on else None,
            "trailing_activate_price": round(h.avg_price * (1 + r.trailing_activate_pct / 100), 4) if r.trailing_activate_pct else None,
            "held_sec": round(held) if held is not None else None,
            "time_left_sec": round(r.max_hold_min * 60 - held) if r.max_hold_min and held is not None else None,
            "flatten_at": _iso(flatten_at),
        })

    # 감시 종목 지표
    watch = []
    if strat and state.subscribed:
        for sym in state.subscribed:
            m = strat.scalp_metrics(sym)
            tick = state.ticks.get(sym)
            if not m:
                continue
            price = tick.price if tick else None
            dist = (lambda lv: round((price / lv - 1) * 100, 3) if lv and price else None)
            watch.append({"symbol": sym, "price": price, "change_rate": tick.change_rate if tick else None,
                          "vwap": round(m["vwap"], 4) if m["vwap"] else None, "vs_vwap_pct": dist(m["vwap"]),
                          "breakout_level": m["breakout_level"], "vs_breakout_pct": dist(m["breakout_level"]),
                          "orb_high": m["orb_high"], "vs_orb_pct": dist(m["orb_high"]),
                          "volume_ratio": round(m["volume_ratio"], 2) if m["volume_ratio"] else None,
                          "strength": m["strength"], "spread_pct": round(m["spread_pct"], 3) if m["spread_pct"] is not None else None,
                          "bars": m["bars"], "buy_target": sym in state.targets})

    active = {k: (list(getattr(p, k)) if k == "sessions" else getattr(p, k)) for k in SCALP_KEYS}
    active["sessions"] = [s.value for s in p.sessions]
    active["off"] = list(p.off)
    return {
        "trading": state.phase == "trading", "env": state.env,
        "preset": state.presets.active_name if state.presets else None,
        "active": active, "entry_label": ENTRY_MODES.get(p.entry_mode, p.entry_mode), "is_scalp": p.is_scalp,
        "fee": {"round_trip_pct": round(rt, 3), "commission_pct": fees.commission_pct},
        "warnings": warnings,
        "templates": [{"key": k, "name": t["name"], "description": t["description"], "values": t["values"]}
                      for k, t in TEMPLATES.items()],
        "presets": [x for x in presets_items if x["values"].get("entry_mode", "ma_cross") != "ma_cross"],
        "trips": list(reversed(trips)), "summary": summarize(trips),
        "summary_scalp": summarize([t for t in trips if t["mode"] not in ("", "ma_cross")]),
        "positions": positions, "watch": watch,
        "entry_modes": ENTRY_MODES,
    }


def template_values(key: str, base_off: list[str] | tuple[str, ...] = ()) -> tuple[str, str, dict]:
    """템플릿 값. base_off(바탕이 되는 켜진 옵션의 꺼 둔 기능) 중 템플릿이 값을 정하는 기능은 켠다."""
    t = TEMPLATES.get(key)
    if t is None:
        raise ValueError("없는 템플릿입니다")
    from config.live import FIELDS   # 순환 import 방지
    toggle_of = {f.key: f.toggle for f in FIELDS if f.section == "ma_cross" and f.toggle}
    turned_on = {toggle_of[k] for k in t["values"] if k in toggle_of}
    values = {**t["values"], "off": [n for n in base_off if n not in turned_on]}
    MaCrossParams.from_dict({**{k: v for k, v in replace(MaCrossParams()).__dict__.items()}, **values})   # 검증
    return t["name"], t["description"], values
