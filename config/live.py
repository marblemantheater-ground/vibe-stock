"""설정 즉시 적용: config/strategy.toml 의 매매 설정을 프로그램을 끄지 않고 바꾼다.

- 대시보드 설정 화면에서 저장하면 → 파일에 기록(주석·순서 그대로, 값만 교체) → 실행 중인 매매에 바로 반영
- 파일을 직접 고쳐도 몇 초 안에 감지해 같은 방식으로 반영 (잘못된 값이면 오류만 알리고 기존 값 유지)
- 적용 시점은 항목마다 다르다 (FIELDS 의 apply)
    now      : 바로 적용 (익절·손절, 등락률 범위, 주문 한도, 손실 한도 …)
    next_day : 다음 거래일부터 (봉 단위·이동평균 기간 → 그날 쌓은 봉을 다시 만들어야 해서)
    file     : 화면에서는 바꿀 수 없음 (실전 주문 허용·주문 미전송 같은 안전 스위치) → 파일 수정 + 다음 거래일
"""
from __future__ import annotations

import logging
import os
import re
import time
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable

from broker.order import OrderParams
from core.notifier import NOTIFY
from market.clock import Session
from report.annual import FeeParams, TaxParams
from broker.order import ORDER_TOGGLES
from risk.guard import RISK_TOGGLES, RiskParams
from strategy.ma_cross import TOGGLES as MA_TOGGLES, ZERO_OFF as MA_ZERO_OFF, MaCrossParams

log = logging.getLogger(__name__)

_POLL_SEC = 2.0
_SECTIONS = {"ma_cross": MaCrossParams, "order": OrderParams, "risk": RiskParams, "fees": FeeParams, "tax": TaxParams}


@dataclass(frozen=True)
class Field:
    section: str
    key: str
    label: str
    type: str                 # number | int | bool | select | sessions
    group: str
    apply: str = "now"        # now | next_day | file
    unit: str = ""
    help: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: tuple[tuple[Any, str], ...] = ()
    toggle: str = ""          # 켜고 끄는 기능 이름 (같은 이름의 칸은 함께 켜지고 꺼짐, 첫 칸에 스위치)

    @property
    def id(self) -> str:
        return f"{self.section}.{self.key}"

    @property
    def stored(self) -> bool:
        """설정 값으로 저장되는 칸인가 (스위치만 있는 칸은 off 목록으로 저장)."""
        return self.type != "toggle"


# 섹션별 켜고 끌 수 있는 기능, 0 = 끔 인 값 칸, 다시 켤 때 값이 0 이면 넣어 줄 값
TOGGLES = {"ma_cross": MA_TOGGLES, "order": ORDER_TOGGLES, "risk": RISK_TOGGLES}
ZERO_OFF = {"ma_cross": MA_ZERO_OFF, "order": {"buys_per_symbol": "max_buys_per_symbol"},
            "risk": {"daily_loss": "daily_loss_limit_pct", "max_drawdown": "max_drawdown_pct"}}
ON_VALUES = {"cooldown": 300, "rebuy_wait": 60, "volume": 1.5, "trailing": 1.0, "max_hold": 15, "flatten": 10,
             "strength": 100, "spread": 0.1, "capital_usage": 50, "daily_target": 3, "max_buys_per_day": 20,
             "buys_per_symbol": 2, "daily_loss": 5, "max_drawdown": 15}

_ACTION_OPTS = (("block_buys", "신규 매수만 중지"), ("liquidate", "전부 청산"))
_SESSION_OPTS = tuple((s.value, s.label) for s in (Session.PRE, Session.REGULAR, Session.AFTER))

GROUPS = ("매수 신호", "스캘핑", "익절·손절", "자금 배분", "하루 목표·한도", "주문", "손실 차단", "수수료·세금", "안전 스위치")

