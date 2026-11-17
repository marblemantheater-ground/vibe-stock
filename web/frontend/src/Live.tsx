import { useState, type ReactNode } from "react";
import type { Order, Position, Risk, Snapshot, Watch } from "./types";
import { krw, num, pct, tone, usd } from "./format";

export default function Live({ snap }: { snap: Snapshot }) {
  const a = snap.account;
  return (
    <div className="live">
      <section className="kpis">
        <Kpi label="총자산" value={usd(a?.equity)} sub={a ? `${krw(a.equity_krw)} · 환율 ${num(a.exchange_rate, 2)}` : ""} />
        <Kpi label="오늘 손익" value={usd(a?.day_pnl, { sign: true })} tone={tone(a?.day_pnl)}
             sub={a?.day_start_equity ? `${pct(a.day_pnl_pct)} · 시작 ${usd(a.day_start_equity)}` : "거래일 시작 전"} />
        <Kpi label="주문가능" value={usd(a?.orderable_usd)} sub={`보유 평가 ${usd(a?.holdings_eval)}`} />
        <RiskGauges risk={snap.risk} />
      </section>

      <Panel title="보유 종목" count={snap.positions.length}
             note={`익절 +${snap.strategy.take_profit_pct}% · 손절 −${snap.strategy.stop_loss_pct}%`}>
        <Positions rows={snap.positions} />
      </Panel>

      <div className="split">
        <Panel title="오늘 주문" count={snap.orders.length}>
          <Orders rows={snap.orders} />
        </Panel>
        <Panel title="최근 이벤트" count={snap.events.length}>
          <Events snap={snap} />
        </Panel>
      </div>

      <Panel title="감시 종목" count={snap.watch.length}
             note={`${snap.strategy.bar_minutes}분봉 MA${snap.strategy.short_window}/MA${snap.strategy.long_window} · 등락률 ${snap.strategy.min_change_rate}~${snap.strategy.max_change_rate}% · 1회 ${usd(snap.strategy.budget_per_trade_usd)} · 최대 ${snap.strategy.max_positions}종목 · ${snap.strategy.sessions.join("/")}`}>
        <WatchTable rows={snap.watch} phase={snap.status.phase} />
      </Panel>
    </div>
  );
}

