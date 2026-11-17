import { useCallback, useEffect, useMemo, useState } from "react";
import type { SettingField, SettingValue, SettingsView } from "./types";
import { Dialog } from "./Controls";

type Toast = { ok: boolean; message: string; lines?: string[] };
// 숫자 칸은 입력 중인 글자 그대로 들고 있다가 (예: "-", "1.") 저장할 때 숫자로 바꾼다
type Draft = Record<string, SettingValue>;

const APPLY_LABEL = { now: "바로 적용", next_day: "다음 거래일부터", file: "파일에서만" } as const;

export function same(a: SettingValue, b: SettingValue): boolean {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => x === b[i]);
  return a === b;
}

export function parse(f: SettingField, raw: SettingValue): { value?: SettingValue; error?: string } {
  if (f.type === "sessions" && (raw as string[]).length === 0) return { error: "하나 이상 고르세요" };
  if (f.type !== "number" && f.type !== "int") return { value: raw };
  const s = String(raw).trim().replace(/,/g, "");
  if (s === "" || isNaN(Number(s))) return { error: "숫자를 입력하세요" };
  const n = Number(s);
  if (f.type === "int" && !Number.isInteger(n)) return { error: "정수만 됩니다" };
  if ((f.min !== null && n < f.min) || (f.max !== null && n > f.max)) return { error: `${f.min} ~ ${f.max}${f.unit} 범위` };
  return { value: n };
}

export function show(f: SettingField, v: SettingValue): string {
  if (f.type === "bool") return v ? "켬" : "끔";
  if (f.type === "sessions") return (v as string[]).map((s) => f.options.find((o) => o.value === s)?.label ?? s).join(" / ") || "없음";
  if (f.options.length) return f.options.find((o) => o.value === v)?.label ?? String(v);
  const n = Number(v).toLocaleString("en-US", { maximumFractionDigits: 4 });
  return f.unit === "USD" ? `$${n}` : `${n}${f.unit}`;
}

