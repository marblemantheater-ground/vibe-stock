import { useEffect, useRef, useState } from "react";
import { pct, tone, usd } from "./format";

// 프리마켓 시뮬레이션: 모의투자 프리마켓 신호를 가상 계좌에서 체결한 기록 (실제 계좌·정산과 별개)

type SimDay = {
  date: string; closed: boolean; start_cash: number; end_value: number; pnl: number; realized_pnl: number;
  return_pct: number | null; fees: number; buys: number; sells: number; wins: number; losses: number;
  slippage_pct: number | null; open_positions: number;
};
type SimTrade = {
  time: string; symbol: string; side: "BUY" | "SELL"; qty: number; signal_price: number; quote: number; price: number;
  amount: number; fee: number; pnl: number | null; reason: string; preset: string;
};
type SimDetail = SimDay & {
  cash: number; updated_at: string | null;
  positions: { symbol: string; exchange: string; qty: number; avg_price: number; last_price: number; bought_at: string; pnl: number }[];
  trades: SimTrade[];
};
type SimList = { env: string; days: SimDay[]; enabled: boolean | null; slippage_pct: number | null; active: boolean };

export default function Sim() {
  const [list, setList] = useState<SimList | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      fetch("/api/sim")
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((d: SimList) => {
          setList(d);
          setError("");
          if (d.days.length) setSelected((s) => s ?? d.days[d.days.length - 1].date);
        })
        .catch(() => setError("프리마켓 시뮬레이션 기록을 불러오지 못했습니다."));
    load();
    const t = setInterval(load, 5_000);
    return () => clearInterval(t);
  }, []);

  if (error) return <div className="empty">{error}</div>;
  if (!list) return <div className="empty">불러오는 중…</div>;

  const days = list.days;
  const total = days.reduce((s, d) => s + d.pnl, 0);
  const fees = days.reduce((s, d) => s + d.fees, 0);
  const wins = days.reduce((s, d) => s + d.wins, 0);
  const losses = days.reduce((s, d) => s + d.losses, 0);

  return (
    <div className="reports">
      <p className="day-note">
        모의투자는 KIS 모의 서버가 프리마켓 주문을 받지 않아, 프리마켓 신호를 <b>가상 계좌</b>에서 체결한 결과입니다
        (시작 금액 = 그날 모의 계좌 주문가능 USD, 지금 켜진 거래 옵션·급등주 선정 그대로).
        체결가는 호가보다 {list.slippage_pct ?? "–"}% 불리하게 잡고 수수료를 뺍니다. 정규장이 시작되면 남은 종목은 마지막 가격으로 판 것으로 정리합니다.
        실제 계좌·정산·손실 한도와는 별개입니다.
        {list.enabled === false && <b> 지금은 설정에서 꺼져 있습니다.</b>}
      </p>

      {!days.length ? (
        <div className="empty">아직 기록이 없습니다. 모의투자로 프리마켓(한국 18:00~23:30, 서머타임 17:00~22:30)에 실행하면 쌓입니다.</div>
      ) : (
        <>
          <section className="kpis">
            <div className="kpi"><div className="kpi-label">기간</div><div className="kpi-value">{days.length}일</div>
              <div className="kpi-sub">{days[0].date} ~ {days[days.length - 1].date}</div></div>
            <div className="kpi"><div className="kpi-label">누적 손익</div>
              <div className={`kpi-value num tone-${tone(total)}`}>{usd(total, { sign: true })}</div>
              <div className="kpi-sub">수수료 {usd(fees)} 포함</div></div>
            <div className="kpi"><div className="kpi-label">승률</div>
              <div className="kpi-value num">{wins + losses ? `${((wins / (wins + losses)) * 100).toFixed(0)}%` : "–"}</div>
              <div className="kpi-sub">이익 {wins} · 손실 {losses}</div></div>
            <div className="kpi"><div className="kpi-label">상태</div>
              <div className="kpi-value">{list.active ? "진행 중" : "대기"}</div>
              <div className="kpi-sub">{list.active ? "프리마켓 가상 매매 중" : "다음 프리마켓에 시작"}</div></div>
          </section>

          <section className="panel">
            <div className="panel-head"><h2>일별 결과</h2><span className="panel-note">행을 누르면 그날 상세</span></div>
            <div className="table-scroll tall">
              <table>
                <thead>
                  <tr><th>날짜</th><th className="r">시작</th><th className="r">끝</th><th className="r">손익</th><th className="r">수익률</th>
                    <th className="r">매수/매도</th><th className="r">이익/손실</th><th className="r">수수료</th></tr>
                </thead>
                <tbody>
                  {[...days].reverse().map((d) => (
                    <tr key={d.date} className={`clickable ${selected === d.date ? "selected" : ""}`} onClick={() => setSelected(d.date)}>
                      <td className="num">{d.date}{!d.closed && <span className="pill tone-info day-live">진행 중</span>}</td>
                      <td className="r num">{usd(d.start_cash)}</td>
                      <td className="r num">{usd(d.end_value)}</td>
                      <td className={`r num tone-${tone(d.pnl)}`}>{usd(d.pnl, { sign: true })}</td>
                      <td className={`r num tone-${tone(d.return_pct)}`}>{pct(d.return_pct)}</td>
                      <td className="r num">{d.buys}/{d.sells}</td>
                      <td className="r num">{d.wins}/{d.losses}</td>
                      <td className="r num">{usd(d.fees)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          {selected && <SimDayDetail day={selected} version={days.find((d) => d.date === selected)} />}
        </>
      )}
    </div>
  );
}

function SimDayDetail({ day, version }: { day: string; version?: SimDay }) {
  const [rep, setRep] = useState<SimDetail | null>(null);
  const [error, setError] = useState("");
  const shown = useRef("");
  useEffect(() => {
    if (shown.current !== day) setRep(null);
    shown.current = day;
    setError("");
    fetch(`/api/sim/${day}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setRep)
      .catch(() => setError(`${day} 기록을 불러오지 못했습니다.`));
  }, [day, version?.buys, version?.sells, version?.end_value, version?.closed]);

  if (error) return <div className="empty">{error}</div>;
  if (!rep) return <div className="empty">불러오는 중…</div>;
  return (
    <>
      {rep.positions.length > 0 && (
        <section className="panel">
          <div className="panel-head"><h2>가상 보유</h2>
            <span className="panel-note">현금 {usd(rep.cash)} · 평가손익은 매수 수수료 포함 원가 기준</span></div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>종목</th><th className="r">수량</th><th className="r">평균 단가</th><th className="r">현재가</th>
                  <th className="r">평가손익</th><th>산 시각</th></tr>
              </thead>
              <tbody>
                {rep.positions.map((p) => (
                  <tr key={p.symbol}>
                    <td>{p.symbol}</td>
                    <td className="r num">{p.qty}</td>
                    <td className="r num">{usd(p.avg_price)}</td>
                    <td className="r num">{usd(p.last_price)}</td>
                    <td className={`r num tone-${tone(p.pnl)}`}>{usd(p.pnl, { sign: true })}</td>
                    <td className="num muted">{p.bought_at.slice(11, 19)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <section className="panel">
        <div className="panel-head">
          <h2>{rep.date} 가상 체결{!rep.closed && <span className="pill tone-info day-live">진행 중</span>}</h2>
          <span className="panel-note">
            {usd(rep.start_cash)} → {usd(rep.end_value)} ({pct(rep.return_pct)}) · 수수료 {usd(rep.fees)} · 불리하게 {rep.slippage_pct ?? "–"}%
          </span>
        </div>
        {rep.trades.length === 0 ? <div className="empty">이날 가상 체결이 없습니다.</div> : (
          <div className="table-scroll tall">
            <table>
              <thead>
                <tr><th>시각</th><th>구분</th><th>종목</th><th className="r">수량</th><th className="r">호가</th>
                  <th className="r">체결가</th><th className="r">금액</th><th className="r">수수료</th><th className="r">손익</th><th>사유</th></tr>
              </thead>
              <tbody>
                {rep.trades.map((t, i) => (
                  <tr key={i}>
                    <td className="num muted">{t.time.slice(11, 19)}</td>
                    <td className={t.side === "BUY" ? "tone-up" : "tone-down"}>{t.side === "BUY" ? "매수" : "매도"}</td>
                    <td>{t.symbol}</td>
                    <td className="r num">{t.qty}</td>
                    <td className="r num">{usd(t.quote)}</td>
                    <td className="r num">{usd(t.price)}</td>
                    <td className="r num">{usd(t.amount)}</td>
                    <td className="r num">{usd(t.fee)}</td>
                    <td className={`r num tone-${tone(t.pnl)}`}>{t.pnl == null ? "–" : usd(t.pnl, { sign: true })}</td>
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