function Kpi({ label, value, sub, tone: t = "flat" }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value num tone-${t}`}>{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

function RiskGauges({ risk }: { risk: Risk }) {
  return (
    <div className="kpi">
      <div className="kpi-label">손실 한도</div>
      <Gauge label="하루 손실" used={risk.daily_loss_pct} limit={risk.daily_limit_pct} />
      <Gauge label="고점 대비" used={risk.drawdown_pct} limit={risk.max_drawdown_pct} />
    </div>
  );
}

function Gauge({ label, used, limit }: { label: string; used: number | null; limit: number }) {
  if (!limit) return <div className="gauge"><span>{label}</span><span className="muted">꺼짐</span></div>;
  const ratio = used == null ? 0 : Math.min(used / limit, 1);
  const level = ratio >= 1 ? "bad" : ratio >= 0.5 ? "warn" : "good";
  return (
    <div className="gauge">
      <span>{label}</span>
      <div className="bar" title={`${pct(used ?? 0, { sign: false })} / 한도 ${limit}%`}>
        <div className={`fill level-${level}`} style={{ width: `${ratio * 100}%` }} />
      </div>
      <span className="num small">{used == null ? "–" : `${used.toFixed(1)}`}/{limit}%</span>
    </div>
  );
}

function Panel({ title, count, note, children }: { title: string; count?: number; note?: string; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}{count != null && <span className="count">{count}</span>}</h2>
        {note && <span className="panel-note">{note}</span>}
      </div>
      {children}
    </section>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

function Positions({ rows }: { rows: Position[] }) {
  if (!rows.length) return <Empty>보유 종목이 없습니다.</Empty>;
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr><th>종목</th><th className="r">수량</th><th className="r">평균단가</th><th className="r">현재가</th>
            <th className="r">평가손익</th><th className="r">수익률</th><th>손절까지 ← 현재 → 익절까지</th></tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.symbol}>
              <td><Sym symbol={p.symbol} sub={p.name || p.exchange} /></td>
              <td className="r num">{num(p.qty, 4)}</td>
              <td className="r num">{usd(p.avg_price)}</td>
              <td className="r num">
                {usd(p.price)}
                {!p.live && <span className="tag" title="실시간 시세가 아닌 잔고 조회 가격">조회가</span>}
              </td>
              <td className={`r num tone-${tone(p.pnl)}`}>{usd(p.pnl, { sign: true })}</td>
              <td className={`r num tone-${tone(p.pnl_pct)}`}>{pct(p.pnl_pct)}</td>
              <td><ExitMeter p={p} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// 손절가(왼쪽)~익절가(오른쪽) 사이에서 현재 수익률 위치
function ExitMeter({ p }: { p: Position }) {
  const span = p.to_tp_pct + p.to_sl_pct;
  const pos = span > 0 ? Math.min(Math.max(p.to_sl_pct / span, 0), 1) : 0.5;
  return (
    <div className="exit-meter" title={`손절 ${usd(p.sl_price)} · 익절 ${usd(p.tp_price)}`}>
      <span className="num small tone-down">{p.to_sl_pct.toFixed(1)}%</span>
      <div className="track"><i style={{ left: `${pos * 100}%` }} /></div>
      <span className="num small tone-up">{p.to_tp_pct.toFixed(1)}%</span>
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  FILLED: "good", PARTIAL: "warn", SUBMITTED: "info", PENDING: "info", UNKNOWN: "warn",
  CANCELED: "idle", REJECTED: "bad", DRY_RUN: "idle",
};

function Orders({ rows }: { rows: Order[] }) {
  if (!rows.length) return <Empty>오늘 주문이 없습니다.</Empty>;
  return (
    <div className="table-scroll tall">
      <table>
        <thead>
          <tr><th>시각</th><th>구분</th><th>종목</th><th className="r">수량</th><th className="r">지정가</th><th>상태</th><th>사유</th></tr>
        </thead>
        <tbody>
          {rows.map((o, i) => (
            <tr key={o.odno || `${o.time}-${o.symbol}-${i}`}>
              <td className="num muted">{o.time}</td>
              <td className={o.side === "BUY" ? "tone-up" : "tone-down"}>{o.side === "BUY" ? "매수" : "매도"}</td>
              <td>{o.symbol}{o.external && <span className="tag">외부</span>}</td>
              <td className="r num">{o.filled_qty ? `${num(o.filled_qty, 4)}/` : ""}{o.qty}</td>
              <td className="r num">{usd(o.limit_price)}</td>
              <td><span className={`pill tone-${STATUS_TONE[o.status] ?? "idle"}`}>{o.status_label}</span></td>
              <td className="reason" title={o.message || o.reason}>{o.message || o.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Events({ snap }: { snap: Snapshot }) {
  const rows = [...snap.events].reverse();
  if (!rows.length) return <Empty>아직 이벤트가 없습니다. 신호·주문·체결·경고가 여기에 쌓입니다.</Empty>;
  return (
    <ul className="events">
      {rows.map((e) => (
        <li key={e.id} className={`lv-${e.level.toLowerCase()}`}>
          <span className="num muted">{e.time}</span>
          <span>{e.message}</span>
        </li>
      ))}
    </ul>
  );
}

const SOURCES = ["전체", "보유", "미체결", "관심", "선정"] as const;

function WatchTable({ rows, phase }: { rows: Watch[]; phase: string }) {
  const [filter, setFilter] = useState<(typeof SOURCES)[number]>("전체");
  if (!rows.length)
    return <Empty>{phase === "trading" ? "감시 중인 종목이 없습니다." : "세션이 시작되면 실시간 감시 종목이 표시됩니다."}</Empty>;
  const shown = filter === "전체" ? rows : rows.filter((r) => r.source === filter);
  return (
    <>
      <div className="filters">
        {SOURCES.map((s) => {
          const n = s === "전체" ? rows.length : rows.filter((r) => r.source === s).length;
          return (
            <button key={s} className={`filter ${filter === s ? "on" : ""}`} onClick={() => setFilter(s)} disabled={!n}>
              {s} <span className="muted">{n}</span>
            </button>
          );
        })}
      </div>
      <div className="table-scroll tall">
        <table>
          <thead>
            <tr><th>종목</th><th>출처</th><th className="r">현재가</th><th className="r">등락률</th>
              <th className="r">체결강도</th><th className="r">단기 / 장기 이평</th><th>추세</th></tr>
          </thead>
          <tbody>
            {shown.map((w) => (
              <tr key={w.symbol}>
                <td><Sym symbol={w.symbol} sub={w.exchange} /></td>
                <td><span className={`src src-${w.source}`}>{w.source}</span>{!w.buy_target && <span className="tag" title="매수 대상이 아님 (보유·미체결 때문에 감시)">매도만</span>}</td>
                <td className="r num">{usd(w.price)}</td>
                <td className={`r num tone-${tone(w.change_rate)}`}>{pct(w.change_rate)}</td>
                <td className="r num">{w.strength == null ? "–" : w.strength.toFixed(1)}</td>
                <td className="r num muted">{w.ma_short == null ? "–" : `${usd(w.ma_short)} / ${usd(w.ma_long)}`}</td>
                <td>
                  {w.trend ? <span className={`trend tone-${w.trend}`}>{w.trend === "up" ? "▲ 단기 위" : "▼ 단기 아래"}</span>
                    : <span className="muted small">봉 {w.bars}개 · 준비 중</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Sym({ symbol, sub }: { symbol: string; sub?: string }) {
  return (
    <div className="sym">
      <strong>{symbol}</strong>
      {sub && <span className="muted small">{sub}</span>}
    </div>
  );
}
