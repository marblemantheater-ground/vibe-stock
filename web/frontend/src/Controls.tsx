import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Snapshot } from "./types";
import { kstTime } from "./format";

type Command = "pause" | "resume" | "kill";
type Toast = { ok: boolean; message: string };

const OPEN = new Set(["PENDING", "SUBMITTED", "PARTIAL", "UNKNOWN"]);
const KILL_WORD = "긴급정지";

// 매매 제어: 일시정지 · 재개 · 긴급 정지. 되돌리기 어려운 조작은 화면 안 확인 창을 거친다.
export default function Controls({ snap }: { snap: Snapshot }) {
  const [modal, setModal] = useState<null | "kill" | "resume">(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const halt = snap.risk.halt;
  const trading = snap.status.phase === "trading";

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 7000);
    return () => clearTimeout(t);
  }, [toast]);

  const send = async (command: Command, body?: object) => {
    setBusy(true);
    try {
      const r = await fetch(`/api/control/${command}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Vibe-Control": "1" },
        body: JSON.stringify(body ?? {}),
      });
      const data = await r.json().catch(() => ({}));
      setToast({ ok: r.ok && data.ok !== false, message: data.message ?? data.error ?? "요청을 처리하지 못했습니다" });
      if (r.ok) setModal(null);
    } catch {
      setToast({ ok: false, message: "매매 프로그램에 연결할 수 없습니다" });
    } finally {
      setBusy(false);
    }
  };

  const state = !halt
    ? { tone: "good", text: "매매 중", sub: "신호가 나면 매수·매도합니다" }
    : halt.kind === "PAUSE"
      ? { tone: "warn", text: "일시정지", sub: "신규 매수만 멈춤 · 익절·손절 매도는 계속" }
      : halt.kind === "KILL"
        ? { tone: "bad", text: "긴급 정지", sub: "보유 종목 청산 · 신규 매수 중지" }
        : { tone: "bad", text: `${halt.label} 중지`, sub: "신규 매수 중지 · 매도는 계속" };

  // 손실 한도·긴급 정지 해제는 기준점이 바뀌거나 청산이 멈추므로 확인을 거친다
  const resumeNeedsConfirm = halt && halt.kind !== "PAUSE";

  return (
    <section className="controls">
      <div className="control-state">
        <span className={`state-pill tone-${state.tone}`}><i />{state.text}</span>
        <span className="muted small">
          {state.sub}
          {halt && ` · ${kstTime(halt.since)}부터`}
          {!trading && " · 장 외 시간: 누르면 다음 세션 시작 때 적용"}
        </span>
      </div>
      <div className="control-buttons">
        {!halt && (
          <button className="btn" disabled={busy} onClick={() => send("pause")}
                  title="신규 매수만 멈춥니다. 보유 종목의 익절·손절 매도는 계속됩니다">
            일시정지
          </button>
        )}
        {halt && (
          <button className="btn primary" disabled={busy}
                  onClick={() => (resumeNeedsConfirm ? setModal("resume") : send("resume"))}>
            재개
          </button>
        )}
        {halt?.kind !== "KILL" && (
          <button className="btn danger" disabled={busy} onClick={() => setModal("kill")}>긴급 정지</button>
        )}
      </div>
      {toast && (
        <div className={`toast ${toast.ok ? "ok" : "fail"}`} role="status" onClick={() => setToast(null)}>
          {toast.message}
        </div>
      )}
      {modal === "kill" && <KillDialog snap={snap} busy={busy} onCancel={() => setModal(null)}
                                       onConfirm={() => send("kill", { confirm: KILL_WORD })} />}
      {modal === "resume" && halt && (
        <Dialog title={`${halt.label} 해제`} onCancel={() => setModal(null)}>
          <p>{resumeText(halt.kind)}</p>
          <p className="muted small">중지 사유: {halt.reason}</p>
          <div className="dialog-actions">
            <button className="btn" onClick={() => setModal(null)}>취소</button>
            <button className="btn primary" disabled={busy} onClick={() => send("resume")}>재개</button>
          </div>
        </Dialog>
      )}
    </section>
  );
}

function resumeText(kind: string): string {
  if (kind === "DAILY_LOSS") return "신규 매수를 다시 시작합니다. 현재 총자산이 오늘의 새 기준점이 되어, 지금부터 한도만큼 더 잃으면 다시 멈춥니다.";
  if (kind === "MAX_DRAWDOWN") return "신규 매수를 다시 시작합니다. 현재 총자산이 새 고점이 되어, 지금부터 한도만큼 빠지면 다시 멈춥니다.";
  return "청산을 멈추고 신규 매수를 다시 시작합니다. 아직 팔리지 않은 종목은 다시 전략의 익절·손절 기준을 따릅니다.";
}

function KillDialog({ snap, busy, onCancel, onConfirm }: { snap: Snapshot; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const [word, setWord] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);
  const openOrders = snap.orders.filter((o) => OPEN.has(o.status)).length;
  const holdings = snap.positions.length;
  const real = snap.status.env === "real" && snap.status.dry_run === false;
  return (
    <Dialog title="긴급 정지" danger onCancel={onCancel}>
      <ul className="kill-list">
        <li>미체결 주문 <strong>{openOrders}건</strong>을 모두 취소합니다 (HTS에서 낸 주문 포함)</li>
        <li>보유 종목 <strong>{holdings}개</strong>를 모두 현재가보다 조금 낮은 지정가로 매도합니다</li>
        <li>신규 매수를 멈춥니다. 재개를 누를 때까지 유지됩니다</li>
      </ul>
      {real && <p className="warn-box">실전 계좌입니다. 실제로 주문이 나갑니다.</p>}
      {snap.status.phase !== "trading" && <p className="muted small">지금은 장 외 시간이라 다음 세션이 시작되면 청산합니다.</p>}
      <label htmlFor="kill-word" className="small">확인을 위해 <strong>{KILL_WORD}</strong> 를 입력하세요</label>
      <input id="kill-word" ref={input} value={word} onChange={(e) => setWord(e.target.value)} autoComplete="off"
             onKeyDown={(e) => e.key === "Enter" && word === KILL_WORD && !busy && onConfirm()} />
      <div className="dialog-actions">
        <button className="btn" onClick={onCancel}>취소</button>
        <button className="btn danger solid" disabled={busy || word !== KILL_WORD} onClick={onConfirm}>
          {busy ? "처리 중…" : "긴급 정지 실행"}
        </button>
      </div>
    </Dialog>
  );
}

function Dialog({ title, danger, onCancel, children }: { title: string; danger?: boolean; onCancel: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);
  return (
    <div className="backdrop" onClick={onCancel}>
      <div className={`dialog ${danger ? "danger" : ""}`} role="dialog" aria-modal="true" aria-label={title}
           onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        {children}
      </div>
    </div>
  );
}
