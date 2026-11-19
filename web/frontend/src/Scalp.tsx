import { useCallback, useEffect, useState } from "react";
import type { BtRunSummary, Coverage, SettingValue } from "./types";
import { Dialog } from "./Controls";
import { pct, tone, usd } from "./format";

type Trip = {
  symbol: string; qty: number; entry_time: string | null; exit_time: string | null; hold_sec: number | null;
  entry_price: number; exit_price: number; gross: number; fees: number; net: number; net_pct: number | null;
  exit: string; reason: string; preset: string; mode: string; entry_reason: string;
};
type Summary = { trades: number; wins: number; win_rate: number | null; gross: number; fees: number; net: number;
  avg_hold_sec: number | null; fee_share_pct: number | null; best: number | null; worst: number | null };
type PosRow = { symbol: string; qty: number; avg_price: number; price: number; pnl_pct: number | null; preset: string;
  mode: string; mode_label: string; tp_price: number; sl_price: number; peak: number | null; trailing_on: boolean;
  trailing_stop: number | null; trailing_activate_price: number | null; held_sec: number | null;
  time_left_sec: number | null; flatten_at: string | null };
type WatchRow = { symbol: string; price: number | null; change_rate: number | null; vwap: number | null; vs_vwap_pct: number | null;
  breakout_level: number | null; vs_breakout_pct: number | null; orb_high: number | null; vs_orb_pct: number | null;
  volume_ratio: number | null; strength: number | null; spread_pct: number | null; bars: number; buy_target: boolean };
type PresetItem = { id: string; name: string; description: string; active: boolean; summary: string;
  values: Record<string, SettingValue>; backtest: BtRunSummary | null };
type View = {
  trading: boolean; env: string; preset: string | null; active: Record<string, SettingValue>; entry_label: string; is_scalp: boolean;
  fee: { round_trip_pct: number; commission_pct: number }; warnings: string[];
  templates: { key: string; name: string; description: string; values: Record<string, SettingValue> }[];
  presets: PresetItem[]; trips: Trip[]; summary: Summary; summary_scalp: Summary; positions: PosRow[]; watch: WatchRow[];
  entry_modes: Record<string, string>;
};

const H = { "Content-Type": "application/json", "X-Vibe-Control": "1" };

function dur(sec: number | null): string {
  if (sec == null) return "–";
  if (sec < 60) return `${sec}초`;
  if (sec < 3600) return `${Math.floor(sec / 60)}분 ${sec % 60}초`;
  return `${Math.floor(sec / 3600)}시간 ${Math.floor((sec % 3600) / 60)}분`;
}