FIELDS: tuple[Field, ...] = (
    # ── 꺼 둔 기능 목록 (화면에는 각 칸의 스위치로 보인다) ──
    Field("ma_cross", "off", "꺼 둔 기능", "off", "매수 신호", options=tuple(MA_TOGGLES.items())),
    Field("order", "off", "꺼 둔 주문 규칙", "off", "주문", options=tuple(ORDER_TOGGLES.items())),
    Field("risk", "off", "꺼 둔 손실 한도", "off", "손실 차단", options=tuple(RISK_TOGGLES.items())),
    # ── 매수 신호 ──
    Field("ma_cross", "sessions", "매매할 세션", "sessions", "매수 신호", options=_SESSION_OPTS,
          help="신호를 낼 시간대. 고르지 않은 세션에는 익절·손절 매도도 하지 않습니다"),
    Field("ma_cross", "min_change_rate", "등락률 하한", "number", "매수 신호", toggle="change_rate", unit="%", min=-30, max=30, step=0.1,
          help="골든크로스가 나도 전일 대비 등락률이 이 범위 밖이면 매수하지 않습니다"),
    Field("ma_cross", "max_change_rate", "등락률 상한", "number", "매수 신호", toggle="change_rate", unit="%", min=-30, max=30, step=0.1),
    Field("ma_cross", "cooldown_sec", "신호 간격", "int", "매수 신호", toggle="cooldown", unit="초", min=0, max=3600, step=10,
          help="한 종목에서 신호가 난 뒤 이 시간 동안 같은 종목 신호를 무시합니다"),
    Field("ma_cross", "rebuy_wait_sec", "매도 후 재매수 대기", "int", "매수 신호", toggle="rebuy_wait", unit="초", min=0, max=86_400, step=10,
          help="같은 종목을 판(체결) 뒤 이 시간 동안은 다시 사지 않음 (0 = 바로 가능). 매도는 막지 않음. "
               "같은 날 다시 사려면 주문 > '종목당 하루 매수 횟수'도 2 이상이어야 함"),
    Field("ma_cross", "bar_minutes", "봉 단위", "select", "매수 신호", apply="next_day",
          options=tuple((m, f"{m}분") for m in (1, 2, 3, 5, 10, 15, 30, 60))),
    Field("ma_cross", "short_window", "단기 이동평균", "int", "매수 신호", apply="next_day", unit="봉", min=1, max=118, step=1),
    Field("ma_cross", "long_window", "장기 이동평균", "int", "매수 신호", apply="next_day", unit="봉", min=2, max=119, step=1,
          help="단기보다 커야 합니다. 과거 분봉 조회 한도 때문에 119봉까지"),
    # ── 스캘핑 (진입 방식 · 짧은 청산 · 실시간 거르개) ──
    Field("ma_cross", "entry_mode", "진입 방식", "select", "스캘핑",
          options=(("ma_cross", "이평 교차 (기존)"), ("breakout", "박스 돌파"), ("vwap", "VWAP 눌림 반등"),
                   ("orb", "시가 범위 돌파")),
          help="이평 교차: 단기 이평이 장기 이평 상향 돌파 / 박스 돌파: 직전 N봉 고가 돌파 / "
               "VWAP 눌림: 상승 추세에서 VWAP 까지 눌렸다가 양봉 반등 / 시가 범위: 정규장 첫 N분 고가 돌파"),
    Field("ma_cross", "breakout_bars", "박스 돌파 기준 봉 수", "int", "스캘핑", unit="봉", min=2, max=118, step=1,
          help="박스 돌파에서 직전 몇 개 봉의 최고가를 넘으면 살지"),
    Field("ma_cross", "orb_minutes", "시가 범위 시간", "int", "스캘핑", unit="분", min=1, max=120, step=1,
          help="시가 범위 돌파에서 정규장 시작 후 몇 분을 범위로 볼지"),
    Field("ma_cross", "volume_mult", "돌파 거래량 배수", "number", "스캘핑", toggle="volume", unit="배", min=0, max=50, step=0.1,
          help="돌파 봉 거래량이 기준 봉 평균의 이 배 이상일 때만 매수 (0 = 끔). 거래량 없는 돌파는 실패가 많음"),
    Field("ma_cross", "trailing_activate_pct", "추적 손절 시작", "number", "스캘핑", toggle="trailing", unit="%", min=0, max=50, step=0.1,
          help="고점이 매입가 대비 이만큼 오르면 추적 손절을 켬 (0 = 끔)"),
    Field("ma_cross", "trailing_pct", "추적 손절 폭", "number", "스캘핑", toggle="trailing", unit="%", min=0.05, max=20, step=0.05,
          help="추적 손절이 켜진 뒤 고점 대비 이만큼 밀리면 매도"),
    Field("ma_cross", "max_hold_min", "최대 보유 시간", "int", "스캘핑", toggle="max_hold", unit="분", min=0, max=1440, step=1,
          help="산 뒤 이 시간이 지나면 손익과 상관없이 매도 (0 = 끔). 움직이지 않는 종목에 돈이 묶이지 않게"),
    Field("ma_cross", "flatten_before_close_min", "마감 전 정리", "int", "스캘핑", toggle="flatten", unit="분", min=0, max=240, step=1,
          help="고른 세션 중 마지막 세션이 끝나기 이 시간 전에 보유 전부 매도, 그 뒤로는 매수 안 함 (0 = 끔)"),
    Field("ma_cross", "min_strength", "체결강도 하한 (실시간)", "number", "스캘핑", toggle="strength", min=0, max=1000, step=5,
          help="체결강도가 이 값 이상일 때만 매수 (0 = 끔, 100 = 매수·매도 균형). 분봉에 없어 백테스트에는 적용 안 됨"),
    Field("ma_cross", "max_spread_pct", "호가 스프레드 상한 (실시간)", "number", "스캘핑", toggle="spread", unit="%", min=0, max=5, step=0.01,
          help="최우선 매수·매도 호가 차이가 이 % 이하일 때만 매수 (0 = 끔). 백테스트에는 적용 안 됨"),
    # ── 익절·손절 ──
    Field("ma_cross", "take_profit_pct", "익절", "number", "익절·손절", toggle="take_profit", unit="%", min=0.1, max=100, step=0.1,
          help="매입평균가 대비 이만큼 오르면 전량 매도"),
    Field("ma_cross", "stop_loss_pct", "손절", "number", "익절·손절", toggle="stop_loss", unit="%", min=0.1, max=50, step=0.1,
          help="매입평균가 대비 이만큼 내리면 전량 매도. 끄려면 추적 손절·최대 보유 시간·마감 전 정리 중 하나 이상이 켜져 있어야 함"),
    Field("ma_cross", "dead_cross", "데드크로스 매도", "toggle", "익절·손절", toggle="dead_cross",
          help="이평 교차로 산 종목을 단기 이평이 장기 이평 아래로 내려갈 때 매도"),
    # ── 자금 배분 ──
    Field("ma_cross", "capital_usage_pct", "주문가능금액 사용 비율", "number", "자금 배분", toggle="capital_usage", unit="%", min=0, max=100, step=1,
          help="종목당 예산 = 거래일 시작 시 주문가능 USD × 비율 ÷ 최대 보유 종목 수. 끄면 아래 고정 금액 사용"),
    Field("ma_cross", "budget_per_trade_usd", "1회 매수 고정 금액", "number", "자금 배분", unit="USD", min=1, max=10_000_000, step=100,
          help="주문가능금액 사용 비율을 끄면 씁니다"),
    # ── 하루 목표·한도 (거래 옵션마다 다르게) ──
    Field("ma_cross", "daily_profit_target_pct", "하루 목표 수익", "number", "하루 목표·한도", toggle="daily_target", unit="%", min=0, max=100, step=0.5,
          help="오늘 손익(실현+보유 평가)이 하루 시작 예수금의 이 비율 이상이면 보유 전부 매도하고 그날 매매 종료. "
               "다음 거래일 자동 재개 (0 = 끔)"),
    Field("ma_cross", "max_buys_per_day", "하루 최대 매수 횟수", "int", "하루 목표·한도", toggle="max_buys_per_day", unit="회", min=0, max=1000, step=1,
          help="오늘 매수가 이 횟수에 닿으면 더 사지 않음 (0 = 제한 없음). 매도는 막지 않음. 1회 금액은 자금 배분 설정"),
    Field("ma_cross", "max_positions", "최대 보유 종목 수", "int", "하루 목표·한도", toggle="max_positions", unit="종목", min=1, max=50, step=1,
          help="보유(+매수 대기) 종목이 이 수에 닿으면 새 종목을 사지 않음. 하나가 팔리면 다시 매수. "
               "사용 비율로 예산을 정할 때 나누는 수이기도 함 (끄면 종목 수 제한 없음, 이때는 고정 금액만 씀)"),
    # ── 주문 ──
    Field("order", "slippage_pct", "지정가 여유", "number", "주문", unit="%", min=0, max=4.9, step=0.05,
          help="매수는 현재가보다 이만큼 높게, 매도는 낮게 지정가를 냅니다 (사실상 즉시 체결)"),
    Field("order", "order_timeout_sec", "미체결 자동 취소", "int", "주문", unit="초", min=5, max=3600, step=5,
          help="이 시간 안에 체결되지 않으면 남은 수량을 취소합니다"),
    Field("order", "poll_interval_sec", "체결 확인 주기", "int", "주문", unit="초", min=1, max=60, step=1),
    Field("order", "max_orders_per_day", "하루 최대 주문", "int", "주문", unit="건", min=1, max=1000, step=1,
          help="모든 옵션 공통 안전 한도 (매수·매도 합계, 거부 제외). 한도에 닿으면 매수만 막고 매도는 계속"),
    Field("order", "max_buys_per_symbol", "종목당 하루 매수 횟수", "int", "주문", toggle="buys_per_symbol", unit="회", min=0, max=100, step=1,
          help="0 = 제한 없음. 1 이면 손절한 종목을 같은 날 다시 사지 않습니다"),
    Field("order", "cancel_buy_above_limit", "가격 이탈 시 매수 취소", "bool", "주문",
          help="현재가가 매수 지정가 위로 오르면 미체결 매수를 바로 취소합니다"),
    # ── 손실 차단 ──
    Field("risk", "daily_loss_limit_pct", "하루 손실 한도", "number", "손실 차단", toggle="daily_loss", unit="%", min=0, max=99, step=0.5,
          help="오늘 시작 총자산 대비 이만큼 잃으면 그날 매매 중지 (0 = 끔)"),
    Field("risk", "daily_loss_action", "하루 손실 한도 도달 시", "select", "손실 차단", toggle="daily_loss", options=_ACTION_OPTS),
    Field("risk", "max_drawdown_pct", "최대 낙폭 한도", "number", "손실 차단", toggle="max_drawdown", unit="%", min=0, max=99, step=0.5,
          help="고점 대비 이만큼 빠지면 재개할 때까지 매매 중지 (0 = 끔)"),
    Field("risk", "max_drawdown_action", "최대 낙폭 도달 시", "select", "손실 차단", toggle="max_drawdown", options=_ACTION_OPTS),
    Field("risk", "confirm_sec", "발동 확인 시간", "int", "손실 차단", unit="초", min=0, max=600, step=1,
          help="한도 초과가 이 시간 동안 이어져야 발동 (순간적인 이상 시세 무시)"),
    Field("risk", "liquidation_slippage_pct", "청산 지정가 여유", "number", "손실 차단", unit="%", min=0, max=9.9, step=0.1,
          help="청산 매도 지정가 = 현재가 − 이 비율"),
    # ── 수수료·세금 (연간 정산 계산에만 쓰임, 매매에는 영향 없음) ──
    Field("fees", "commission_pct", "매매 수수료", "number", "수수료·세금", unit="%", min=0, max=4.99, step=0.01,
          help="매수·매도 금액에 각각 붙는 증권사 수수료. 등급·이벤트에 따라 다르니 실제 값으로"),
    Field("fees", "sec_fee_per_million", "SEC 수수료 (매도)", "number", "수수료·세금", unit="USD", min=0, max=1000, step=0.1,
          help="매도 금액 $1,000,000 당 금액 (미국 증권거래위원회 부과, 수시로 바뀜)"),
    Field("fees", "finra_taf_per_share", "FINRA TAF (매도)", "number", "수수료·세금", unit="USD", min=0, max=1, step=0.000001,
          help="매도 1주당 금액, 주문 1건당 상한 있음"),
    Field("tax", "cost_method", "취득가 계산 방식", "select", "수수료·세금",
          options=(("fifo", "선입선출"), ("average", "이동평균")),
          help="같은 종목을 여러 번 나눠 샀을 때 어떤 매수분을 판 것으로 볼지"),
    Field("tax", "basic_deduction_krw", "기본공제", "int", "수수료·세금", unit="원", min=0, max=100_000_000, step=10000,
          help="해외주식 양도소득 연 250만원"),
    Field("tax", "tax_rate_pct", "세율", "number", "수수료·세금", unit="%", min=0, max=100, step=0.1,
          help="양도소득세 20% + 지방소득세 2%"),
    # ── 안전 스위치 (화면에서 변경 불가) ──
    Field("order", "dry_run", "주문 미전송 (신호만 기록)", "bool", "안전 스위치", apply="file"),
    Field("order", "allow_real_trading", "실전 계좌 주문 허용", "bool", "안전 스위치", apply="file"),
    Field("order", "cancel_open_orders_on_start", "시작 시 기존 미체결 취소", "bool", "안전 스위치", apply="file"),
)
_BY_ID = {f.id: f for f in FIELDS}
_STORED = tuple(f for f in FIELDS if f.stored)


