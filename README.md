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
| **긴급 정지** (미체결 취소 + 전부 매도 + 매수 중지) | `kill.bat` 더블클릭 또는 `run.bat --kill` | `./run.sh --kill` |
| 일시정지 (신규 매수만 중지, 매도는 계속) | `run.bat --pause` | `./run.sh --pause` |
| 매매 재개 (손실 한도·긴급 정지·일시정지 해제) | `run.bat --resume` | `./run.sh --resume` |
| 손실 차단 상태 보기 | `run.bat --risk-status` | `./run.sh --risk-status` |

## 대시보드 (브라우저 화면)

매매 프로그램(`run.bat` / `./run.sh`)이 실행되는 동안 **http://127.0.0.1:8765** 에서 볼 수 있다. 이 PC 에서만 접속된다.

- 로그인 비밀번호: `.env` 의 `DASHBOARD_PASSWORD` (비우면 실행할 때마다 임시 비밀번호를 콘솔 창에 표시)
- **매매 제어** 버튼: 일시정지 · 재개 · 긴급 정지 (긴급 정지는 확인 문구 `긴급정지` 입력 필요). 장 외 시간에 누르면 다음 세션 시작 때 적용
- **실시간** 탭: 총자산·오늘 손익·손실 한도 사용률, 보유 종목(익절·손절까지 거리), 오늘 주문, 최근 이벤트, 감시 종목(이동평균 추세) — 1초마다 갱신
- **정산** 탭: 일별 총자산·실현손익 그래프, 일별 요약, 날짜별 매매 상세
- 끄거나 포트를 바꾸려면 `config/strategy.toml` 의 `[dashboard]`

화면을 고칠 때만 Node.js 가 필요하다 (실행하는 PC 에는 필요 없음):

```
cd web/frontend
npm install
npm run dev     # 개발 서버 http://localhost:5173 (매매 프로그램이 실행 중이어야 데이터가 보임)
npm run build   # web/dist 갱신 → 커밋
```

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
risk/guard.py        손실 차단 (하루 손실 한도·최대 낙폭 한도·긴급 정지)
web/server.py        대시보드 웹 서버 (FastAPI, 127.0.0.1 전용)
web/state.py         대시보드에 보낼 상태 모음
web/frontend/        대시보드 화면 소스 (React + TypeScript, Vite)
web/dist/            빌드된 화면 (커밋됨)
```

## 정산 데이터 (`data/reports/{paper|real}/`)

| 파일 | 내용 |
|---|---|
| `trades/YYYY-MM-DD.csv` | 그날 주문 전체 (주문 1건 = 1행, 거부·취소 포함) |
| `daily/YYYY-MM-DD.json` | 그날 정산 보고서 (손익·수익률·보유 종목·매매내역) |
| `daily_summary.csv` | 하루 1행 누적 요약 — 엑셀로 바로 열림 |

- 날짜는 미국 현지 거래일 기준. 실현손익은 수수료·세금 제외 추정치.
- `data/` 는 git 에 올라가지 않으므로 Windows/macOS 각각 따로 쌓인다.

## 손실 차단 (`config/strategy.toml` [risk])

| 장치 | 발동 조건 (기본값) | 동작 | 해제 |
|---|---|---|---|
| 하루 손실 한도 | 오늘 시작 총자산 대비 -5% 가 10초 이상 | 신규 매수 중지 (익절·손절 매도는 계속) | 다음 거래일 자동 |
| 최대 낙폭 한도 | 고점(일별 마감 총자산 최고치) 대비 -15% | 신규 매수 중지 | `--resume` |
| 긴급 정지 | `kill.bat` / `--kill` / 대시보드 버튼 | 미체결 전부 취소 + 보유 전부 매도 + 매수 중지 | `--resume` / 재개 버튼 |
| 일시정지 | `--pause` / 대시보드 버튼 | 신규 매수 중지 (매도는 계속) | `--resume` / 재개 버튼 |

- 총자산 = 주문가능 USD + 보유 평가금액(실시간 체결가). `*_action = "liquidate"` 로 바꾸면 한도 발동 시에도 전부 매도.
- 매매 프로그램이 실행 중이면 `--kill`/`--resume` 은 몇 초 안에 반영된다. 꺼져 있으면 장중엔 그 자리에서 청산, 휴장이면 다음 실행 때 청산.
- 상태는 `data/risk/{paper|real}.json` 에 저장되어 재시작해도 유지된다.
- 입출금이 있으면 총자산이 달라져 한도가 잘못 발동할 수 있다 → 입출금 후 `--resume` 으로 기준점을 다시 잡는다.

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
