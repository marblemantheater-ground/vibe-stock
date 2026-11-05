"""KIS 연동 예외 계층."""
from __future__ import annotations


class KisError(Exception):
    """모든 KIS 관련 오류의 기반 클래스."""


class KisNetworkError(KisError):
    """네트워크 단절, 타임아웃, 5xx 등 재시도 후에도 복구되지 않은 오류."""


class KisAuthError(KisError):
    """토큰 발급 실패 (앱키/시크릿 오류, 발급 한도 초과 지속 등)."""


class KisApiError(KisError):
    """서버가 rt_cd != '0' 으로 응답한 업무 오류 (잔고 부족, 장 종료 등). 재시도하지 않는다."""

    def __init__(self, msg_cd: str, msg: str, tr_id: str = "", status: int | None = None):
        self.msg_cd = msg_cd
        self.msg = msg
        self.tr_id = tr_id
        self.status = status
        super().__init__(f"[{tr_id}] {msg_cd} {msg} (HTTP {status})")