def toggle_meta(f: Field) -> dict | None:
    """화면용 스위치 정보: 이름, 이 칸에 스위치를 그릴지(main), 0 = 끔 인 값 칸, 다시 켤 때 넣을 값."""
    if not f.toggle:
        return None
    main = next(x for x in FIELDS if x.section == f.section and x.toggle == f.toggle)
    zero_key = ZERO_OFF.get(f.section, {}).get(f.toggle)
    return {"name": f.toggle, "label": TOGGLES[f.section][f.toggle], "main": main is f,
            "zero_key": zero_key, "on_value": ON_VALUES.get(f.toggle)}


@dataclass(frozen=True)
class Change:
    field: Field
    old: Any
    new: Any

    def text(self) -> str:
        f = self.field
        if f.type == "off":   # "추적 손절 끔, 손절 켬"
            old, new = set(_jsonable(self.old)), set(_jsonable(self.new))
            names = dict(f.options)
            return ", ".join([f"{names.get(n, n)} 끔" for n in names if n in new - old]
                             + [f"{names.get(n, n)} 켬" for n in names if n in old - new])
        return f"{f.label} {_show(f, self.old)} → {_show(f, self.new)}"


def _jsonable(v: Any) -> Any:
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, Session):
        return v.value
    return v


def _show(f: Field, v: Any) -> str:
    v = _jsonable(v)
    if f.type == "bool":
        return "켬" if v else "끔"
    if f.type == "sessions":
        names = dict(_SESSION_OPTS)
        return "/".join(names.get(s, s) for s in v) or "없음"
    if f.type == "off":
        names = dict(f.options)
        return ", ".join(names.get(n, n) for n in v) or "없음"
    if f.options:
        return dict(f.options).get(v, str(v))
    s = f"{v:,.6f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"
    return f"${s}" if f.unit == "USD" else f"{s}{f.unit}"


