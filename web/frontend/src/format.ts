// 숫자 표시. 국내 증권 화면 관례대로 상승은 빨강, 하락은 파랑.

export type Tone = "up" | "down" | "flat";

export function tone(v: number | null | undefined): Tone {
  if (v == null || Math.abs(v) < 1e-9) return "flat";
  return v > 0 ? "up" : "down";
}

export function usd(v: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (v == null || Number.isNaN(v)) return "–";
  const abs = Math.abs(v);
  const digits = abs > 0 && abs < 1 ? 4 : 2;
  const body = abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: digits });
  const sign = v < 0 ? "−" : opts.sign && v > 0 ? "+" : "";
  return `${sign}$${body}`;
}

export function pct(v: number | null | undefined, opts: { sign?: boolean } = { sign: true }): string {
  if (v == null || Number.isNaN(v)) return "–";
  const sign = v < 0 ? "−" : opts.sign && v > 0 ? "+" : "";
  return `${sign}${Math.abs(v).toFixed(2)}%`;
}

export function krw(v: number | null | undefined): string {
  if (v == null) return "–";
  if (Math.abs(v) >= 1e8) return `약 ${(v / 1e8).toFixed(2)}억원`;
  if (Math.abs(v) >= 1e4) return `약 ${Math.round(v / 1e4).toLocaleString("ko-KR")}만원`;
  return `${Math.round(v).toLocaleString("ko-KR")}원`;
}

export function num(v: number | null | undefined, digits = 0): string {
  if (v == null) return "–";
  return v.toLocaleString("en-US", { maximumFractionDigits: digits });
}

export function secondsSince(iso: string | null, now: number): number | null {
  if (!iso) return null;
  return Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
}

export function ago(sec: number | null): string {
  if (sec == null) return "없음";
  if (sec < 60) return `${sec}초 전`;
  if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
  return `${Math.floor(sec / 3600)}시간 전`;
}

export function kstTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleString("ko-KR", {
    timeZone: "Asia/Seoul", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

// 원 단위 그대로 (세금 계산처럼 정확한 금액이 필요할 때)
export function won(v: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (v == null || Number.isNaN(v)) return "–";
  const sign = v < 0 ? "−" : opts.sign && v > 0 ? "+" : "";
  return `${sign}${Math.round(Math.abs(v)).toLocaleString("ko-KR")}원`;
}
