import { useCallback, useEffect, useMemo, useState } from "react";
import type { BtRunSummary, Coverage, SettingField, SettingValue } from "./types";
import { SettingRow, isOn, offDiff, parse, same, show } from "./Settings";
import { Dialog } from "./Controls";
import { pct, tone, usd } from "./format";

type Live = { buys: number; sells: number; wins: number; realized_pnl: number; first: string; last: string } | null;
type Preset = {
  id: string; name: string; description: string; active: boolean; created_at: string; updated_at: string;
  values: Record<string, SettingValue>; summary: string; live: Live; backtest: BtRunSummary | null;
};
type View = {
  active: string; presets: Preset[]; fields: SettingField[]; trading: boolean; env: string;
  holdings: { symbol: string; qty: number; preset: string | null; rule: string | null }[];
};
type Editing = { mode: "new" | "edit"; id?: string; name: string; description: string; draft: Record<string, SettingValue> };

const H = { "Content-Type": "application/json", "X-Vibe-Control": "1" };
const KEY_ROWS = ["entry_mode", "bar_minutes", "short_window", "long_window", "min_change_rate", "max_change_rate", "take_profit_pct",
  "stop_loss_pct", "capital_usage_pct", "max_positions", "sessions", "daily_profit_target_pct", "max_buys_per_day", "rebuy_wait_sec", "off"];

