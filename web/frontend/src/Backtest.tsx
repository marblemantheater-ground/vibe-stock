import { useCallback, useEffect, useMemo, useState } from "react";
import type { BtResult, BtRunSummary, BtStatus, Coverage, SettingField, SettingValue, SettingsView } from "./types";
import { FieldRow, parse, same, show } from "./Settings";
import { Dialog } from "./Controls";
import { pct, tone, usd } from "./format";
import { useWidth } from "./Reports";

const GROUPS = ["매수 신호", "익절·손절", "자금 배분", "주문", "손실 차단"];
const FEW_TRADES = 30;

// 백테스트: 저장된 1분봉으로 실제 전략 코드를 돌려 본다. 바꿔 본 값은 파일에 쓰지 않는다 (적용 버튼을 눌러야 설정에 반영)
export default function Backtest() {
  const [cov, setCov] = useState<Coverage | null>(null);
  const [view, setView] = useState<SettingsView | null>(null);
  const [draft, setDraft] = useState<Record<string, SettingValue>>({});
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [picked, setPicked] = useState<string[] | null>(null);   // null = 전체
  const [cash, setCash] = useState("100000");
  const [slip, setSlip] = useState("0.05");
  const [status, setStatus] = useState<BtStatus | null>(null);
  const [result, setResult] = useState<BtResult | null>(null);
  const [runs, setRuns] = useState<BtRunSummary[]>([]);
  const [error, setError] = useState("");
  const [applying, setApplying] = useState(false);
  const [toast, setToast] = useState("");

  const fields = useMemo(() => view?.groups.filter((g) => GROUPS.includes(g.title)).flatMap((g) => g.fields) ?? [], [view]);

  const loadRuns = useCallback(() => {
    fetch("/api/backtest/runs").then((r) => r.json()).then((d) => setRuns(d.runs)).catch(() => {});
  }, []);

  useEffect(() => {
    Promise.all([fetch("/api/backtest/data").then((r) => r.json()), fetch("/api/settings").then((r) => r.json())])
      .then(([d, s]: [{ coverage: Coverage; status: BtStatus }, SettingsView]) => {
        setCov(d.coverage);
        setStatus(d.status);
        setView(s);
        setDraft(Object.fromEntries(s.groups.flatMap((g) => g.fields).map((f) => [f.id, f.value])));
        if (d.coverage.last) {
          setEnd(d.coverage.last);
          setStart(d.coverage.first!);
        }
      })
      .catch(() => setError("백테스트 정보를 불러오지 못했습니다."));
    loadRuns();
  }, [loadRuns]);

  // 실행 중이면 진행률을 0.5초마다 확인
  useEffect(() => {
    if (!status?.running) return;
    const t = setInterval(() => {
      fetch("/api/backtest/status").then((r) => r.json()).then(setStatus).catch(() => {});
    }, 500);
    return () => clearInterval(t);
  }, [status?.running]);

  const { overrides, errors } = useMemo(() => {
    const overrides: Record<string, SettingValue> = {};
    const errors: Record<string, string> = {};
    for (const f of fields) {
      const p = parse(f, draft[f.id] ?? f.value);
      if (p.error) errors[f.id] = p.error;
      else if (!same(p.value!, f.value)) overrides[f.id] = p.value!;
    }
    return { overrides, errors };
  }, [fields, draft]);

  const run = async () => {
    setError("");
    setStatus((s) => ({ ...(s as BtStatus), running: true, progress: 0 }));
    try {
      const r = await fetch("/api/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Vibe-Control": "1" },
        body: JSON.stringify({ start, end, symbols: picked, overrides, start_cash: Number(cash), fill_slippage_pct: Number(slip) }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) setError(data.error ?? "백테스트에 실패했습니다");
      else setResult(data);
    } catch {
      setError("매매 프로그램에 연결할 수 없습니다");
    } finally {
      setStatus((s) => ({ ...(s as BtStatus), running: false }));
      loadRuns();
    }
  };

  const openRun = async (id: string) => {
    const r = await fetch(`/api/backtest/runs/${id}`);
    if (r.ok) setResult(await r.json());
  };

  const applySettings = async () => {
    if (!result) return;
    const r = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-Vibe-Control": "1" },
      body: JSON.stringify({ values: result.overrides }),
    });
    const data = await r.json().catch(() => ({}));
    setApplying(false);
    if (r.ok && data.ok) {
      setView(data);
      setDraft(Object.fromEntries((data as SettingsView).groups.flatMap((g) => g.fields).map((f) => [f.id, f.value])));
      setToast(data.message);
    } else setToast(data.error ?? "설정에 적용하지 못했습니다");
  };

  if (error && !cov) return <div className="empty">{error}</div>;
  if (!cov || !view) return <div className="empty">불러오는 중…</div>;
  if (!cov.symbols.length)
    return (
      <div className="empty bt-empty">
        <p><strong>저장된 분봉이 없습니다.</strong></p>
        <p>백테스트는 1분봉으로 합니다. 아래 명령으로 먼저 모으세요 (KIS 는 최근 약 한 달치를 줍니다).</p>
        <code>run.bat --collect-minute</code>
        <p className="muted small">이후에는 매매 프로그램이 거래일이 끝날 때마다 자동으로 이어 받아 기간이 늘어납니다.</p>
      </div>
    );

  const running = Boolean(status?.running);
  const allSyms = cov.symbols.map((s) => s.symbol);
  const chosen = picked ?? allSyms;
  const nErrors = Object.keys(errors).length;
  const byId = Object.fromEntries(fields.map((f) => [f.id, f]));

  return (
    <div className="backtest">
      <aside className="panel bt-form">
        <div className="panel-head"><h2>조건</h2>
          <span className="panel-note">분봉 {cov.symbols.length}종목 · {cov.first} ~ {cov.last}</span></div>
        <div className="bt-fields">
          <label>기간
            <span className="bt-range">
              <input type="date" value={start} min={cov.first!} max={end} onChange={(e) => setStart(e.target.value)} />
              <span className="muted">~</span>
              <input type="date" value={end} min={start} max={cov.last!} onChange={(e) => setEnd(e.target.value)} />
            </span>
          </label>
          <div className="bt-syms">
            <span className="bt-label">종목 <span className="muted">{chosen.length}/{allSyms.length}</span>
              <button className="link" onClick={() => setPicked(picked ? null : [])}>{picked ? "전체 선택" : "모두 해제"}</button></span>
            <div className="filters bt-chips">
              {cov.symbols.map((s) => {
                const on = chosen.includes(s.symbol);
                return (
                  <button key={s.symbol} className={`filter ${on ? "on" : ""}`} title={`${s.first} ~ ${s.last} (${s.days}일)`}
                          onClick={() => setPicked(on ? chosen.filter((x) => x !== s.symbol) : [...chosen, s.symbol])}>
                    {s.symbol}
                  </button>
                );
              })}
            </div>
          </div>
          <div className="bt-pair">
            <label>시작 자금 <span className="num-input"><span className="unit">$</span>
              <input inputMode="decimal" value={cash} onChange={(e) => setCash(e.target.value)} /></span></label>
            <label title="신호 가격보다 이만큼 불리하게 체결된다고 가정">체결 미끄러짐 <span className="num-input">
              <input inputMode="decimal" value={slip} onChange={(e) => setSlip(e.target.value)} /><span className="unit">%</span></span></label>
          </div>
        </div>
        <div className="panel-head sub"><h2>바꿔 볼 값</h2>
          <span className="panel-note">비워 두면 지금 설정 그대로 · 파일에는 저장되지 않음</span>
          {Object.keys(overrides).length > 0 && (
            <button className="link" onClick={() => setDraft(Object.fromEntries(fields.map((f) => [f.id, f.value])))}>현재 설정으로</button>
          )}
        </div>
        <div className="fields bt-scroll">
          {fields.map((f) => (
            <FieldRow key={f.id} f={{ ...f, apply: "now" }} value={draft[f.id] ?? f.value} error={errors[f.id]}
                      changed={f.id in overrides} onChange={(v) => setDraft((d) => ({ ...d, [f.id]: v }))} />
          ))}
        </div>
        <div className="bt-run">
          {running ? (
            <div className="bt-progress">
              <div className="small">계산 중… {Math.round((status?.progress ?? 0) * 100)}%</div>
              <div className="bar"><div className="fill level-good" style={{ width: `${(status?.progress ?? 0) * 100}%` }} /></div>
            </div>
          ) : (
            <span className="small muted">{Object.keys(overrides).length ? `바꾼 값 ${Object.keys(overrides).length}개` : "현재 설정 그대로"}</span>
          )}
          <button className="btn primary" disabled={running || nErrors > 0 || !chosen.length || !start || !end} onClick={run}>
            {running ? "실행 중…" : "백테스트 실행"}
          </button>
        </div>
        {error && <p className="error bt-error">{error}</p>}
      </aside>

      <div className="bt-main">
        {result ? <Result r={result} byId={byId} onApply={() => setApplying(true)} /> : (
          <div className="panel empty">왼쪽 조건으로 실행하면 결과가 여기에 나옵니다. 아래 기록을 눌러 지난 결과를 볼 수도 있습니다.</div>
        )}
        <section className="panel">
          <div className="panel-head"><h2>실행 기록</h2><span className="count">{runs.length}</span>
            <span className="panel-note">눌러서 결과 보기 · 바꾼 값끼리 비교</span></div>
          {runs.length === 0 ? <div className="empty">아직 실행 기록이 없습니다.</div> : (
            <div className="table-scroll tall">
              <table>
                <thead><tr><th>실행</th><th>기간</th><th>바꾼 값</th><th className="r">수익률</th><th className="r">단순 보유</th>
                  <th className="r">최대 낙폭</th><th className="r">거래</th><th className="r">승률</th><th className="r">손익비</th></tr></thead>
                <tbody>
                  {runs.map((r) => (
                    <tr key={r.id} className={`clickable ${result?.id === r.id ? "selected" : ""}`} onClick={() => openRun(r.id)}>
                      <td className="num muted">{r.created_at.slice(5, 16).replace("T", " ")}</td>
                      <td className="num">{r.period.start.slice(5)} ~ {r.period.end.slice(5)} <span className="muted">({r.period.days}일·{r.symbol_count}종목)</span></td>
                      <td className="reason">{overrideText(r.overrides, byId)}</td>
                      <td className={`r num tone-${tone(r.return_pct)}`}>{pct(r.return_pct)}</td>
                      <td className={`r num tone-${tone(r.benchmark_return_pct)}`}>{pct(r.benchmark_return_pct)}</td>
                      <td className="r num">{r.max_drawdown_pct.toFixed(2)}%</td>
                      <td className="r num">{r.trades}</td>
                      <td className="r num">{r.win_rate == null ? "–" : `${r.win_rate.toFixed(0)}%`}</td>
                      <td className="r num">{r.profit_factor ?? "–"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      {applying && result && (
        <Dialog title="이 값을 설정에 적용" onCancel={() => setApplying(false)}>
          <ul className="change-list">
            {Object.entries(result.overrides).map(([id, v]) => byId[id] && (
              <li key={id}><span>{byId[id].label}</span>
                <span className="num"><span className="muted">{show(byId[id], byId[id].value)}</span> → <strong>{show(byId[id], v)}</strong></span>
                <span className={`pill tone-${byId[id].apply === "now" ? "good" : "idle"}`}>{byId[id].apply === "now" ? "바로 적용" : "다음 거래일부터"}</span></li>
            ))}
          </ul>
          {result.trades < FEW_TRADES && <p className="warn-box">거래가 {result.trades}건뿐입니다. 이 결과만으로 설정을 바꾸면 우연에 맞춘 값일 수 있습니다.</p>}
          {view.env === "real" && <p className="warn-box">실전 계좌입니다. 바꾼 값으로 실제 주문이 나갑니다.</p>}
          <div className="dialog-actions">
            <button className="btn" onClick={() => setApplying(false)}>취소</button>
            <button className="btn primary" onClick={applySettings}>설정에 적용</button>
          </div>
        </Dialog>
      )}
      {toast && <div className="toast fixed ok" role="status" onClick={() => setToast("")}>{toast}</div>}
    </div>
  );
}

function overrideText(o: Record<string, SettingValue>, byId: Record<string, SettingField>): string {
  const parts = Object.entries(o ?? {}).map(([id, v]) => (byId[id] ? `${byId[id].label} ${show(byId[id], v)}` : id));
  return parts.length ? parts.join(", ") : "현재 설정";
}

function Result({ r, byId, onApply }: { r: BtResult; byId: Record<string, SettingField>; onApply: () => void }) {
  const [showAll, setShowAll] = useState(false);
  const excess = r.return_pct - r.benchmark_return_pct;
  const trades = showAll ? [...r.trade_list].reverse() : [...r.trade_list].reverse().slice(0, 50);
  const hasOverrides = Object.keys(r.overrides ?? {}).length > 0;
  return (
    <>
      <section className="panel bt-head">
        <div className="panel-head">
          <h2>결과</h2>
          <span className="panel-note">{r.period.start} ~ {r.period.end} · {r.period.days}거래일 · {r.symbols.length}종목 ·
            시작 {usd(r.start_cash)} · 미끄러짐 {r.params.fill_slippage_pct}% · 계산 {r.elapsed_sec}초</span>
          {hasOverrides && <button className="btn small primary bt-apply" onClick={onApply}>이 값을 설정에 적용</button>}
        </div>
        <div className="bt-overrides small">{hasOverrides ? `바꾼 값: ${overrideText(r.overrides, byId)}` : "현재 설정 그대로 실행"}</div>
        {r.trades < FEW_TRADES && (
          <p className="notice-list bt-note">거래 {r.trades}건으로 표본이 작습니다. 기간·종목을 늘리거나 결과를 참고로만 보세요.</p>
        )}
      </section>

      <section className="kpis">
        <div className="kpi"><div className="kpi-label">수익률</div>
          <div className={`kpi-value num tone-${tone(r.return_pct)}`}>{pct(r.return_pct)}</div>
          <div className="kpi-sub">단순 보유 {pct(r.benchmark_return_pct)} · 차이 <span className={`tone-${tone(excess)}`}>{pct(excess)}</span></div></div>
        <div className="kpi"><div className="kpi-label">최대 낙폭</div>
          <div className="kpi-value num">{r.max_drawdown_pct.toFixed(2)}%</div>
          <div className="kpi-sub">일 마감 자산 기준{r.halts.length ? ` · 손실 차단 ${r.halts.length}회` : ""}</div></div>
        <div className="kpi"><div className="kpi-label">거래 · 승률</div>
          <div className="kpi-value num">{r.trades}건 · {r.win_rate == null ? "–" : `${r.win_rate.toFixed(0)}%`}</div>
          <div className="kpi-sub">평균 보유 {r.avg_hold_min == null ? "–" : `${r.avg_hold_min}분`}</div></div>
        <div className="kpi"><div className="kpi-label">손익비</div>
          <div className="kpi-value num">{r.profit_factor ?? "–"}</div>
          <div className="kpi-sub">평균 이익 {usd(r.avg_win)} · 평균 손실 {usd(r.avg_loss)}</div></div>
      </section>

      <section className="panel">
        <div className="panel-head"><h2>자산 변화</h2>
          <span className="legend"><i className="lg-strategy" />전략 <i className="lg-bench" />단순 보유 (선택 종목 같은 비중)</span></div>
        {r.equity_curve.length ? <EquityChart r={r} /> : <div className="empty">기간 안에 거래일이 없습니다.</div>}
      </section>

      <div className="split">
        <section className="panel">
          <div className="panel-head"><h2>청산 사유별</h2><span className="panel-note">실현 {usd(r.realized_pnl, { sign: true })} · 수수료 {usd(r.fees)}</span></div>
          <div className="table-scroll">
            <table>
              <thead><tr><th>사유</th><th className="r">건수</th><th className="r">손익</th></tr></thead>
              <tbody>
                {r.by_exit.map((e) => (
                  <tr key={e.exit}><td>{e.exit}</td><td className="r num">{e.count}</td>
                    <td className={`r num tone-${tone(e.pnl)}`}>{usd(e.pnl, { sign: true })}</td></tr>
                ))}
                {r.open_positions.length > 0 && (
                  <tr><td>미청산 (기간 끝 평가)</td><td className="r num">{r.open_positions.length}</td>
                    <td className={`r num tone-${tone(r.unrealized_pnl)}`}>{usd(r.unrealized_pnl, { sign: true })}</td></tr>
                )}
              </tbody>
            </table>
          </div>
          {Object.values(r.skipped).some(Boolean) && (
            <p className="muted small bt-skip">건너뛴 매수 신호: {Object.entries(r.skipped).filter(([, n]) => n).map(([k, n]) =>
              `${{ cash: "현금 부족", max_orders: "하루 주문 한도", buys_per_symbol: "종목당 매수 횟수", halt: "손실 차단" }[k] ?? k} ${n}`).join(" · ")}</p>
          )}
        </section>
        <section className="panel">
          <div className="panel-head"><h2>종목별</h2></div>
          <div className="table-scroll tall">
            <table>
              <thead><tr><th>종목</th><th className="r">거래</th><th className="r">승</th><th className="r">손익</th></tr></thead>
              <tbody>
                {r.by_symbol.map((s) => (
                  <tr key={s.symbol}><td>{s.symbol}</td><td className="r num">{s.trades}</td><td className="r num">{s.wins}</td>
                    <td className={`r num tone-${tone(s.pnl)}`}>{usd(s.pnl, { sign: true })}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section className="panel">
        <div className="panel-head"><h2>거래 내역</h2><span className="count">{r.trade_list.length}</span>
          <span className="panel-note">시각은 미국 동부(ET)</span>
          {r.trade_list.length > 50 && <button className="link" onClick={() => setShowAll(!showAll)}>{showAll ? "최근 50건만" : "전체 보기"}</button>}</div>
        {r.trade_list.length === 0 ? <div className="empty">이 조건에서는 거래가 없었습니다.</div> : (
          <div className="table-scroll tall">
            <table>
              <thead><tr><th>매수</th><th>매도</th><th>종목</th><th className="r">수량</th><th className="r">매수가</th><th className="r">매도가</th>
                <th className="r">손익</th><th className="r">보유</th><th>청산 사유</th></tr></thead>
              <tbody>
                {trades.map((t, i) => (
                  <tr key={i}>
                    <td className="num muted">{etTime(t.entry_time)}</td>
                    <td className="num muted">{etTime(t.exit_time)}</td>
                    <td>{t.symbol}</td>
                    <td className="r num">{t.qty}</td>
                    <td className="r num">{usd(t.entry_price)}</td>
                    <td className="r num">{usd(t.exit_price)}</td>
                    <td className={`r num tone-${tone(t.pnl)}`}>{usd(t.pnl, { sign: true })} <span className="small">({pct(t.pnl_pct)})</span></td>
                    <td className="r num">{holdText(t.hold_min)}</td>
                    <td className="reason" title={t.reason}>{t.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function etTime(iso: string): string {
  return new Date(iso).toLocaleString("ko-KR", { timeZone: "America/New_York", month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false });
}

function holdText(min: number): string {
  if (min < 60) return `${min}분`;
  if (min < 60 * 24) return `${Math.floor(min / 60)}시간 ${min % 60}분`;
  return `${Math.floor(min / 1440)}일`;
}

function EquityChart({ r }: { r: BtResult }) {
  const [ref, W] = useWidth<HTMLDivElement>(760);
  const [hover, setHover] = useState<number | null>(null);
  const H = 240, padL = 72, padR = 70, padT = 14, padB = 24;
  const c = r.equity_curve;
  const n = c.length;
  const vals = [r.start_cash, ...c.map((d) => d.equity), ...c.map((d) => d.benchmark)];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1) { lo -= 1; hi += 1; }
  const padV = (hi - lo) * 0.08;
  lo -= padV; hi += padV;
  const x = (i: number) => padL + (n === 1 ? (W - padL - padR) / 2 : (i / (n - 1)) * (W - padL - padR));
  const y = (v: number) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
  const line = (key: "equity" | "benchmark") => c.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join(" ");
  const ticks = [hi - padV, r.start_cash, lo + padV].filter((v, i, a) => a.findIndex((w) => Math.abs(w - v) < (hi - lo) * 0.08) === i);
  const step = n > 1 ? (W - padL - padR) / (n - 1) : W - padL - padR;
  const last = c[n - 1];
  const h = hover == null ? null : c[hover];
  return (
    <div className="chart-box" ref={ref}>
      <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="chart" role="img" aria-label="전략과 단순 보유의 일별 자산"
           onMouseLeave={() => setHover(null)}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={padL} x2={W - padR} y1={y(v)} y2={y(v)} className={Math.abs(v - r.start_cash) < 1e-6 ? "zero" : "grid"} />
            <text x={padL - 8} y={y(v) + 4} className="axis" textAnchor="end">{usd(v).replace(/\.\d+$/, "")}</text>
          </g>
        ))}
        <path d={line("benchmark")} className="line bench" />
        <path d={line("equity")} className="line" />
        <circle cx={x(n - 1)} cy={y(last.equity)} r={4} className="endpoint" />
        <text x={x(n - 1) + 8} y={y(last.equity) + 4} className="axis strong">전략</text>
        <text x={x(n - 1) + 8} y={y(last.benchmark) + (Math.abs(y(last.benchmark) - y(last.equity)) < 12 ? 16 : 4)} className="axis">단순 보유</text>
        {h && hover != null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={padT} y2={H - padB} className="crosshair" />
            <circle cx={x(hover)} cy={y(h.equity)} r={4} className="endpoint" />
            <circle cx={x(hover)} cy={y(h.benchmark)} r={4} className="endpoint bench" />
          </g>
        )}
        {c.map((d, i) => (
          <rect key={d.date} x={x(i) - step / 2} width={Math.max(step, 6)} y={padT} height={H - padT - padB} className="hit"
                onMouseEnter={() => setHover(i)} />
        ))}
        <text x={x(0)} y={H - 6} className="axis" textAnchor={n === 1 ? "middle" : "start"}>{c[0].date}</text>
        {n > 1 && <text x={x(n - 1)} y={H - 6} className="axis" textAnchor="end">{last.date}</text>}
      </svg>
      {h && (
        <div className="chart-tip" style={{ left: Math.min(x(hover!) + 12, W - 200) }}>
          <strong>{h.date}</strong>
          <div><i className="lg-strategy" />전략 {usd(h.equity)} ({pct((h.equity / r.start_cash - 1) * 100)})</div>
          <div><i className="lg-bench" />단순 보유 {usd(h.benchmark)} ({pct((h.benchmark / r.start_cash - 1) * 100)})</div>
          <div className="muted">보유 {h.positions}종목 · 현금 {usd(h.cash)}</div>
        </div>
      )}
    </div>
  );
}