export default function Settings() {
  const [view, setView] = useState<SettingsView | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);

  const fields = useMemo(() => view?.groups.flatMap((g) => g.fields) ?? [], [view]);

  const adopt = useCallback((v: SettingsView) => {
    setView(v);
    setDraft(Object.fromEntries(v.groups.flatMap((g) => g.fields).map((f) => [f.id, f.value])));
  }, []);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/settings");
      if (!r.ok) throw new Error();
      adopt(await r.json());
      setLoadError(null);
    } catch {
      setLoadError("설정을 불러오지 못했습니다");
    }
  }, [adopt]);

  useEffect(() => void load(), [load]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), toast.ok ? 8000 : 12000);
    return () => clearTimeout(t);
  }, [toast]);

  // 바뀐 칸 · 잘못 입력한 칸
  const { changed, errors } = useMemo(() => {
    const changed: { f: SettingField; value: SettingValue }[] = [];
    const errors: Record<string, string> = {};
    for (const f of fields) {
      if (!(f.id in draft)) continue;
      const p = parse(f, draft[f.id]);
      if (p.error) errors[f.id] = p.error;
      else if (!same(p.value!, f.value)) changed.push({ f, value: p.value! });
    }
    return { changed, errors };
  }, [fields, draft]);
  const dirty = changed.length > 0 || Object.keys(errors).length > 0;

  // 다른 곳(파일 직접 수정)에서 바뀌었을 수 있으니, 고치는 중이 아니면 창으로 돌아올 때 다시 읽는다
  useEffect(() => {
    const onFocus = () => !dirty && load();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [dirty, load]);

  const save = async () => {
    setBusy(true);
    try {
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-Vibe-Control": "1" },
        body: JSON.stringify({ values: Object.fromEntries(changed.map((c) => [c.f.id, c.value])) }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.ok) {
        adopt(data);
        setConfirming(false);
        setToast({ ok: true, message: data.message, lines: data.changes });
      } else {
        setToast({ ok: false, message: data.error ?? data.message ?? "저장하지 못했습니다" });
        setConfirming(false);
      }
    } catch {
      setToast({ ok: false, message: "매매 프로그램에 연결할 수 없습니다" });
    } finally {
      setBusy(false);
    }
  };

  if (loadError) return <div className="empty">{loadError} <button className="btn small" onClick={load}>다시 시도</button></div>;
  if (!view) return <div className="empty">불러오는 중…</div>;
  if (!view.editable) return <div className="empty">이 실행에서는 설정 화면을 쓸 수 없습니다</div>;

  const real = view.env === "real";
  const later = changed.filter((c) => c.f.apply !== "now");

  return (
    <div className="settings">
      <section className={`settings-intro ${real ? "real" : ""}`}>
        <div>
          <strong>저장하면 {view.trading ? "지금 매매에 바로 적용됩니다" : "다음 세션부터 적용됩니다"}</strong>
          <span className="muted small">
            {" "}config/{view.file} 에 기록됩니다 (주석은 그대로). 파일을 직접 고쳐도 몇 초 안에 반영됩니다.
            봉 단위·이동평균 기간은 그날 쌓은 봉과 맞물려 있어 다음 거래일부터 적용됩니다.
          </span>
        </div>
        {real && <p className="warn-box">실전 계좌입니다. 바꾼 값으로 실제 주문이 나갑니다.</p>}
      </section>

      <div className="settings-grid">
        {view.groups.map((g) => (
          <section key={g.title} className={`panel settings-group ${g.title === "안전 스위치" ? "locked" : ""}`}>
            <div className="panel-head">
              <h2>{g.title}</h2>
              {g.title === "안전 스위치" && <span className="panel-note">안전을 위해 {view.file} 를 직접 고쳐야 바뀝니다</span>}
            </div>
            <div className="fields">
              {g.fields.map((f) => (
                <FieldRow key={f.id} f={f} value={draft[f.id] ?? f.value} error={errors[f.id]}
                          changed={changed.some((c) => c.f.id === f.id)}
                          onChange={(v) => setDraft((d) => ({ ...d, [f.id]: v }))} />
              ))}
            </div>
          </section>
        ))}
      </div>

      {dirty && (
        <div className="save-bar" role="region" aria-label="저장">
          <div className="save-bar-inner">
            <span>
              {Object.keys(errors).length > 0
                ? <span className="error">입력값을 확인하세요 ({Object.keys(errors).length}칸)</span>
                : <>바뀐 설정 <strong>{changed.length}개</strong>{later.length > 0 && <span className="muted"> · {later.length}개는 다음 거래일부터</span>}</>}
            </span>
            <span className="save-actions">
              <button className="btn" disabled={busy} onClick={() => adopt(view)}>되돌리기</button>
              <button className="btn primary" disabled={busy || changed.length === 0 || Object.keys(errors).length > 0}
                      onClick={() => setConfirming(true)}>저장</button>
            </span>
          </div>
        </div>
      )}

      {confirming && (
        <Dialog title="설정 저장" onCancel={() => setConfirming(false)}>
          <ul className="change-list">
            {changed.map(({ f, value }) => (
              <li key={f.id}>
                <span>{f.label}</span>
                <span className="num">
                  <span className="muted">{show(f, f.value)}</span> → <strong>{show(f, value)}</strong>
                </span>
                <span className={`pill tone-${f.apply === "now" ? "good" : "idle"}`}>
                  {view.trading || f.apply !== "now" ? APPLY_LABEL[f.apply] : "다음 세션부터"}
                </span>
              </li>
            ))}
          </ul>
          {riskNotes(changed).map((n) => <p key={n} className="muted small">{n}</p>)}
          {real && <p className="warn-box">실전 계좌입니다.</p>}
          <div className="dialog-actions">
            <button className="btn" onClick={() => setConfirming(false)}>취소</button>
            <button className="btn primary" disabled={busy} onClick={save}>{busy ? "저장 중…" : "저장하고 적용"}</button>
          </div>
        </Dialog>
      )}

      {toast && (
        <div className={`toast fixed ${toast.ok ? "ok" : "fail"}`} role="status" onClick={() => setToast(null)}>
          {toast.message}
          {toast.lines && toast.lines.length > 0 && (
            <ul>{toast.lines.map((l) => <li key={l}>{l}</li>)}</ul>
          )}
        </div>
      )}
    </div>
  );
}

