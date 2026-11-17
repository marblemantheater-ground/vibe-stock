import { useState, type FormEvent } from "react";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (r.ok) return onLogin();
      const body = await r.json().catch(() => ({}));
      setError(body.error ?? "로그인하지 못했습니다");
    } catch {
      setError("매매 프로그램에 연결할 수 없습니다");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form onSubmit={submit} className="login-card">
        <div className="brand">vibe-stock</div>
        <p className="muted">미국주식 자동매매 대시보드</p>
        <label htmlFor="pw">비밀번호</label>
        <input id="pw" type="password" autoFocus autoComplete="current-password"
               value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <p className="error">{error}</p>}
        <button className="btn primary" disabled={busy || !password}>{busy ? "확인 중…" : "로그인"}</button>
        <p className="hint">.env 의 DASHBOARD_PASSWORD. 비워 두었다면 매매 프로그램 콘솔 창에 표시된 임시 비밀번호를 입력하세요.</p>
      </form>
    </div>
  );
}
