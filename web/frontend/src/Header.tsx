import { useEffect, useState } from "react";
import type { Snapshot } from "./types";
import type { Link, Tab } from "./App";
import { ago, kstTime, secondsSince } from "./format";

type Props = { snap: Snapshot | null; link: Link; tab: Tab; onLogout: () => void };

export default function Header({ snap, link, tab, onLogout }: Props) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const s = snap?.status;
  const tickAge = secondsSince(s?.last_tick_at ?? null, now);
  const sessionTone = !s ? "idle" : s.session === "REGULAR" ? "good" : s.session === "CLOSED" ? "idle" : "warn";
  const halt = snap?.risk.halt;

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand-row">
            <span className="brand">vibe-stock</span>
            {s && <span className={`chip env-${s.env}`}>{s.env === "real" ? "실전" : "모의투자"}</span>}
            {snap?.strategy.preset && <a href="#presets" className="chip chip-plain chip-link" title="지금 켜진 거래 옵션">{snap.strategy.preset}</a>}
            {s && s.dry_run !== null && (
              <span className={`chip ${s.dry_run ? "chip-warn" : "chip-plain"}`}>{s.dry_run ? "주문 미전송" : "주문 전송"}</span>
            )}
          </div>
          <nav className="tabs">
            <a href="#live" className={tab === "live" ? "on" : ""}>실시간</a>
            <a href="#reports" className={tab === "reports" ? "on" : ""}>정산</a>
            <a href="#backtest" className={tab === "backtest" ? "on" : ""}>백테스트</a>
            <a href="#presets" className={tab === "presets" ? "on" : ""}>거래 옵션</a>
            <a href="#scalp" className={tab === "scalp" ? "on" : ""}>스캘핑</a>
            <a href="#settings" className={tab === "settings" ? "on" : ""}>설정</a>
          </nav>
          <div className="status-row">
            {s && (
              <span className={`dot-label tone-${sessionTone}`}>
                <i />
                {s.session_label}
                {s.phase !== "trading" && s.next_session && (
                  <span className="muted"> · 다음 {s.next_session.label} {kstTime(s.next_session.start)}</span>
                )}
              </span>
            )}
            {s && s.phase === "trading" && (
              <span className={`dot-label tone-${s.ws_connected ? "good" : "bad"}`}>
                <i />시세 {s.ws_connected ? "연결" : "끊김"}
                <span className="muted"> · {ago(tickAge)}</span>
              </span>
            )}
            <span className={`dot-label tone-${link === "live" ? "good" : link === "lost" ? "bad" : "idle"}`}>
              <i />{link === "live" ? "화면 실시간" : link === "lost" ? "프로그램 응답 없음" : "연결 중"}
            </span>
            <button className="btn ghost small" onClick={onLogout}>로그아웃</button>
          </div>
        </div>
      </header>
      {halt && (
        <div className={`halt-banner ${halt.kind === "DAILY_TARGET" ? "good" : ""}`} role="alert">
          <strong>{halt.kind === "DAILY_TARGET" ? "오늘 매매 종료 · 하루 목표 달성" : `매매 중지 · ${halt.label}`}</strong>
          <span>{halt.reason}</span>
          <span className="muted">{kstTime(halt.since)} 부터 · 해제: 실시간 탭의 재개 버튼</span>
        </div>
      )}
    </>
  );
}