function hm(iso: string | null): string {
  if (!iso) return "–";
  return new Date(iso).toLocaleTimeString("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function dist(v: number | null) {
  return v == null ? <span className="muted">–</span> : <span className={`num tone-${tone(v)}`}>{pct(v)}</span>;
}

function scalpChips(v: Record<string, SettingValue>, modes: Record<string, string>): string[] {
  const out = [modes[String(v.entry_mode)] ?? String(v.entry_mode), `${v.bar_minutes}분봉`];
  if (v.entry_mode === "breakout") out.push(`${v.breakout_bars}봉 고가`);
  if (v.entry_mode === "orb") out.push(`첫 ${v.orb_minutes}분`);
  if (Number(v.volume_mult) > 0) out.push(`거래량 ${v.volume_mult}배↑`);
  out.push(`익절 ${v.take_profit_pct}%`, `손절 ${v.stop_loss_pct}%`);
  if (Number(v.trailing_activate_pct) > 0) out.push(`추적 +${v.trailing_activate_pct}%/−${v.trailing_pct}%`);
  if (Number(v.max_hold_min) > 0) out.push(`최대 ${v.max_hold_min}분`);
  if (Number(v.flatten_before_close_min) > 0) out.push(`마감 ${v.flatten_before_close_min}분 전 정리`);
  if (Number(v.min_strength) > 0) out.push(`체결강도 ${v.min_strength}↑`);
  if (Number(v.max_spread_pct) > 0) out.push(`스프레드 ${v.max_spread_pct}%↓`);
  return out;
}

// 스캘핑: 템플릿으로 옵션 만들기 → 백테스트 → 켜기, 그리고 오늘의 짧은 매매를 따로 본다
export default function Scalp() {
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState("");
  const [cov, setCov] = useState<Coverage | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<{ ok: boolean; message: string } | null>(null);
  const [onlyScalp, setOnlyScalp] = useState(true);
  const [activating, setActivating] = useState<PresetItem | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/scalp");
      if (!r.ok) throw new Error();
      setView(await r.json());
      setError("");
    } catch {
      setError("스캘핑 정보를 불러오지 못했습니다.");
    }
  }, []);

  useEffect(() => {
    void load();
    fetch("/api/backtest/data").then((r) => r.json()).then((d) => setCov(d.coverage)).catch(() => {});
    const t = setInterval(load, 2000);   // 보유 종목 청산 상태·지표 실시간 갱신
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), toast.ok ? 8000 : 12000);
    return () => clearTimeout(t);
  }, [toast]);

  const post = async (key: string, url: string, body?: object) => {
    setBusy(key);
    try {
      const r = await fetch(url, { method: "POST", headers: H, body: JSON.stringify(body ?? {}) });
      const d = await r.json().catch(() => ({}));
      if (r.ok) setToast({ ok: true, message: d.message ?? `수익률 ${pct(d.return_pct)} (단순 보유 ${pct(d.benchmark_return_pct)}), 거래 ${d.trades}건, 승률 ${d.win_rate ?? "–"}%` });
      else setToast({ ok: false, message: d.error ?? "처리하지 못했습니다" });
      return r.ok;
    } catch {
      setToast({ ok: false, message: "매매 프로그램에 연결할 수 없습니다" });
      return false;
    } finally {
      setBusy(null);
      void load();
    }
  };

  const backtest = (p: PresetItem) => {
    if (!cov?.first || !cov.last) {
      setToast({ ok: false, message: "저장된 분봉이 없습니다. run.bat --collect-minute 으로 먼저 모으세요" });
      return;
    }
    void post(`bt-${p.id}`, "/api/backtest", { start: cov.first, end: cov.last, preset: p.id });
  };

  if (error && !view) return <div className="empty">{error}</div>;
  if (!view) return <div className="empty">불러오는 중…</div>;

  const s = onlyScalp ? view.summary_scalp : view.summary;
  const trips = onlyScalp ? view.trips.filter((t) => t.mode && t.mode !== "ma_cross") : view.trips;

  return (
    <div className="scalp">
      <section className={`settings-intro ${view.is_scalp ? "" : "muted-intro"}`}>
        <div>
          <strong>지금 켜진 옵션: {view.preset ?? "–"} · {view.entry_label}</strong>
          {!view.is_scalp && <span className="muted small"> · 지금은 스캘핑 진입 방식이 아닙니다. 아래 템플릿으로 스캘핑 옵션을 만들어 백테스트한 뒤 켜 보세요.</span>}
        </div>
        <div className="chips">{scalpChips(view.active, view.entry_modes).map((c) => <span key={c} className="chip chip-plain">{c}</span>)}</div>
        <div className="small">
          왕복 수수료 <b className="num">{view.fee.round_trip_pct.toFixed(2)}%</b> (매수·매도 각 {view.fee.commission_pct}% + SEC)
          <span className="muted"> → 한 번 사고팔 때 최소 이만큼은 올라야 본전입니다.</span>
        </div>
        {view.warnings.length > 0 && <ul className="notice-list">{view.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
      </section>

      <section className="panel">
        <div className="panel-head"><h2>스캘핑 옵션 만들기 · 테스트</h2>
          <span className="panel-note">템플릿은 흔히 쓰는 규칙을 출발점으로 둔 값입니다. 백테스트로 확인하고 거래 옵션 탭에서 다듬으세요</span></div>
        <div className="tpl-grid">
          {view.templates.map((t) => (
            <div key={t.key} className="tpl">
              <strong>{t.name}</strong>
              <span className="muted small">{t.description}</span>
              <div className="chips">{scalpChips(t.values, view.entry_modes).slice(1).map((c) => <span key={c} className="chip chip-plain">{c}</span>)}</div>
              <button className="btn small" disabled={busy !== null} onClick={() => post(`tpl-${t.key}`, `/api/scalp/templates/${t.key}`)}>
                {busy === `tpl-${t.key}` ? "만드는 중…" : "거래 옵션으로 만들기"}
              </button>
            </div>
          ))}
        </div>
        {view.presets.length > 0 && (
          <div className="table-scroll">
            <table>
              <thead><tr><th>스캘핑 옵션</th><th>규칙</th><th className="r">최근 백테스트</th><th className="r">거래</th><th className="r">승률</th><th className="r">낙폭</th><th></th></tr></thead>
              <tbody>
                {view.presets.map((p) => (
                  <tr key={p.id}>
                    <td><b>{p.name}</b>{p.active && <span className="pill tone-good" style={{ marginLeft: 6 }}>사용 중</span>}</td>
                    <td className="reason" title={scalpChips(p.values, view.entry_modes).join(" · ")}>{scalpChips(p.values, view.entry_modes).join(" · ")}</td>
                    {p.backtest ? (
                      <>
                        <td className={`r num tone-${tone(p.backtest.return_pct)}`}>{pct(p.backtest.return_pct)} <span className="muted small">(보유 {pct(p.backtest.benchmark_return_pct)})</span></td>
                        <td className="r num">{p.backtest.trades}</td>
                        <td className="r num">{p.backtest.win_rate == null ? "–" : `${p.backtest.win_rate.toFixed(0)}%`}</td>
                        <td className="r num">{p.backtest.max_drawdown_pct.toFixed(1)}%</td>
                      </>
                    ) : <td className="r muted" colSpan={4}>아직 없음</td>}
                    <td className="actions">
                      <button className="btn small" disabled={busy !== null} onClick={() => backtest(p)}>{busy === `bt-${p.id}` ? "계산 중…" : "백테스트"}</button>
                      {!p.active && <button className="btn small primary" disabled={busy !== null} onClick={() => setActivating(p)}>켜기</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="scalp-head">
        <h2>오늘의 매매</h2>
        <div className="seg" role="group" aria-label="보기">
          <button className={onlyScalp ? "on" : ""} aria-pressed={onlyScalp} onClick={() => setOnlyScalp(true)}>스캘핑만</button>
          <button className={!onlyScalp ? "on" : ""} aria-pressed={!onlyScalp} onClick={() => setOnlyScalp(false)}>전체</button>
        </div>
      </div>
      <section className="kpis">
        <div className="kpi"><div className="kpi-label">왕복 매매</div>
          <div className="kpi-value num">{s.trades}건</div>
          <div className="kpi-sub">승 {s.wins} · 승률 {s.win_rate == null ? "–" : `${s.win_rate}%`}</div></div>
        <div className="kpi"><div className="kpi-label">수수료 차감 손익</div>
          <div className={`kpi-value num tone-${tone(s.net)}`}>{usd(s.net, { sign: true })}</div>
          <div className="kpi-sub">최고 {usd(s.best, { sign: true })} · 최저 {usd(s.worst, { sign: true })}</div></div>
        <div className="kpi"><div className="kpi-label">수수료</div>
          <div className="kpi-value num">{usd(s.fees)}</div>
          <div className="kpi-sub">수수료 전 손익 {usd(s.gross, { sign: true })}{s.fee_share_pct != null ? ` · 손익의 ${s.fee_share_pct}%` : ""}</div></div>
        <div className="kpi"><div className="kpi-label">평균 보유 시간</div>
          <div className="kpi-value num">{dur(s.avg_hold_sec)}</div>
          <div className="kpi-sub">{view.trading ? "장중 · 2초마다 갱신" : "장 외 시간"}</div></div>
      </section>

      <section className="panel">
        <div className="panel-head"><h2>보유 종목 청산 상태</h2><span className="count">{view.positions.length}</span>
          <span className="panel-note">산 옵션 기준 · 추적 손절가·남은 보유 시간·마감 정리 시각</span></div>
        {view.positions.length === 0 ? <div className="empty">보유 종목이 없습니다.</div> : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>종목</th><th>산 옵션</th><th className="r">수익률</th><th className="r">손절가</th><th className="r">익절가</th>
                <th className="r">추적 손절</th><th className="r">보유</th><th className="r">시간 청산까지</th><th className="r">마감 정리</th></tr></thead>
              <tbody>
                {view.positions.map((p) => (
                  <tr key={p.symbol}>
                    <td><b>{p.symbol}</b> <span className="muted small">{p.qty}주 @ {usd(p.avg_price)}</span></td>
                    <td>{p.preset}<div className="muted small">{p.mode_label}</div></td>
                    <td className={`r num tone-${tone(p.pnl_pct)}`}>{pct(p.pnl_pct)}</td>
                    <td className="r num">{usd(p.sl_price)}</td>
                    <td className="r num">{usd(p.tp_price)}</td>
                    <td className="r num">{p.trailing_on ? <b>{usd(p.trailing_stop)}</b> : p.trailing_activate_price ? <span className="muted">{usd(p.trailing_activate_price)}↑ 시 켜짐</span> : <span className="muted">끔</span>}
                      {p.peak ? <div className="muted small">고점 {usd(p.peak)}</div> : null}</td>
                    <td className="r num">{dur(p.held_sec)}</td>
                    <td className="r num">{p.time_left_sec == null ? <span className="muted">끔</span> : dur(Math.max(p.time_left_sec, 0))}</td>
                    <td className="r num">{p.flatten_at ? hm(p.flatten_at) : <span className="muted">끔</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-head"><h2>왕복 매매 내역</h2><span className="count">{trips.length}</span>
          <span className="panel-note">매수 체결 → 매도 체결 · 수수료는 설정의 수수료율로 추정</span></div>
        {trips.length === 0 ? <div className="empty">오늘 끝난 매매가 없습니다.</div> : (
          <div className="table-scroll tall">
            <table>
              <thead><tr><th>매수</th><th>매도</th><th>종목</th><th className="r">수량</th><th className="r">매수가</th><th className="r">매도가</th>
                <th className="r">보유</th><th className="r">수수료</th><th className="r">손익(차감)</th><th>청산</th><th>옵션</th></tr></thead>
              <tbody>
                {trips.map((t, i) => (
                  <tr key={i}>
                    <td className="num muted">{hm(t.entry_time)}</td><td className="num muted">{hm(t.exit_time)}</td>
                    <td>{t.symbol}</td><td className="r num">{t.qty}</td>
                    <td className="r num">{usd(t.entry_price)}</td><td className="r num">{usd(t.exit_price)}</td>
                    <td className="r num">{dur(t.hold_sec)}</td><td className="r num muted">{usd(t.fees)}</td>
                    <td className={`r num tone-${tone(t.net)}`}>{usd(t.net, { sign: true })} <span className="small">({pct(t.net_pct)})</span></td>
                    <td className="reason" title={`${t.entry_reason} → ${t.reason}`}>{t.exit}</td>
                    <td className="muted small">{t.preset}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-head"><h2>감시 종목 스캘핑 지표</h2><span className="count">{view.watch.length}</span>
          <span className="panel-note">현재가가 기준가보다 몇 % 위(+)·아래(−)인지 · 체결강도·스프레드는 실시간 값</span></div>
        {view.watch.length === 0 ? <div className="empty">{view.trading ? "지표를 계산 중입니다." : "장이 열리면 실시간으로 표시됩니다."}</div> : (
          <div className="table-scroll tall">
            <table>
              <thead><tr><th>종목</th><th className="r">현재가</th><th className="r">VWAP 대비</th><th className="r">돌파 기준가 대비</th>
                <th className="r">시가 범위 고가 대비</th><th className="r">거래량 배수</th><th className="r">체결강도</th><th className="r">스프레드</th></tr></thead>
              <tbody>
                {view.watch.map((w) => (
                  <tr key={w.symbol}>
                    <td>{w.symbol}{!w.buy_target && <span className="tag">매도만</span>}</td>
                    <td className="r num">{w.price == null ? "–" : usd(w.price)}</td>
                    <td className="r" title={w.vwap ? `VWAP ${usd(w.vwap)}` : ""}>{dist(w.vs_vwap_pct)}</td>
                    <td className="r" title={w.breakout_level ? `${usd(w.breakout_level)}` : ""}>{dist(w.vs_breakout_pct)}</td>
                    <td className="r" title={w.orb_high ? `${usd(w.orb_high)}` : ""}>{dist(w.vs_orb_pct)}</td>
                    <td className="r num">{w.volume_ratio == null ? "–" : `${w.volume_ratio.toFixed(1)}배`}</td>
                    <td className={`r num ${w.strength != null && w.strength >= 100 ? "tone-up" : w.strength != null ? "tone-down" : ""}`}>{w.strength == null ? "–" : w.strength.toFixed(1)}</td>
                    <td className="r num">{w.spread_pct == null ? "–" : `${w.spread_pct.toFixed(3)}%`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {activating && (
        <Dialog title={`'${activating.name}' 켜기`} onCancel={() => setActivating(null)}>
          <p className="small">{scalpChips(activating.values, view.entry_modes).join(" · ")}</p>
          <p className="small">새 매수부터 이 옵션으로 매매합니다. 지금 보유 중인 종목은 산 때 옵션 기준으로 팝니다.</p>
          {!activating.backtest && <p className="warn-box">아직 백테스트 기록이 없습니다. 먼저 백테스트를 권합니다.</p>}
          {view.env === "real" && <p className="warn-box">실전 계좌입니다. 실제 주문이 나갑니다.</p>}
          <div className="dialog-actions">
            <button className="btn" onClick={() => setActivating(null)}>취소</button>
            <button className="btn primary" disabled={busy !== null}
                    onClick={async () => { if (await post("act", `/api/presets/${activating.id}/activate`)) setActivating(null); }}>켜기</button>
          </div>
        </Dialog>
      )}
      {toast && <div className={`toast fixed ${toast.ok ? "ok" : "fail"}`} role="status" onClick={() => setToast(null)}>{toast.message}</div>}
    </div>
  );
}
