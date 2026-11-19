"""기능 켜고 끄기: 설정 섹션마다 off = ["trailing", ...] 목록으로 꺼 둔 기능을 적는다.

- 끈 기능의 값은 그대로 남는다 → 다시 켜면 예전 값으로 동작
- 예전 방식(값 0 = 끔)도 그대로 통한다 (zero_off: 값이 0 이면 꺼진 것으로 본다)
- 매매에 쓰는 값은 effective() 로 바꾼 것: 꺼진 기능은 '없는 것과 같은 값'(0, 넓은 범위 등)이 들어간다
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any


def parse_off(raw: Any, toggles: dict[str, str], section: str) -> tuple[str, ...]:
    """TOML·화면의 off 목록 → 정해진 순서의 tuple. 모르는 이름이면 ValueError."""
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ValueError(f"[{section}] off 는 목록이어야 합니다 (예: off = [\"trailing\"])")
    bad = [x for x in raw if x not in toggles]
    if bad:
        raise ValueError(f"[{section}] off: 알 수 없는 항목 {bad} (가능: {', '.join(toggles)})")
    return tuple(n for n in toggles if n in raw)


def uses(params: Any, name: str, zero_off: dict[str, str]) -> bool:
    """기능이 켜져 있는가: off 목록에 없고, 0 = 끔 인 항목이면 값이 0 이 아님."""
    if name in params.off:
        return False
    key = zero_off.get(name)
    return key is None or bool(getattr(params, key))


def effective(params: Any, neutral: dict[str, dict[str, Any]]):
    """꺼진 기능의 값을 '없는 것과 같은 값'으로 바꾼 사본 (off 목록은 그대로 둔다 → 화면에서 '끔' 표시용)."""
    changes: dict[str, Any] = {}
    for name in params.off:
        changes.update(neutral.get(name, {}))
    if not changes or all(getattr(params, k) == v for k, v in changes.items()):
        return params
    return replace(params, **changes)


def off_labels(off: tuple[str, ...] | list[str], toggles: dict[str, str]) -> str:
    return ", ".join(toggles.get(n, n) for n in off) or "없음"
