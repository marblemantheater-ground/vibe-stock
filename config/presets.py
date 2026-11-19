"""거래 옵션: 전략 값([ma_cross] 전체) 묶음을 여러 개 저장하고, 그중 하나만 켠다.

- 저장: config/presets.toml (active = 켜진 옵션 id, [presets.<id>] = 이름·설명·전략 값)
- 켜기: 그 옵션의 값을 설정 즉시 적용(LiveConfig.update)으로 넣는다 → strategy.toml [ma_cross] 와 실행 중인 매매에 반영
  (봉 단위·이동평균 기간은 다음 거래일부터). 이미 보유 중인 종목은 산 때 옵션 기준으로 판다 (strategy/positions.py)
- 설정 탭이나 파일 수정으로 [ma_cross] 가 바뀌면 켜져 있는 옵션에도 같이 저장한다
- 항상 하나만 켜져 있고, 켜진 옵션은 지울 수 없다 → 최소 하나는 켜져 있다
손실 한도·주문 규칙·수수료는 계좌 공통 설정이라 옵션에 넣지 않는다.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any

from config.live import _BY_ID, Change, LiveConfig, _coerce, _jsonable, _show, _toml_literal
from core.notifier import NOTIFY
from market.clock import KST
from strategy.ma_cross import MaCrossParams

log = logging.getLogger(__name__)

KEYS = [f.name for f in fields(MaCrossParams)]
_MAX_PRESETS = 20


def _clean_text(v: Any, limit: int, what: str) -> str:
    s = str(v or "").strip()
    if any(c in s for c in '"\\\n\r'):
        raise ValueError(f"{what}에는 따옴표·역슬래시·줄바꿈을 쓸 수 없습니다")
    if len(s) > limit:
        raise ValueError(f"{what}은(는) {limit}자 이내로 적어 주세요")
    return s


class PresetStore:
    def __init__(self, path: Path, cfg: LiveConfig):
        self.path = path
        self.cfg = cfg
        self.active_id = ""
        self.presets: dict[str, dict] = {}
        self._load()
        cfg.subscribe(self._on_config)

    # ── 파일 ───────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with self.path.open("rb") as f:
                raw = tomllib.load(f)
            presets = raw.get("presets", {})
            if not isinstance(presets, dict) or not presets:
                raise ValueError("옵션이 없습니다")
            defaults = {k: _jsonable(getattr(MaCrossParams(), k)) for k in KEYS}
            for pid, p in presets.items():
                MaCrossParams.from_dict({k: p[k] for k in KEYS if k in p})   # 검증
            # 새로 생긴 칸(예: 하루 목표)이 없는 예전 옵션은 기본값(끔)으로 채운다
            self.presets = {pid: {**defaults, **dict(p)} for pid, p in presets.items()}
            self.active_id = raw.get("active") if raw.get("active") in presets else next(iter(presets))
        except FileNotFoundError:
            self._bootstrap("처음 실행")
        except (OSError, ValueError, KeyError, TypeError) as e:   # TOMLDecodeError 포함
            log.error("거래 옵션 파일(%s)을 읽지 못했습니다 → 현재 설정으로 '거래옵션1'을 새로 만듭니다: %s", self.path.name, e,
                      extra=NOTIFY)
            if self.path.exists():
                self.path.replace(self.path.with_name(self.path.name + ".broken"))
            self._bootstrap("파일 오류")
        # strategy.toml 이 켜진 옵션과 다르면(파일을 따로 고친 경우) strategy.toml 을 기준으로 맞춘다
        if self._values(self.active_id) != self._current_values():
            self.presets[self.active_id].update(self._current_values())
            self._save()

    def _bootstrap(self, why: str) -> None:
        now = datetime.now(KST).isoformat(timespec="seconds")
        self.presets = {"p1": {"name": "거래옵션1", "description": "기존 설정", "created_at": now, "updated_at": now,
                               **self._current_values()}}
        self.active_id = "p1"
        self._save()
        log.info("[거래 옵션] %s → 현재 전략 설정을 '거래옵션1'로 저장하고 켬", why)

    def _save(self) -> None:
        lines = ["# 거래 옵션 — 대시보드 '거래 옵션' 탭에서 관리한다. active 가 지금 켜진 옵션.",
                 "# 각 옵션은 전략 값([ma_cross]) 묶음. 켜면 config/strategy.toml [ma_cross] 에 들어간다.",
                 f"active = {_toml_literal(self.active_id)}", ""]
        for pid, p in self.presets.items():
            lines.append(f"[presets.{pid}]")
            for k in ("name", "description", "created_at", "updated_at"):
                lines.append(f"{k} = {_toml_literal(str(p.get(k, '')))}")
            for k in KEYS:
                lines.append(f"{k} = {_toml_literal(p[k])}")
            lines.append("")
        text = "\n".join(lines)
        tomllib.loads(text)   # 쓰기 전에 올바른지 확인
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, self.path)

    # ── 조회 ───────────────────────────────────────────────
    def _current_values(self) -> dict:
        return {k: _jsonable(getattr(self.cfg.params("ma_cross"), k)) for k in KEYS}   # 원래 값 (꺼 둔 기능의 값도)

    def _values(self, pid: str) -> dict:
        p = self.presets[pid]
        return {k: p[k] for k in KEYS}

    @property
    def active_name(self) -> str:
        return self.presets[self.active_id]["name"]

    def info(self) -> tuple[str, str]:
        return self.active_id, self.active_name

    def list(self) -> list[dict]:
        return [{"id": pid, "name": p["name"], "description": p.get("description", ""), "active": pid == self.active_id,
                 "created_at": p.get("created_at", ""), "updated_at": p.get("updated_at", ""), "values": self._values(pid)}
                for pid, p in self.presets.items()]

    def summary(self, pid: str) -> str:
        v = self._values(pid)
        off = set(v.get("off") or ())
        tp = "익절 끔" if "take_profit" in off else f"익절 {v['take_profit_pct']:g}%"
        sl = "손절 끔" if "stop_loss" in off else f"손절 {v['stop_loss_pct']:g}%"
        return f"{v['bar_minutes']}분봉 MA{v['short_window']}/MA{v['long_window']} · {tp} · {sl}"

    # ── 변경 ───────────────────────────────────────────────
    def _validated(self, values: dict, base: dict) -> dict:
        if not isinstance(values, dict):
            raise ValueError("옵션 값 형식 오류")
        merged = dict(base)
        for k, v in values.items():
            key = k.removeprefix("ma_cross.")
            if key not in KEYS:
                raise ValueError(f"거래 옵션에 없는 값: {k}")
            merged[key] = _coerce(_BY_ID[f"ma_cross.{key}"], v)
        MaCrossParams.from_dict(merged)   # 단기 < 장기 등 전체 검증
        return merged

    def create(self, name: str, description: str = "", values: dict | None = None, copy_from: str | None = None) -> str:
        if len(self.presets) >= _MAX_PRESETS:
            raise ValueError(f"거래 옵션은 {_MAX_PRESETS}개까지 만들 수 있습니다")
        name = _clean_text(name, 30, "이름")
        if not name:
            raise ValueError("이름을 적어 주세요")
        if any(p["name"] == name for p in self.presets.values()):
            raise ValueError(f"'{name}' 이름의 옵션이 이미 있습니다")
        base = self._values(copy_from) if copy_from in self.presets else self._values(self.active_id)
        merged = self._validated(values or {}, base)
        n = 1
        while f"p{n}" in self.presets:
            n += 1
        pid = f"p{n}"
        now = datetime.now(KST).isoformat(timespec="seconds")
        self.presets[pid] = {"name": name, "description": _clean_text(description, 100, "설명"), "created_at": now,
                             "updated_at": now, **merged}
        self._save()
        log.info("[거래 옵션] '%s' 만듦 (%s)", name, self.summary(pid))
        return pid

    def update(self, pid: str, name: str | None = None, description: str | None = None,
               values: dict | None = None) -> list[Change]:
        if pid not in self.presets:
            raise ValueError("없는 거래 옵션입니다")
        p = self.presets[pid]
        new_name = _clean_text(name, 30, "이름") if name is not None else p["name"]
        if not new_name:
            raise ValueError("이름을 적어 주세요")
        if any(q["name"] == new_name for qid, q in self.presets.items() if qid != pid):
            raise ValueError(f"'{new_name}' 이름의 옵션이 이미 있습니다")
        merged = self._validated(values or {}, self._values(pid))
        changes: list[Change] = []
        if pid == self.active_id and merged != self._values(pid):
            # 켜진 옵션 값 변경 → 실행 중인 매매에도 적용 (리스너가 이 옵션 값을 다시 저장)
            changes = self.cfg.update({f"ma_cross.{k}": v for k, v in merged.items()},
                                      source=f"거래 옵션 '{new_name}' 수정")
        p.update(merged, name=new_name, updated_at=datetime.now(KST).isoformat(timespec="seconds"))
        if description is not None:
            p["description"] = _clean_text(description, 100, "설명")
        self._save()
        return changes

    def delete(self, pid: str) -> None:
        if pid not in self.presets:
            raise ValueError("없는 거래 옵션입니다")
        if pid == self.active_id:
            raise ValueError("켜져 있는 옵션은 지울 수 없습니다. 다른 옵션을 먼저 켜세요")
        name = self.presets.pop(pid)["name"]
        self._save()
        log.info("[거래 옵션] '%s' 삭제", name)

    def activate(self, pid: str) -> list[Change]:
        """옵션 켜기: 그 값을 설정에 적용. 실패하면 이전 옵션 그대로."""
        if pid not in self.presets:
            raise ValueError("없는 거래 옵션입니다")
        if pid == self.active_id:
            raise ValueError("이미 켜져 있는 옵션입니다")
        prev = self.active_id
        prev_name = self.active_name
        self.active_id = pid
        try:
            changes = self.cfg.update({f"ma_cross.{k}": v for k, v in self._values(pid).items()},
                                      source=f"거래 옵션 전환 '{prev_name}' → '{self.active_name}'")
        except Exception:
            self.active_id = prev
            raise
        self._save()
        log.warning("[거래 옵션] '%s' → '%s' 켬 (%s). 새 매수부터 적용, 보유 종목은 산 때 옵션 기준으로 매도",
                    prev_name, self.active_name, self.summary(pid), extra=NOTIFY)
        return changes

    def _on_config(self, cfg: LiveConfig, changes: list[Change]) -> None:
        """설정 탭·파일에서 전략 값이 바뀌면 켜진 옵션에도 저장."""
        if not any(c.field.section == "ma_cross" for c in changes):
            return
        p = self.presets[self.active_id]
        p.update(self._current_values(), updated_at=datetime.now(KST).isoformat(timespec="seconds"))
        self._save()
