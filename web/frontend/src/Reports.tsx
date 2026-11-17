import { useEffect, useRef, useState } from "react";
import type { DayReport, SummaryDay } from "./types";
import { pct, tone, usd } from "./format";
import Annual from "./Annual";

// 정산 탭: 일별 / 연간·세금 (주소 #reports/annual 로 바로 열 수 있다)
export default function Reports() {
  const [view, setView] = useState<"daily" | "annual">(() => (location.hash === "#reports/annual" ? "annual" : "daily"));
  const pick = (v: "daily" | "annual") => {
    setView(v);
    history.replaceState(null, "", v === "annual" ? "#reports/annual" : "#reports");
  };
  return (
    <div className="reports">
      <div className="seg view-switch" role="tablist" aria-label="정산 보기">
        <button role="tab" aria-selected={view === "daily"} className={view === "daily" ? "on" : ""} onClick={() => pick("daily")}>일별</button>
        <button role="tab" aria-selected={view === "annual"} className={view === "annual" ? "on" : ""} onClick={() => pick("annual")}>연간·세금</button>
      </div>
      {view === "daily" ? <DailyReports /> : <Annual />}
    </div>
  );
}

function DailyReports() {
  const [days, setDays] = useState<SummaryDay[] | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/reports")
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => {
        setDays(d.days);
        if (d.days.length) setSelected(d.days[d.days.length - 1].date);
      })
      .catch(() => setError("정산 데이터를 불러오지 못했습니다."));
  }, []);

  if (error) return <div className="empty">{error}</div>;
  if (!days) return <div className="empty">불러오는 중…</div>;
  if (!days.length) return <div className="empty">아직 정산 기록이 없습니다. 거래일이 끝나면 자동으로 쌓입니다.</div>;

  const total = days.reduce((s, d) => s + d.realized_pnl, 0);
  const wins = days.reduce((s, d) => s + d.wins, 0);
  const losses = days.reduce((s, d) => s + d.losses, 0);
  const first = days[0].start_equity;
  const last = days[days.length - 1].end_equity;

  return (
    <div className="reports">
      <section className="kpis">
        <div className="kpi"><div className="kpi-label">기간</div><div className="kpi-value">{days.length}일</div>
          <div className="kpi-sub">{days[0].date} ~ {days[days.length - 1].date}</div></div>
        <div className="kpi"><div className="kpi-label">누적 실현손익</div>
          <div className={`kpi-value num tone-${tone(total)}`}>{usd(total, { sign: true })}</div>
          <div className="kpi-sub">수수료·세금 제외</div></div>
        <div className="kpi"><div className="kpi-label">총자산 변화</div>
          <div className={`kpi-value num tone-${tone(last - first)}`}>{pct(first ? (last / first - 1) * 100 : null)}</div>
          <div className="kpi-sub">{usd(first)} → {usd(last)}</div></div>
        <div className="kpi"><div className="kpi-label">승률</div>
          <div className="kpi-value num">{wins + losses ? `${((wins / (wins + losses)) * 100).toFixed(0)}%` : "–"}</div>
          <div className="kpi-sub">익절 {wins} · 손절 {losses}</div></div>
      </section>

      <section className="panel">
        <div className="panel-head"><h2>일별 총자산과 실현손익</h2><span className="panel-note">막대를 누르면 그날 상세</span></div>
        <DailyChart days={days} selected={selected} onSelect={setSelected} />
      </section>

      <section className="panel">
        <div className="panel-head"><h2>일별 요약</h2></div>
        <div className="table-scroll tall">
          <table>
            <thead>
              <tr><th>날짜</th><th className="r">주문</th><th className="r">체결</th><th className="r">실현손익</th>
                <th className="r">익절/손절</th><th className="r">총자산</th><th className="r">수익률</th></tr>
            </thead>
            <tbody>
              {[...days].reverse().map((d) => (
                <tr key={d.date} className={`clickable ${selected === d.date ? "selected" : ""}`} onClick={() => setSelected(d.date)}>
                  <td className="num">{d.date}</td>
                  <td className="r num">{d.orders}</td>
                  <td className="r num">{d.filled_orders}</td>
                  <td className={`r num tone-${tone(d.realized_pnl)}`}>{usd(d.realized_pnl, { sign: true })}</td>
                  <td className="r num">{d.wins}/{d.losses}</td>
                  <td className="r num">{usd(d.end_equity)}</td>
                  <td className={`r num tone-${tone(d.return_pct)}`}>{pct(d.return_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {selected && <DayDetail day={selected} />}
    </div>
  );
}

// 컨테이너 실제 폭으로 그려서 글자 크기가 화면 폭에 따라 커지거나 작아지지 않게 한다
export function useWidth<T extends HTMLElement>(fallback: number) {
  const ref = useRef<T>(null);
  const [w, setW] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(320, Math.floor(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

function DailyChart({ days, selected, onSelect }: { days: SummaryDay[]; selected: string | null; onSelect: (d: string) => void }) {
  const [ref, W] = useWidth<HTMLDivElement>(760);
  const H = 220, padL = 64, padR = 12, padT = 12, lineH = 120, gap = 16, barH = 56;
  const n = days.length;
  const x = (i: number) => padL + (n === 1 ? (W - padL - padR) / 2 : (i / (n - 1)) * (W - padL - padR));
  const eqs = days.map((d) => d.end_equity);
  let lo = Math.min(...eqs), hi = Math.max(...eqs);
  if (hi - lo < 1) { lo -= 1; hi += 1; }
  const y = (v: number) => padT + (1 - (v - lo) / (hi - lo)) * lineH;
  const maxAbs = Math.max(1, ...days.map((d) => Math.abs(d.realized_pnl)));
  const barMid = padT + lineH + gap + barH / 2;
  const bw = Math.max(3, Math.min(24, ((W - padL - padR) / Math.max(n, 1)) * 0.6));
  const path = days.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.end_equity).toFixed(1)}`).join(" ");
  const area = `${path} L${x(n - 1).toFixed(1)},${padT + lineH} L${x(0).toFixed(1)},${padT + lineH} Z`;

  return (
    <div className="chart-box" ref={ref}>
      <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="chart" role="img" aria-label="일별 총자산과 실현손익">
        {[hi, (hi + lo) / 2, lo].map((v) => (
          <g key={v}>
            <line x1={padL} x2={W - padR} y1={y(v)} y2={y(v)} className="grid" />
            <text x={padL - 8} y={y(v) + 4} className="axis" textAnchor="end">{usd(v).replace(/\.\d+$/, "")}</text>
          </g>
        ))}
        <path d={area} className="area" />
        <path d={path} className="line" />
        <circle cx={x(n - 1)} cy={y(eqs[n - 1])} r={4} className="endpoint" />
        <line x1={padL} x2={W - padR} y1={barMid} y2={barMid} className="grid" />
        <text x={padL - 8} y={barMid + 4} className="axis" textAnchor="end">실현 0</text>
        {days.map((d, i) => {
          const h = (Math.abs(d.realized_pnl) / maxAbs) * (barH / 2);
          return (
            <rect key={d.date} x={x(i) - bw / 2} width={bw} y={d.realized_pnl >= 0 ? barMid - h : barMid}
                  height={Math.max(h, 1)} className={`pnl-bar tone-${tone(d.realized_pnl)} ${selected === d.date ? "sel" : ""}`}
                  onClick={() => onSelect(d.date)}>
              <title>{`${d.date} 실현 ${usd(d.realized_pnl, { sign: true })} · 총자산 ${usd(d.end_equity)}`}</title>
            </rect>
          );
        })}
        <text x={x(0)} y={H - 4} className="axis" textAnchor={n === 1 ? "middle" : "start"}>{days[0].date}</text>
        {n > 1 && <text x={x(n - 1)} y={H - 4} className="axis" textAnchor="end">{days[n - 1].date}</text>}
      </svg>
    </div>
  );
}

function DayDetail({ day }: { day: string }) {
  const [rep, setRep] = useState<DayReport | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setRep(null);
    setError("");
    fetch(`/api/reports/${day}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setRep)
      .catch(() => setError(`${day} 보고서를 불러오지 못했습니다.`));
  }, [day]);

  if (error) return <div className="empty">{error}</div>;
  if (!rep) return <div className="empty">불러오는 중…</div>;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{rep.date} 상세</h2>
        <span className="panel-note">
          매수 {usd(rep.buy_amount)} · 매도 {usd(rep.sell_amount)} · 실현 {usd(rep.realized_pnl, { sign: true })} ·
          승률 {rep.win_rate.toFixed(0)}% · 마감 보유 {rep.holdings.length}종목
        </span>
      </div>
      {rep.trades.length === 0 ? <div className="empty">이날 주문이 없습니다.</div> : (
        <div className="table-scroll tall">
          <table>
            <thead>
              <tr><th>시각</th><th>구분</th><th>종목</th><th className="r">체결/주문</th><th className="r">체결가</th>
                <th>상태</th><th className="r">실현손익</th><th>사유</th></tr>
            </thead>
            <tbody>
              {rep.trades.map((t, i) => {
                const pnl = t.realized_pnl === "" ? null : Number(t.realized_pnl);
                return (
                  <tr key={i}>
                    <td className="num muted">{t.placed_at.slice(11, 19)}</td>
                    <td className={t.side === "BUY" ? "tone-up" : "tone-down"}>{t.side === "BUY" ? "매수" : "매도"}</td>
                    <td>{t.symbol}</td>
                    <td className="r num">{Number(t.filled_qty)}/{t.qty}</td>
                    <td className="r num">{Number(t.avg_fill_price) ? usd(Number(t.avg_fill_price)) : "–"}</td>
                    <td>{t.status}</td>
                    <td className={`r num tone-${tone(pnl)}`}>{pnl == null ? "–" : usd(pnl, { sign: true })}</td>
                    <td className="reason" title={t.message || t.reason}>{t.message || t.reason}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