def _coerce(f: Field, v: Any) -> Any:
    """화면에서 온 값을 TOML 에 쓸 타입으로. 범위 밖이면 ValueError."""
    if f.type == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"{f.label}: 켬/끔 값이어야 합니다")
        return v
    if f.type == "sessions":
        if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
            raise ValueError(f"{f.label}: 세션 목록이어야 합니다")
        allowed = [s for s, _ in _SESSION_OPTS]
        if any(s not in allowed for s in v):
            raise ValueError(f"{f.label}: PRE / REGULAR / AFTER 중에서 골라야 합니다")
        return [s for s in allowed if s in v]          # 순서 고정, 중복 제거
    if f.type == "off":
        names = [o for o, _ in f.options]
        if not isinstance(v, list) or any(not isinstance(x, str) or x not in names for x in v):
            raise ValueError(f"{f.label}: 알 수 없는 기능 이름입니다")
        return [n for n in names if n in v]
    if f.type == "toggle":
        raise ValueError(f"{f.label}: 켬/끔은 off 목록으로 바꿉니다")
    if f.type == "select":
        allowed = [o for o, _ in f.options]
        if v not in allowed or isinstance(v, bool):
            raise ValueError(f"{f.label}: 허용되지 않은 값입니다 ({v!r})")
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{f.label}: 숫자여야 합니다")
    if f.type == "int":
        if v != int(v):
            raise ValueError(f"{f.label}: 정수여야 합니다")
        v = int(v)
    else:
        v = float(v)
    if (f.min is not None and v < f.min) or (f.max is not None and v > f.max):
        raise ValueError(f"{f.label}: {f.min:g} ~ {f.max:g}{f.unit} 범위여야 합니다")
    return v


