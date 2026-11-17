import { useEffect, useState } from "react";
import type { Annual as AnnualData } from "./types";
import { tone, usd, won } from "./format";
import { useWidth } from "./Reports";

const RATE_LABEL = { exact: "", near: "가까운 날 환율", current: "현재 환율", none: "환율 없음" } as const;

// 연간 정산: 수수료 차감 → 원화 양도차익 → 기본공제 → 예상 세금
export default function Annual() {
  const [year, setYear] = useState<number | null>(null);
  const [data, setData] = useState<AnnualData | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setError("");
    fetch(`/api/annual${year ? `?year=${year}` : ""}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d: AnnualData) => {
        setData(d);
        if (!year) setYear(d.year);
      })
      .catch(() => setError("연간 정산을 불러오지 못했습니다."));
  }, [year]);

  if (error) return <div className="empty">{error}</div>;
  if (!data) return <div className="empty">불러오는 중…</div>;

  const p = data.params;
  const used = p.basic_deduction_krw ? Math.max(0, data.gain_krw) / p.basic_deduction_krw : 0;

  return (
    <div className="reports">
      <div className="annual-bar">
        <label className="small muted" htmlFor="annual-year">연도</label>
        <select id="annual-year" value={data.year} onChange={(e) => setYear(Number(e.target.value))}>
          {data.years.map((y) => <option key={y} value={y}>{y}년</option>)}
        </select>
        <span className="muted small">
          결제일 기준 · 취득가 {data.cost_method === "fifo" ? "선입선출" : "이동평균"} · 수수료 {p.commission_pct}% · 세율 {p.tax_rate_pct}%
          <span className="hide-narrow"> (설정 탭에서 변경)</span>
        </span>
        <a className="btn small" href={`/api/annual/${data.year}/sells.csv`} download>매도 상세 CSV</a>
      </div>

      {data.warnings.length > 0 && (
        <ul className="notice-list">{data.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
      )}

      <section className="kpis">
        <div className="kpi"><div className="kpi-label">손익 (수수료 차감)</div>
          <div className={`kpi-value num tone-${tone(data.gain_usd)}`}>{usd(data.gain_usd, { sign: true })}</div>
          <div className="kpi-sub">매도 {data.sell_count}건 · 수수료 {usd(data.fees_usd)}</div></div>
        <div className="kpi"><div className="kpi-label">원화 양도차익</div>
          <div className={`kpi-value num tone-${tone(data.gain_krw)}`}>{won(data.gain_krw, { sign: true })}</div>
          <div className="kpi-sub">이익 {data.wins}건 · 손실 {data.losses}건 상계</div></div>
        <div className="kpi"><div className="kpi-label">예상 세금</div>
          <div className="kpi-value num">{won(data.tax_krw)}</div>
          <div className="kpi-sub">{data.tax_krw ? `과세표준 ${won(data.taxable_krw)} × ${p.tax_rate_pct}%` : `공제 ${won(data.deduction_left_krw)} 남음`}</div></div>
        <div className="kpi"><div className="kpi-label">세후 손익</div>
          <div className={`kpi-value num tone-${tone(data.after_tax_krw)}`}>{won(data.after_tax_krw, { sign: true })}</div>
          <div className="kpi-sub">{data.filing}</div></div>
      </section>

      <div className="split">
        <section className="panel">
          <div className="panel-head"><h2>월별 원화 양도차익</h2><span className="panel-note">막대: 그달 손익 · 선: 누적 · 점선: 기본공제</span></div>
          <MonthlyChart data={data} />
        </section>
        <section className="panel">
          <div className="panel-head"><h2>세금 계산</h2></div>
          <dl className="tax-steps">
            <dt>원화 양도차익</dt><dd className={`num tone-${tone(data.gain_krw)}`}>{won(data.gain_krw, { sign: true })}</dd>
            <dt>− 기본공제</dt><dd className="num">{won(Math.min(Math.max(data.gain_krw, 0), p.basic_deduction_krw))}</dd>
            <dt>= 과세표준</dt><dd className="num">{won(data.taxable_krw)}</dd>
            <dt>× 세율 {p.tax_rate_pct}%</dt><dd className="num strong">{won(data.tax_krw)}</dd>
            <dt>세후 손익</dt><dd className={`num strong tone-${tone(data.after_tax_krw)}`}>{won(data.after_tax_krw, { sign: true })}</dd>
          </dl>
          <div className="deduction">
            <div className="small muted">기본공제 {won(p.basic_deduction_krw)} 중 {Math.min(100, used * 100).toFixed(0)}% 사용</div>
            <div className="bar"><div className={`fill level-${used >= 1 ? "warn" : "good"}`}
                                       style={{ width: `${Math.min(100, used * 100)}%` }} /></div>
          </div>
          <p className="muted small tax-note">
            추정치입니다. 다른 증권사 해외주식 손익과 합산해 신고하며, 배당·환전 수수료는 포함하지 않았습니다.
            실제 신고는 증권사 양도세 계산 자료로 확인하세요.
          </p>
        </section>
      </div>

      <section className="panel">
        <div className="panel-head"><h2>매도 내역</h2><span className="count">{data.sells.length}</span>
          <span className="panel-note">취득가는 매수 수수료 포함, 원화는 결제일 환율</span></div>
        {data.sells.length === 0 ? <div className="empty">{data.year}년에 결제된 매도가 없습니다.</div> : (
          <div className="table-scroll tall">
            <table>
              <thead>
                <tr><th>결제일</th><th>종목</th><th className="r">수량</th><th className="r">매도금액</th><th className="r">수수료</th>
                  <th className="r">취득가</th><th className="r">손익(USD)</th><th className="r">환율</th><th className="r">양도차익(원)</th><th>비고</th></tr>
              </thead>
              <tbody>
                {[...data.sells].reverse().map((s, i) => (
                  <tr key={i}>
                    <td className="num">{s.settle_date}<div className="small muted">체결 {s.trade_date.slice(5)}</div></td>
                    <td>{s.symbol}</td>
                    <td className="r num">{s.qty}</td>
                    <td className="r num">{usd(s.sell_amount)}</td>
                    <td className="r num muted">{usd(s.sell_fee)}</td>
                    <td className="r num">{usd(s.cost_usd)}</td>
                    <td className={`r num tone-${tone(s.gain_usd)}`}>{usd(s.gain_usd, { sign: true })}</td>
                    <td className="r num">{s.rate ? s.rate.toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "–"}
                      {s.rate_source !== "exact" && <div className="small warn-text">{RATE_LABEL[s.rate_source]}</div>}</td>
                    <td className={`r num tone-${tone(s.gain_krw)}`}>{won(s.gain_krw, { sign: true })}</td>
                    <td className="reason" title={s.note}>{s.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {data.open_lots.length > 0 && (
        <section className="panel">
          <div className="panel-head"><h2>아직 팔지 않은 매수분</h2><span className="panel-note">다음 매도의 취득가가 됩니다</span></div>
          <div className="table-scroll">
            <table>
              <thead><tr><th>종목</th><th className="r">수량</th><th className="r">취득가(USD)</th><th className="r">취득가(원)</th></tr></thead>
              <tbody>
                {data.open_lots.map((l) => (
                  <tr key={l.symbol}><td>{l.symbol}</td><td className="r num">{l.qty}</td>
                    <td className="r num">{usd(l.cost_usd)}</td><td className="r num">{won(l.cost_krw)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

function MonthlyChart({ data }: { data: AnnualData }) {
  const [ref, W] = useWidth<HTMLDivElement>(640);
  const H = 240, padL = 64, padR = 12, padT = 14, padB = 24;
  const months = data.monthly;
  let run = 0;
  const cum = months.map((m) => (run += m.gain_krw));
  const ded = data.params.basic_deduction_krw;
  const vals = [0, ...months.map((m) => m.gain_krw), ...cum, ded];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1) hi = lo + 1;
  const pad = (hi - lo) * 0.06;
  lo -= lo < 0 ? pad : 0;
  hi += pad;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const y = (v: number) => padT + (1 - (v - lo) / (hi - lo)) * plotH;
  const step = plotW / 12;
  const cx = (i: number) => padL + step * (i + 0.5);
  const bw = Math.min(28, step * 0.55);
  const lastIdx = months.reduce((a, m, i) => (m.sells ? i : a), -1);
  const line = cum.slice(0, lastIdx + 1).map((v, i) => `${i ? "L" : "M"}${cx(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const ticks = [hi - pad, 0, ...(lo < 0 ? [lo] : [])].filter((v, i, a) => a.indexOf(v) === i);
  const short = (v: number) => (Math.abs(v) >= 1e4 ? `${Math.round(v / 1e4).toLocaleString("ko-KR")}만` : `${Math.round(v)}`);

  return (
    <div className="chart-box" ref={ref}>
      <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="chart" role="img" aria-label="월별 원화 양도차익과 누적">
        {ticks.map((v) => (
          <g key={v}>
            <line x1={padL} x2={W - padR} y1={y(v)} y2={y(v)} className="grid" />
            <text x={padL - 8} y={y(v) + 4} className="axis" textAnchor="end">{v === 0 ? "0" : short(v)}</text>
          </g>
        ))}
        <line x1={padL} x2={W - padR} y1={y(0)} y2={y(0)} className="zero" />
        {ded > 0 && (
          <g>
            <line x1={padL} x2={W - padR} y1={y(ded)} y2={y(ded)} className="ref-line" />
            <text x={W - padR} y={y(ded) - 5} className="axis" textAnchor="end">기본공제 {short(ded)}</text>
          </g>
        )}
        {months.map((m, i) => {
          const top = y(Math.max(m.gain_krw, 0)), bottom = y(Math.min(m.gain_krw, 0));
          return (
            <g key={m.month}>
              {m.sells > 0 && (
                <rect x={cx(i) - bw / 2} width={bw} y={top} height={Math.max(bottom - top, 1)} rx={2}
                      className={`pnl-bar tone-${tone(m.gain_krw)}`} />
              )}
              {/* 마우스를 올릴 영역은 막대보다 넓게 (월 전체 폭) */}
              <rect x={cx(i) - step / 2} width={step} y={padT} height={plotH} className="hit">
                <title>{`${m.month}월 · 매도 ${m.sells}건\n양도차익 ${won(m.gain_krw, { sign: true })}\n누적 ${won(cum[i], { sign: true })}\n수수료 ${usd(m.fees_usd)}`}</title>
              </rect>
              <text x={cx(i)} y={H - 6} className="axis" textAnchor="middle">{m.month}</text>
            </g>
          );
        })}
        {lastIdx >= 0 && <path d={line} className="line" />}
        {lastIdx >= 0 && <circle cx={cx(lastIdx)} cy={y(cum[lastIdx])} r={4} className="endpoint" />}
        {lastIdx >= 0 && (
          <text x={cx(lastIdx) + (lastIdx > 8 ? -8 : 8)} y={y(cum[lastIdx]) - 8} className="axis strong"
                textAnchor={lastIdx > 8 ? "end" : "start"}>누적 {short(cum[lastIdx])}</text>
        )}
      </svg>
    </div>
  );
}