// 저장 전에 알아 두면 좋은 영향
function riskNotes(changed: { f: SettingField; value: SettingValue }[]): string[] {
  const notes: string[] = [];
  const has = (k: string) => changed.find((c) => c.f.key === k);
  if (has("daily_loss_limit_pct") || has("max_drawdown_pct"))
    notes.push("손실 한도를 낮추면 지금 손실이 이미 새 한도를 넘었을 경우 확인 시간 뒤 바로 매매가 멈춥니다. 이미 멈춘 상태는 한도를 올려도 풀리지 않습니다 (재개 버튼).");
  if (has("capital_usage_pct") || has("budget_per_trade_usd") || has("max_positions"))
    notes.push("종목당 예산은 오늘 시작 때의 주문가능금액으로 다시 계산합니다. 이미 산 종목은 그대로 둡니다.");
  if (has("take_profit_pct") || has("stop_loss_pct"))
    notes.push("익절·손절 기준은 보유 중인 종목에도 다음 시세부터 적용됩니다.");
  return notes;
}

export function FieldRow({ f, value, error, changed, onChange }: {
  f: SettingField; value: SettingValue; error?: string; changed: boolean; onChange: (v: SettingValue) => void;
}) {
  const locked = f.apply === "file";
  const id = `set-${f.id}`;
  return (
    <div className={`field ${changed ? "changed" : ""} ${error ? "invalid" : ""}`}>
      <div className="field-text">
        <label htmlFor={id}>
          {f.label}
          {f.apply !== "now" && <span className={`apply-tag ${f.apply}`}>{APPLY_LABEL[f.apply]}</span>}
        </label>
        {f.help && <span className="field-help">{f.help}</span>}
        {changed && <span className="field-help">원래 값 {show(f, f.value)}</span>}
      </div>
      <div className="field-input">
        {f.type === "bool" ? (
          <label className="switch">
            <input id={id} type="checkbox" checked={Boolean(value)} disabled={locked}
                   onChange={(e) => onChange(e.target.checked)} />
            <span>{value ? "켬" : "끔"}</span>
          </label>
        ) : f.type === "select" ? (
          <select id={id} value={String(value)} disabled={locked}
                  onChange={(e) => {
                    const o = f.options.find((x) => String(x.value) === e.target.value);
                    if (o) onChange(o.value);
                  }}>
            {f.options.map((o) => <option key={String(o.value)} value={String(o.value)}>{o.label}</option>)}
          </select>
        ) : f.type === "sessions" ? (
          <div className="seg" role="group" aria-label={f.label} id={id}>
            {f.options.map((o) => {
              const list = value as string[];
              const on = list.includes(String(o.value));
              return (
                <button key={String(o.value)} type="button" className={on ? "on" : ""} aria-pressed={on}
                        onClick={() => onChange(on ? list.filter((s) => s !== o.value)
                                                   : f.options.map((x) => String(x.value)).filter((s) => s === o.value || list.includes(s)))}>
                  {o.label}
                </button>
              );
            })}
          </div>
        ) : (
          <span className="num-input">
            {f.unit === "USD" && <span className="unit">$</span>}
            <input id={id} inputMode="decimal" value={String(value)} disabled={locked} autoComplete="off"
                   aria-invalid={Boolean(error)} onChange={(e) => onChange(e.target.value)} />
            {f.unit && f.unit !== "USD" && <span className="unit">{f.unit}</span>}
          </span>
        )}
        {error && <span className="field-error">{error}</span>}
      </div>
    </div>
  );
}