# ── TOML 값 교체 (주석·빈 줄·순서 보존) ─────────────────────
def _toml_literal(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        if any(c in v for c in '"\\\n\r'):
            raise ValueError("따옴표·줄바꿈이 들어간 값은 쓸 수 없습니다")
        return f'"{v}"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_literal(x) for x in v) + "]"
    raise TypeError(f"TOML 로 쓸 수 없는 값: {v!r}")


def _comment_start(s: str) -> int:
    """따옴표 밖의 첫 '#' 위치 (없으면 len)."""
    quote = None
    for i, c in enumerate(s):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            return i
    return len(s)


def patch_toml(text: str, section: str, key: str, value: Any) -> str:
    """[section] 의 key 줄에서 값만 바꾼다. 줄이 없으면 그 섹션 끝에 추가."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    header = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")
    key_re = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(.*)$")
    literal = _toml_literal(value)
    start = end = None
    for i, line in enumerate(lines):
        m = header.match(line)
        if m:
            if start is not None:
                end = i
                break
            if m.group(1) == section:
                start = i
    if start is None:   # 섹션이 없으면 파일 끝에 새로 만든다
        body = nl.join(lines).rstrip("\r\n")
        return f"{body}{nl}{nl}[{section}]{nl}{key} = {literal}{nl}"
    end = len(lines) if end is None else end
    for i in range(start + 1, end):
        m = key_re.match(lines[i])
        if not m:
            continue
        prefix, rest = m.groups()
        cut = _comment_start(rest)
        old_value = rest[:cut].rstrip()
        if old_value.startswith("[") and not old_value.endswith("]"):
            raise ValueError(f"[{section}] {key} 가 여러 줄로 적혀 있어 자동으로 바꿀 수 없습니다. 파일에서 한 줄로 고쳐 주세요")
        comment = rest[cut:]
        if comment:
            gap = max(1, len(rest[:cut]) - len(literal))   # 주석 위치를 되도록 그대로
            lines[i] = prefix + literal + " " * gap + comment
        else:
            lines[i] = prefix + literal
        return nl.join(lines)
    last = end - 1
    while last > start and not lines[last].strip():
        last -= 1
    lines.insert(last + 1, f"{key} = {literal}")
    return nl.join(lines)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def load_params(path: Path) -> dict[str, object]:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return {s: cls.from_dict(raw.get(s, {})) for s, cls in _SECTIONS.items()}


Listener = Callable[["LiveConfig", list[Change]], None]


class LiveConfig:
    """현재 적용 중인 설정. 바뀌면 등록된 listener 에게 (설정, 변경 목록)을 알린다."""

    def __init__(self, path: Path, params: dict[str, object]):
        self.path = path
        self._p = dict(params)
        self._mtime = _mtime(path)
        self._bad_mtime: float | None = None
        self._next_poll = 0.0
        self._listeners: list[Listener] = []

    @classmethod
    def load(cls, path: Path) -> LiveConfig:
        return cls(path, load_params(path))

    def params(self, section: str):
        return self._p[section]

    # 매매에 쓰는 값 (꺼 둔 기능은 없는 것과 같은 값). 파일·화면에 있는 원래 값은 params(section)
    ma = property(lambda self: self._p["ma_cross"].effective())
    order = property(lambda self: self._p["order"].effective())
    risk = property(lambda self: self._p["risk"].effective())
    fees = property(lambda self: self._p["fees"])
    tax = property(lambda self: self._p["tax"])

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    # ── 화면용 ─────────────────────────────────────────────
    def describe(self) -> dict:
        groups = []
        for g in GROUPS:
            items = []
            for f in FIELDS:
                if f.group != g:
                    continue
                p = self.params(f.section)
                items.append({"id": f.id, "section": f.section, "key": f.key, "label": f.label, "type": f.type,
                              "apply": f.apply, "unit": f.unit, "help": f.help, "min": f.min, "max": f.max,
                              "step": f.step, "options": [{"value": v, "label": l} for v, l in f.options],
                              "toggle": toggle_meta(f),
                              "value": _jsonable(getattr(p, f.key)) if f.stored else p.uses(f.toggle)})
            groups.append({"title": g, "fields": items})
        return {"groups": groups, "file": self.path.name}

    # ── 변경 ───────────────────────────────────────────────
    def _validate(self, values: dict[str, Any]) -> tuple[dict[Field, Any], dict[str, object]]:
        """현재 설정에 values 를 덮어쓴 결과를 검증한다. (실제로 바뀌는 칸, 섹션별 새 설정). 문제가 있으면 ValueError."""
        if not isinstance(values, dict):
            raise ValueError("설정 형식 오류")
        coerced: dict[Field, Any] = {}
        for fid, v in values.items():
            f = _BY_ID.get(fid)
            if f is None:
                raise ValueError(f"알 수 없는 설정: {fid}")
            if f.apply == "file":
                raise ValueError(f"'{f.label}' 은(는) 안전을 위해 화면에서 바꿀 수 없습니다. {self.path.name} 를 직접 수정하세요")
            coerced[f] = _coerce(f, v)
        # 바뀐 값만 남기고, 모두 합쳐 한꺼번에 검증 (예: 단기 < 장기)
        coerced = {f: v for f, v in coerced.items() if _jsonable(getattr(self.params(f.section), f.key)) != v}
        merged = {s: {fl.name: getattr(self.params(s), fl.name) for fl in fields(cls)} for s, cls in _SECTIONS.items()}
        for f, v in coerced.items():
            merged[f.section][f.key] = v
        new = {s: cls.from_dict(merged[s]) for s, cls in _SECTIONS.items()}   # ValueError → 그대로 전달
        return coerced, new

    def preview(self, values: dict[str, Any]) -> dict[str, object]:
        """파일에 쓰지 않고 values 를 적용한 설정 (백테스트용). 화면과 같은 검증을 거친다."""
        return self._validate(values or {})[1]

    def update(self, values: dict[str, Any], source: str = "대시보드") -> list[Change]:
        """{"ma_cross.take_profit_pct": 4.0, ...} → 검증 → 파일 기록 → 적용. 문제가 있으면 ValueError (아무것도 안 바뀜)."""
        self.poll(force=True)            # 파일을 직접 고친 내용이 있으면 먼저 반영
        if not isinstance(values, dict) or not values:
            raise ValueError("바꿀 설정이 없습니다")
        coerced, new = self._validate(values)
        if not coerced:
            return []

        text = self.path.read_bytes().decode("utf-8")
        for f, v in coerced.items():
            text = patch_toml(text, f.section, f.key, v)
        try:
            check = tomllib.loads(text)                 # 쓰기 전에 결과 파일이 올바른지 확인
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{self.path.name} 에 문법 오류가 있어 저장하지 못했습니다. 파일을 먼저 고쳐 주세요 ({e})") from None
        for f, v in coerced.items():
            if check.get(f.section, {}).get(f.key) != v:
                raise ValueError(f"{f.label}: 설정 파일에 기록하지 못했습니다")
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, self.path)
        self._mtime = _mtime(self.path)
        return self._apply(new, source=source)

    def poll(self, force: bool = False) -> list[Change]:
        """파일이 바뀌었으면 다시 읽어 적용. 매매 루프에서 자주 불러도 된다 (2초 간격으로만 확인)."""
        now = time.monotonic()
        if not force and now < self._next_poll:
            return []
        self._next_poll = now + _POLL_SEC
        mtime = _mtime(self.path)
        if mtime is None or mtime == self._mtime:
            return []
        try:
            new = load_params(self.path)
        except (OSError, ValueError) as e:   # tomllib.TOMLDecodeError 도 ValueError
            if mtime != self._bad_mtime:
                self._bad_mtime = mtime
                log.error("[설정 변경] %s 에 오류가 있어 적용하지 않았습니다 (기존 설정 유지): %s", self.path.name, e,
                          extra=NOTIFY)
            return []
        self._mtime, self._bad_mtime = mtime, None
        return self._apply(new, source="설정 파일")

    def _apply(self, new: dict[str, object], source: str) -> list[Change]:
        changes = [Change(f, getattr(self._p[f.section], f.key), getattr(new[f.section], f.key))
                   for f in _STORED
                   if _jsonable(getattr(self._p[f.section], f.key)) != _jsonable(getattr(new[f.section], f.key))]
        self._p = new
        if not changes:
            return []
        # 거래일 중이면 바로 반영되고, 장 외 시간이면 다음 세션부터 (봉 단위·이평 기간은 항상 다음 거래일부터)
        text = ", ".join(c.text() + (" (다음 거래일부터)" if c.field.apply != "now" else "") for c in changes)
        log.warning("[설정 변경] (%s) %s", source, text, extra=NOTIFY)
        for fn in list(self._listeners):
            try:
                fn(self, changes)
            except Exception:
                log.exception("설정 변경 반영 중 오류")
        return changes