// 거래 옵션: 전략 값 묶음을 여러 개 두고 하나만 켠다. 보유 종목은 산 때 옵션 기준으로 판다.
export default function Presets() {
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState("");
  const [toast, setToast] = useState<{ ok: boolean; message: string } | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [confirm, setConfirm] = useState<Preset | null>(null);
  const [removing, setRemoving] = useState<Preset | null>(null);
  const [busy, setBusy] = useState(false);
  const [btRunning, setBtRunning] = useState<string | null>(null);
  const [cov, setCov] = useState<Coverage | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/presets");
      if (!r.ok) throw new Error();
      setView(await r.json());
    } catch {
      setError("거래 옵션을 불러오지 못했습니다.");
    }
  }, []);

  useEffect(() => {
    void load();
    fetch("/api/backtest/data").then((r) => r.json()).then((d) => setCov(d.coverage)).catch(() => {});
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), toast.ok ? 7000 : 12000);
    return () => clearTimeout(t);
  }, [toast]);

  const call = async (url: string, method: string, body?: object): Promise<boolean> => {
    setBusy(true);
    try {
      const r = await fetch(url, { method, headers: H, body: body ? JSON.stringify(body) : undefined });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        setView(d);
        setToast({ ok: true, message: d.message });
        return true;
      }
      setToast({ ok: false, message: d.error ?? d.message ?? "처리하지 못했습니다" });
    } catch {
      setToast({ ok: false, message: "매매 프로그램에 연결할 수 없습니다" });
    } finally {
      setBusy(false);
    }
    return false;
  };

  const backtest = async (p: Preset) => {
    if (!cov?.first || !cov.last) {
      setToast({ ok: false, message: "저장된 분봉이 없습니다. run.bat --collect-minute 으로 먼저 모으세요" });
      return;
    }
    setBtRunning(p.id);
    try {
      const r = await fetch("/api/backtest", { method: "POST", headers: H,
        body: JSON.stringify({ start: cov.first, end: cov.last, preset: p.id }) });
      const d = await r.json().catch(() => ({}));
      if (r.ok) setToast({ ok: true, message: `'${p.name}' 백테스트: 수익률 ${pct(d.return_pct)} (단순 보유 ${pct(d.benchmark_return_pct)}), 거래 ${d.trades}건` });
      else setToast({ ok: false, message: d.error ?? "백테스트에 실패했습니다" });
    } finally {
      setBtRunning(null);
      void load();
    }
  };

  if (error) return <div className="empty">{error}</div>;
  if (!view) return <div className="empty">불러오는 중…</div>;

  const byKey = Object.fromEntries(view.fields.map((f) => [f.key, f]));
  const active = view.presets.find((p) => p.active)!;
  const unrecorded = view.holdings.filter((h) => !h.preset);

  return (
    <div className="presets">
      <section className="settings-intro">
        <div>
          <strong>지금 켜진 옵션: {active.name}</strong>
          <span className="muted small"> · 옵션은 하나만 켜집니다. 켜면 <b>새 매수부터</b> 적용되고,
            이미 보유한 종목은 <b>산 때 옵션의 익절·손절·데드크로스 기준</b>으로 팝니다. 손실 한도·주문 규칙·수수료는 옵션과 상관없이 공통입니다.</span>
        </div>
      </section>

      <div className="preset-grid">
        {view.presets.map((p) => (
          <article key={p.id} className={`panel preset-card ${p.active ? "on" : ""}`}>
            <header className="preset-head">
              <span className={`radio ${p.active ? "on" : ""}`} aria-hidden="true" />
              <div className="preset-title">
                <h3>{p.name}</h3>
                {p.description && <span className="muted small">{p.description}</span>}
              </div>
              {p.active ? <span className="pill tone-good">사용 중</span> : (
                <button className="btn small primary" disabled={busy} onClick={() => setConfirm(p)}>켜기</button>
              )}
            </header>
            <dl className="preset-values">
              {KEY_ROWS.filter((k) => byKey[k]).map((k) => (
                <div key={k} className={!p.active && !same(p.values[k], active.values[k]) ? "diff" : ""}>
                  <dt>{byKey[k].label}</dt>
                  <dd className="num">{isOn(byKey[k], (x) => p.values[x]) ? show(byKey[k], p.values[k]) : <span className="muted">끔</span>}</dd>
                </div>
              ))}
            </dl>
            <div className="preset-stats">
              <div>
                <span className="muted small">실제 운용</span>
                {p.live && p.live.sells + p.live.buys > 0 ? (
                  <span className="small">매도 {p.live.sells}건 · 승 {p.live.wins} ·{" "}
                    <b className={`num tone-${tone(p.live.realized_pnl)}`}>{usd(p.live.realized_pnl, { sign: true })}</b>
                    <span className="muted"> ({p.live.first.slice(5)}~{p.live.last.slice(5)})</span></span>
                ) : <span className="small muted">아직 없음</span>}
              </div>
              <div>
                <span className="muted small">최근 백테스트</span>
                {p.backtest ? (
                  <span className="small"><b className={`num tone-${tone(p.backtest.return_pct)}`}>{pct(p.backtest.return_pct)}</b>
                    <span className="muted"> (단순 보유 {pct(p.backtest.benchmark_return_pct)}) · 거래 {p.backtest.trades}건 ·
                      낙폭 {p.backtest.max_drawdown_pct.toFixed(1)}% · {p.backtest.period.start.slice(5)}~{p.backtest.period.end.slice(5)}</span></span>
                ) : <span className="small muted">아직 없음</span>}
              </div>
            </div>
            <footer className="preset-actions">
              <button className="btn small" disabled={btRunning !== null} onClick={() => backtest(p)}>
                {btRunning === p.id ? "백테스트 중…" : "백테스트"}
              </button>
              <button className="btn small" onClick={() => setEditing({ mode: "edit", id: p.id, name: p.name, description: p.description, draft: { ...p.values } })}>수정</button>
              <button className="btn small" onClick={() => setEditing({ mode: "new", name: `${p.name} 복사본`.slice(0, 30), description: p.description, draft: { ...p.values } })}>복제</button>
              {!p.active && <button className="btn small danger" onClick={() => setRemoving(p)}>삭제</button>}
            </footer>
          </article>
        ))}
        <button className="panel preset-new" onClick={() => setEditing({ mode: "new", name: `거래옵션${view.presets.length + 1}`, description: "", draft: { ...active.values } })}>
          <span className="plus">+</span>새 거래 옵션<span className="muted small">지금 켜진 옵션을 바탕으로 만듭니다</span>
        </button>
      </div>

      <section className="panel">
        <div className="panel-head"><h2>보유 종목의 매도 기준</h2>
          <span className="panel-note">산 때 켜져 있던 옵션 기준으로 팝니다</span></div>
        {view.holdings.length === 0 ? <div className="empty">보유 종목이 없습니다.</div> : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>종목</th><th className="r">수량</th><th>산 옵션</th><th>매도 기준</th></tr></thead>
              <tbody>
                {view.holdings.map((h) => (
                  <tr key={h.symbol}>
                    <td>{h.symbol}</td><td className="r num">{h.qty}</td>
                    <td>{h.preset ?? <span className="muted">기록 없음 → 지금 켜진 '{active.name}'</span>}</td>
                    <td className="muted">{h.rule ?? active.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {unrecorded.length > 0 && <p className="muted small bt-skip">기록 없음: HTS에서 직접 샀거나 거래 옵션 기능 이전에 산 종목입니다.</p>}
      </section>

      {editing && (
        <EditDialog editing={editing} fields={view.fields} isActive={editing.id === view.active} busy={busy}
                    onCancel={() => setEditing(null)}
                    onSave={async (e, values) => {
                      const ok = e.mode === "new"
                        ? await call("/api/presets", "POST", { name: e.name, description: e.description, values })
                        : await call(`/api/presets/${e.id}`, "PUT", { name: e.name, description: e.description, values });
                      if (ok) setEditing(null);
                    }} />
      )}

      {confirm && (
        <Dialog title={`'${confirm.name}' 켜기`} onCancel={() => setConfirm(null)}>
          <ActivateDiff from={active} to={confirm} fields={view.fields} trading={view.trading} />
          <p className="small">보유 중인 {view.holdings.length}종목은 그대로 산 때 옵션 기준으로 매도합니다.</p>
          {view.env === "real" && <p className="warn-box">실전 계좌입니다. 바꾼 값으로 실제 주문이 나갑니다.</p>}
          {!confirm.backtest && <p className="muted small">이 옵션은 아직 백테스트 기록이 없습니다.</p>}
          <div className="dialog-actions">
            <button className="btn" onClick={() => setConfirm(null)}>취소</button>
            <button className="btn primary" disabled={busy}
                    onClick={async () => { if (await call(`/api/presets/${confirm.id}/activate`, "POST")) setConfirm(null); }}>켜기</button>
          </div>
        </Dialog>
      )}

      {removing && (
        <Dialog title={`'${removing.name}' 삭제`} danger onCancel={() => setRemoving(null)}>
          <p>이 옵션을 지웁니다. 이 옵션으로 산 종목은 지워도 산 때 기준(기록된 값)으로 계속 매도합니다.</p>
          <div className="dialog-actions">
            <button className="btn" onClick={() => setRemoving(null)}>취소</button>
            <button className="btn danger solid" disabled={busy}
                    onClick={async () => { if (await call(`/api/presets/${removing.id}`, "DELETE")) setRemoving(null); }}>삭제</button>
          </div>
        </Dialog>
      )}

      {toast && <div className={`toast fixed ${toast.ok ? "ok" : "fail"}`} role="status" onClick={() => setToast(null)}>{toast.message}</div>}
    </div>
  );
}

function ActivateDiff({ from, to, fields, trading }: { from: Preset; to: Preset; fields: SettingField[]; trading: boolean }) {
  const diffs = fields.filter((f) => f.type !== "toggle" && !same(from.values[f.key], to.values[f.key]));
  if (!diffs.length) return <p className="small">지금 옵션과 전략 값이 같습니다. 켜진 옵션 이름만 바뀝니다.</p>;
  return (
    <ul className="change-list">
      {diffs.map((f) => f.type === "off" ? offDiff(f, from.values.off, to.values.off).map((d) => (
        <li key={`off.${d.name}`}>
          <span>{d.label}</span>
          <span className="num"><span className="muted">{d.on ? "끔" : "켬"}</span> → <strong>{d.on ? "켬" : "끔"}</strong></span>
          <span className={`pill tone-${trading ? "good" : "idle"}`}>{trading ? "바로 적용" : "다음 세션부터"}</span>
        </li>
      )) : (
        <li key={f.key}>
          <span>{f.label}</span>
          <span className="num"><span className="muted">{show(f, from.values[f.key])}</span> → <strong>{show(f, to.values[f.key])}</strong></span>
          <span className={`pill tone-${f.apply === "now" ? "good" : "idle"}`}>
            {f.apply !== "now" ? "다음 거래일부터" : trading ? "바로 적용" : "다음 세션부터"}
          </span>
        </li>
      ))}
    </ul>
  );
}

function EditDialog({ editing, fields, isActive, busy, onCancel, onSave }: {
  editing: Editing; fields: SettingField[]; isActive: boolean; busy: boolean;
  onCancel: () => void; onSave: (e: Editing, values: Record<string, SettingValue>) => void;
}) {
  const [e, setE] = useState<Editing>(editing);
  const { values, errors } = useMemo(() => {
    const values: Record<string, SettingValue> = {};
    const errors: Record<string, string> = {};
    for (const f of fields) {
      if (f.type === "toggle") continue;   // 켬/끔은 off 목록으로
      const p = parse(f, e.draft[f.key] ?? f.value);
      if (p.error) errors[f.key] = p.error;
      else values[f.key] = p.value!;
    }
    return { values, errors };
  }, [fields, e.draft]);
  const nErr = Object.keys(errors).length + (e.name.trim() ? 0 : 1);
  return (
    <Dialog title={e.mode === "new" ? "새 거래 옵션" : `'${editing.name}' 수정`} onCancel={onCancel}>
      <div className="preset-form">
        <label>이름<input value={e.name} maxLength={30} onChange={(x) => setE({ ...e, name: x.target.value })} /></label>
        <label>설명 (선택)
          <input value={e.description} maxLength={100} placeholder="예: 5분봉, 익절 2%"
                 onChange={(x) => setE({ ...e, description: x.target.value })} /></label>
      </div>
      <div className="fields preset-fields">
        {fields.map((f) => (
          <SettingRow key={f.key} f={{ ...f, apply: "now" }} idPrefix="preset" error={errors[f.key]}
                      get={(k) => e.draft[k]} orig={(k) => editing.draft[k]}
                      changed={editing.mode === "edit" && f.type !== "toggle" && !same(values[f.key] ?? e.draft[f.key], editing.draft[f.key])}
                      onSet={(vals) => setE((cur) => ({ ...cur, draft: { ...cur.draft, ...vals } }))} />
        ))}
      </div>
      {isActive && <p className="muted small">켜져 있는 옵션입니다. 저장하면 바뀐 값이 실행 중인 매매에 바로 적용됩니다 (봉 단위·이평 기간은 다음 거래일부터).</p>}
      <div className="dialog-actions">
        <button className="btn" onClick={onCancel}>취소</button>
        <button className="btn primary" disabled={busy || nErr > 0} onClick={() => onSave(e, values)}>
          {e.mode === "new" ? "만들기" : "저장"}
        </button>
      </div>
    </Dialog>
  );
}
