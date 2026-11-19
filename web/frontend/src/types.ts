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
  target: { pct: number; goal: number; gain: number; ratio: number; start_cash: number; done_today: boolean } | null;
  limits: { buys_today: number; max_buys: number; positions: number; max_positions: number; target_pct: number };
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
  tp_price: number | null;   // 익절·손절을 끈 옵션이면 null
  sl_price: number | null;
  to_tp_pct: number | null;
  to_sl_pct: number | null;
  live: boolean;
  preset: string;
  preset_recorded: boolean;
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
  off: string[];   // 꺼 둔 기능 이름
  preset: string | null;
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

// 설정 화면 (/api/settings). config/live.py 의 FIELDS 와 짝을 이룬다.
export type SettingValue = number | boolean | string | string[];

export type SettingField = {
  id: string;
  section: string;
  key: string;
  label: string;
  type: "number" | "int" | "bool" | "select" | "sessions" | "off" | "toggle";   // off: 꺼 둔 기능 목록(숨김), toggle: 스위치만
  apply: "now" | "next_day" | "file";
  unit: string;
  help: string;
  min: number | null;
  max: number | null;
  step: number | null;
  options: { value: string | number; label: string }[];
  // 켜고 끌 수 있는 칸: 같은 name 의 칸은 함께 켜지고 꺼진다. main 칸에 스위치. zero_key 값이 0 이어도 꺼진 것
  toggle: { name: string; label: string; main: boolean; zero_key: string | null; on_value: number | null } | null;
  value: SettingValue;
};

export type SettingsView = {
  editable: boolean;
  env?: "paper" | "real";
  trading?: boolean;
  preset?: string | null;
  file: string | null;
  groups: { title: string; fields: SettingField[] }[];
};

// 연간 정산 (/api/annual). report/annual.py 의 compute_annual 과 짝을 이룬다.
export type AnnualSell = {
  trade_date: string;
  settle_date: string;
  symbol: string;
  qty: number;
  price: number;
  sell_amount: number;
  sell_fee: number;
  cost_usd: number;
  gain_usd: number;
  rate: number | null;
  rate_source: "exact" | "near" | "current" | "none";
  proceeds_krw: number;
  cost_krw: number;
  gain_krw: number;
  note: string;
};

export type Annual = {
  years: number[];
  year: number;
  env: string;
  cost_method: "fifo" | "average";
  params: { commission_pct: number; sec_fee_per_million: number; finra_taf_per_share: number; basic_deduction_krw: number; tax_rate_pct: number };
  sell_count: number;
  wins: number;
  losses: number;
  buy_amount: number;
  sell_amount: number;
  fees_usd: number;
  gain_usd: number;
  gain_krw: number;
  deduction_krw: number;
  taxable_krw: number;
  tax_krw: number;
  after_tax_krw: number;
  deduction_left_krw: number;
  filing: string;
  monthly: { month: number; sells: number; gain_usd: number; gain_krw: number; fees_usd: number }[];
  sells: AnnualSell[];
  open_lots: { symbol: string; qty: number; cost_usd: number; cost_krw: number }[];
  warnings: string[];
};

// 백테스트 (/api/backtest). backtest/engine.py · runs.py 와 짝을 이룬다.
export type Coverage = {
  symbols: { symbol: string; first: string; last: string; days: number; bars: number }[];
  first: string | null;
  last: string | null;
};

export type BtStatus = { running: boolean; progress: number; started_at: string | null; error: string | null; last_id: string | null };

export type BtTrade = {
  symbol: string; qty: number; entry_time: string; exit_time: string; entry_price: number; exit_price: number;
  pnl: number; pnl_pct: number; fees: number; exit: string; reason: string; hold_min: number;
};

export type BtResult = {
  id: string;
  created_at: string;
  period: { start: string; end: string; days: number };
  symbols: string[];
  params: { start_cash: number; fill_slippage_pct: number };
  start_cash: number;
  end_equity: number;
  return_pct: number;
  benchmark_return_pct: number;
  max_drawdown_pct: number;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null;
  realized_pnl: number;
  unrealized_pnl: number;
  fees: number;
  avg_hold_min: number | null;
  skipped: Record<string, number>;
  by_exit: { exit: string; count: number; pnl: number }[];
  by_symbol: { symbol: string; trades: number; wins: number; pnl: number }[];
  equity_curve: { date: string; equity: number; benchmark: number; cash: number; positions: number }[];
  trade_list: BtTrade[];
  open_positions: { symbol: string; qty: number; avg_price: number; last_price: number; pnl: number }[];
  halts: { time: string; kind: string; equity: number }[];
  settings: Record<string, SettingValue>;
  overrides: Record<string, SettingValue>;
  elapsed_sec: number;
};

export type BtRunSummary = Pick<BtResult, "id" | "created_at" | "period" | "return_pct" | "benchmark_return_pct" |
  "max_drawdown_pct" | "trades" | "win_rate" | "profit_factor" | "fees" | "realized_pnl" | "settings" | "overrides"> & { symbol_count: number };
