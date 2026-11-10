# vibe-stock

한국투자증권(KIS) OpenAPI 기반 미국주식 자동매매 프로그램 (Python 3.11+, asyncio).
Windows 와 macOS 에서 같은 저장소를 공유해 개발한다.

## 처음 설정 (기기마다 1회)

```bash
git clone https://github.com/marblemantheater-ground/vibe-stock.git
cd vibe-stock
```

1. `.env.example` 을 복사해 `.env` 를 만들고 앱키·시크릿·계좌번호 입력
   - Windows: `copy .env.example .env` / macOS: `cp .env.example .env`
   - `.env` 는 git 에 올라가지 않으므로 **기기마다 따로** 만들어야 한다.
2. 실행 (가상환경 생성·패키지 설치까지 자동)

| | Windows | macOS |
|---|---|---|
| 상시 실행 | `run.bat` | `./run.sh` |
| 거래일 하루만 실행 후 종료 (스케줄러용) | `run.bat --once` | `./run.sh --once` |
| N초 테스트 실행 (주문·정산 포함) | `run.bat --stream-test 60` | `./run.sh --stream-test 60` |
| 정산 보고서 보기 | `run.bat --report` (또는 `--report 2026-11-10`) | `./run.sh --report` |

## 구조

```
main.py              진입점 (세션 대기 → 실시간 시세 → 전략 → 주문 → 정산)
config/settings.py   KIS_ENV=paper|real 로 모의/실전 전환
config/watchlist.csv 매매 대상 종목
config/strategy.toml 전략·주문 설정 (실전 주문은 allow_real_trading = true 필요)
core/                토큰·REST·웹소켓·로깅
broker/account.py    예수금·잔고 조회
broker/order.py      주문·체결 확인·미체결 취소
market/clock.py      미국장 세션 판별 (서머타임 자동, 휴장일 연 1회 갱신)
market/bars.py       분봉 조회·집계
strategy/            매매 전략 (이동평균 돌파 + 등락률 조건)
report/settlement.py 거래일 정산 (매매내역·손익·수익률 저장)
```

## 정산 데이터 (`data/reports/{paper|real}/`)

| 파일 | 내용 |
|---|---|
| `trades/YYYY-MM-DD.csv` | 그날 주문 전체 (주문 1건 = 1행, 거부·취소 포함) |
| `daily/YYYY-MM-DD.json` | 그날 정산 보고서 (손익·수익률·보유 종목·매매내역) |
| `daily_summary.csv` | 하루 1행 누적 요약 — 엑셀로 바로 열림 |

- 날짜는 미국 현지 거래일 기준. 실현손익은 수수료·세금 제외 추정치.
- `data/` 는 git 에 올라가지 않으므로 Windows/macOS 각각 따로 쌓인다.

## Windows ↔ macOS 공유 규칙

- **줄바꿈**: `.gitattributes` 로 모든 텍스트 파일을 LF 로 고정 (`*.bat` 만 CRLF).
  각 기기의 `core.autocrlf` 설정과 무관하게 동일하게 저장된다.
- **경로**: 코드에서는 항상 `pathlib.Path` 와 `/` 연산자를 사용한다.
  `"data\token.json"` 같은 문자열 경로나 `C:\...` 같은 절대경로는 쓰지 않는다.
- **인코딩**: 파일 읽기/쓰기에는 항상 `encoding="utf-8"` 을 명시한다
  (Windows 기본값 cp949 와 macOS 기본값 utf-8 이 다르기 때문).
- **기기별 파일은 커밋하지 않음**: `.env`, `data/`(토큰 캐시), `logs/`, `.venv/`
- **두 기기에서 동시 실행 금지**: KIS 는 앱키당 웹소켓 세션을 1개만 허용한다.
  한쪽에서 매매 프로그램이 돌고 있으면 다른 쪽은 종료한 상태로 개발한다.
