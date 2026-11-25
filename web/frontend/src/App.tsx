import { useEffect, useRef, useState } from "react";
import type { Snapshot } from "./types";
import Login from "./Login";
import Live from "./Live";
import Reports from "./Reports";
import Header from "./Header";
import Settings from "./Settings";
import Backtest from "./Backtest";
import Presets from "./Presets";
import Scalp from "./Scalp";
import Sim from "./Sim";

type Auth = "checking" | "in" | "out" | "offline";
export type Tab = "live" | "reports" | "sim" | "backtest" | "presets" | "scalp" | "settings";

const tabFromHash = (): Tab => (location.hash.startsWith("#reports") ? "reports" : location.hash === "#settings" ? "settings" : location.hash === "#backtest" ? "backtest" : location.hash === "#presets" ? "presets" : location.hash === "#scalp" ? "scalp" : location.hash === "#sim" ? "sim" : "live");
export type Link = "connecting" | "live" | "lost";

const STALE_MS = 5000;

export default function App() {
  const [auth, setAuth] = useState<Auth>("checking");
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [link, setLink] = useState<Link>("connecting");
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const lastMsg = useRef(0);
  const [retry, setRetry] = useState(0);

  // 첫 접속: 로그인 여부 확인
  useEffect(() => {
    let cancelled = false;
    fetch("/api/snapshot")
      .then(async (r) => {
        if (cancelled) return;
        if (r.status === 401) return setAuth("out");
        if (!r.ok) return setAuth("offline");
        setSnap(await r.json());
        setAuth("in");
      })
      .catch(() => !cancelled && setAuth("offline"));
    return () => {
      cancelled = true;
    };
  }, [retry]);

  // 로그인 후: 1초마다 오는 실시간 상태 스트림
  useEffect(() => {
    if (auth !== "in") return;
    const es = new EventSource("/api/stream");
    es.onmessage = (e) => {
      lastMsg.current = Date.now();
      setSnap(JSON.parse(e.data));
      setLink("live");
    };
    es.onerror = async () => {
      setLink("lost");
      try {
        const r = await fetch("/api/snapshot");
        if (r.status === 401) {
          es.close();
          setAuth("out");
        }
      } catch {
        /* 매매 프로그램이 꺼짐 → EventSource 가 알아서 다시 연결을 시도한다 */
      }
    };
    const timer = setInterval(() => {
      if (lastMsg.current && Date.now() - lastMsg.current > STALE_MS) setLink("lost");
      if (es.readyState === EventSource.CLOSED) {
        clearInterval(timer);
        setRetry((n) => n + 1);
      }
    }, 1000);
    return () => {
      clearInterval(timer);
      es.close();
    };
  }, [auth, retry]);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (auth === "checking") return <div className="splash">연결 중…</div>;
  if (auth === "offline")
    return (
      <div className="splash">
        <p>매매 프로그램에 연결할 수 없습니다.</p>
        <p className="muted">run.bat (macOS: ./run.sh) 으로 프로그램을 실행한 뒤 다시 시도하세요.</p>
        <button className="btn" onClick={() => { setAuth("checking"); setRetry((n) => n + 1); }}>다시 연결</button>
      </div>
    );
  if (auth === "out") return <Login onLogin={() => { setAuth("checking"); setRetry((n) => n + 1); }} />;

  const logout = async () => {
    await fetch("/api/logout", { method: "POST" });
    setAuth("out");
  };

  return (
    <div className="app">
      <Header snap={snap} link={link} tab={tab} onLogout={logout} />
      <main>{tab === "live" ? snap && <Live snap={snap} /> : tab === "reports" ? <Reports /> : tab === "backtest" ? <Backtest /> : tab === "presets" ? <Presets /> : tab === "scalp" ? <Scalp /> : tab === "sim" ? <Sim /> : <Settings />}</main>
    </div>
  );
}
