// 매매 프로그램(/api/snapshot, /api/stream)이 보내는 상태. web/state.py 와 짝을 이룬다.

export type Status = {
  env: "paper" | "real";
  server_time: string;
  started_at: string;
  phase: "starting" | "waiting" | "trading";
  session: "CLOSED" | "PRE" | "REGULAR" | "AFTER";
  session_label: string;
  next_session: { label: string; start: string } | null;
  trading_date: string | null;
  ws_connected: boolean;
  last_tick_at: string | null;
  tick_count: number;
  dry_run: boolean | null;
};

export type Account = {
  orderable_usd: number;
  exchange_rate: number;
  equity: number;
  equity_krw: number | null;
  day_start_equity: number | null;
  day_pnl: number | null;
  day_pnl_pct: number | null;
  holdings_eval: number;
  fetched_at: string;
};

export type Risk = {
  daily_limit_pct: number;
  max_drawdown_pct: number;
  daily_action: string;
  drawdown_action: string;
  halt: { kind: string; label: string; reason: string; since: string } | null;
  daily_loss_pct: number | null;
  drawdown_pct: number | null;
  peak_equity: number | null;
};

export type Position = {
  symbol: string;
  name: string;
  exchange: string;
  qty: number;
  orderable_qty: number;
  avg_price: number;
  price: number;
  change_rate: number | null;
  eval: number;
  pnl: number;
  pnl_pct: number;
  tp_price: number;
  sl_price: number;
  to_tp_pct: number;
  to_sl_pct: number;
  live: boolean;
};

export type Order = {
  time: string;
  side: "BUY" | "SELL";
  symbol: string;
  qty: number;
  limit_price: number;
  status: string;
  status_label: string;
  filled_qty: number;
  avg_fill_price: number;
  reason: string;
  message: string;
  odno: string;
  external: boolean;
};

export type Watch = {
  symbol: string;
  exchange: string;
  source: "보유" | "미체결" | "관심" | "선정";
  price: number | null;
  change_rate: number | null;
  strength: number | null;
  volume: number | null;
  ma_short: number | null;
  ma_long: number | null;
  trend: "up" | "down" | null;
  bars: number;
  ready: boolean;
  buy_target: boolean;
};

export type LogEvent = { id: number; time: string; level: string; message: string };

export type StrategyInfo = {
  bar_minutes: number;
  short_window: number;
  long_window: number;
  min_change_rate: number;
  max_change_rate: number;
  take_profit_pct: number;
  stop_loss_pct: number;
  budget_per_trade_usd: number;
  max_positions: number;
  sessions: string[];
};

export type Snapshot = {
  status: Status;
  account: Account | null;
  risk: Risk;
  positions: Position[];
  orders: Order[];
  watch: Watch[];
  events: LogEvent[];
  strategy: StrategyInfo;
};

export type SummaryDay = {
  date: string;
  env: string;
  orders: number;
  filled_orders: number;
  buy_amount: number;
  sell_amount: number;
  realized_pnl: number;
  wins: number;
  losses: number;
  start_equity: number;
  end_equity: number;
  equity_change: number;
  return_pct: number;
  unrealized_pnl: number;
};

export type TradeRow = {
  placed_at: string;
  symbol: string;
  side: string;
  qty: string;
  limit_price: string;
  status: string;
  filled_qty: string;
  avg_fill_price: string;
  realized_pnl: string;
  reason: string;
  message: string;
};

export type DayReport = {
  date: string;
  orders: number;
  status_counts: Record<string, number>;
  realized_pnl: number;
  wins: number;
  losses: number;
  win_rate: number;
  start_equity: number;
  end_equity: number;
  return_pct: number;
  unrealized_pnl: number;
  buy_amount: number;
  sell_amount: number;
  holdings: { symbol: string; quantity: number; avg_price: number; current_price: number; pnl_amount: number; pnl_rate: number }[];
  trades: TradeRow[];
};
