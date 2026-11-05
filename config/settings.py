"""환경 설정 로더.

KIS_ENV(paper | real) 하나만 바꾸면 URL, 앱키, 계좌, TR ID, 레이트리밋이 함께 전환된다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

_ENDPOINTS = {
    "real": {
        "rest": "https://openapi.koreainvestment.com:9443",
        "ws": "ws://ops.koreainvestment.com:21000",
    },
    "paper": {
        "rest": "https://openapivts.koreainvestment.com:29443",
        "ws": "ws://ops.koreainvestment.com:31000",
    },
}

# 기능명 -> (실전 TR ID, 모의 TR ID). None 이면 해당 환경 미지원.
_TR_IDS: dict[str, tuple[str | None, str | None]] = {
    "overseas_balance": ("TTTS3012R", "VTTS3012R"),    # 해외주식 잔고
    "overseas_psamount": ("TTTS3007R", "VTTS3007R"),   # 해외주식 매수가능금액
    "overseas_buy": ("TTTT1002U", "VTTT1002U"),        # 미국 매수
    "overseas_sell": ("TTTT1006U", "VTTT1001U"),       # 미국 매도
    "overseas_nccs": ("TTTS3018R", None),              # 미체결내역 (모의 미지원)
}

# 초당 요청 한도 (KIS 공지 한도보다 약간 보수적으로)
_DEFAULT_RPS = {"real": 15.0, "paper": 2.0}


@dataclass(frozen=True)
class Settings:
    env: str
    app_key: str
    app_secret: str
    cano: str            # 계좌번호 앞 8자리
    acnt_prdt_cd: str    # 계좌상품코드 뒤 2자리
    rest_url: str
    ws_url: str
    max_rps: float
    request_timeout: float
    max_retries: int
    token_refresh_margin_sec: int
    token_cache_path: Path
    seed_access_token: str | None
    seed_access_token_expires: str | None
    ref_symbol: str
    ref_exchange: str
    log_dir: Path
    log_level: str

    @property
    def is_real(self) -> bool:
        return self.env == "real"

    def tr_id(self, name: str) -> str:
        real, paper = _TR_IDS[name]
        tr = real if self.is_real else paper
        if tr is None:
            raise NotImplementedError(f"'{name}' 은(는) {self.env} 환경에서 지원되지 않습니다.")
        return tr

    @property
    def balance_exchanges(self) -> tuple[str, ...]:
        # 실전: NASD 하나로 미국 전체 조회 / 모의: 거래소별로 따로 조회해야 함
        return ("NASD",) if self.is_real else ("NASD", "NYSE", "AMEX")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"환경변수 {name} 이(가) 비어 있습니다. .env 를 확인하세요.")
    return value


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def load_settings() -> Settings:
    env = os.getenv("KIS_ENV", "paper").strip().lower()
    if env not in _ENDPOINTS:
        raise ValueError(f"KIS_ENV 는 paper 또는 real 이어야 합니다: {env!r}")

    prefix = f"KIS_{env.upper()}_"
    account = _require(prefix + "ACCOUNT").replace("-", "")
    if len(account) != 10 or not account.isdigit():
        raise ValueError(f"{prefix}ACCOUNT 형식 오류 (예: 12345678-01): {account!r}")

    data_dir = BASE_DIR / "data"
    return Settings(
        env=env,
        app_key=_require(prefix + "APP_KEY"),
        app_secret=_require(prefix + "APP_SECRET"),
        cano=account[:8],
        acnt_prdt_cd=account[8:],
        rest_url=_ENDPOINTS[env]["rest"],
        ws_url=_ENDPOINTS[env]["ws"],
        max_rps=float(os.getenv("KIS_MAX_RPS") or _DEFAULT_RPS[env]),
        request_timeout=float(os.getenv("KIS_TIMEOUT") or 10),
        max_retries=int(os.getenv("KIS_MAX_RETRIES") or 5),
        token_refresh_margin_sec=int(os.getenv("KIS_TOKEN_REFRESH_MARGIN_SEC") or 3600),
        token_cache_path=data_dir / f"token_{env}.json",
        seed_access_token=_optional("KIS_ACCESS_TOKEN"),
        seed_access_token_expires=_optional("KIS_ACCESS_TOKEN_EXPIRES"),
        ref_symbol=os.getenv("KIS_REF_SYMBOL") or "AAPL",
        ref_exchange=os.getenv("KIS_REF_EXCHANGE") or "NASD",
        log_dir=BASE_DIR / "logs",
        log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
    )
