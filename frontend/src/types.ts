// Decimals arrive as strings. The UI never computes money; it displays what the ledger reports.

export type Dec = string;

export interface FeedSymbol {
  ok: boolean;
  error: string | null;
  checked_at?: string;
  updated_at?: string;
  latency_ms?: number;
  stale?: boolean;
  bid?: Dec | null;
  ask?: Dec | null;
  spread_pct?: Dec | null;
  bid_depth_quote?: Dec;
  ask_depth_quote?: Dec;
  market_status?: string;
  allowed?: boolean;
  reasons?: string[];
}

export interface Lot {
  lot_id: string;
  symbol: string;
  entry_price: Dec;
  gross_base_qty: Dec;
  sellable_base_qty: Dec;
  remaining_base_qty: Dec;
  reserved_base_qty: Dec;
  available_base_qty: Dec;
  quote_cost_remaining: Dec;
  entry_fee_amount: Dec;
  entry_fee_currency: string;
  realized_pnl: Dec;
  status: string;
  mark_bid: Dec | null;
}

export interface Working {
  client_order_id: string;
  symbol: string;
  side: string;
  purpose: string;
  state: string;
  limit_price: Dec;
  quote_budget: Dec;
  base_budget: Dec;
  filled_quote: Dec;
  filled_base: Dec;
  lot_id: string | null;
}

export interface Fill {
  fill_id: string;
  side: string;
  lot_id: string;
  symbol: string;
  price: Dec;
  quantity: Dec;
  fee_amount: Dec;
  fee_currency: string;
  quote_amount: Dec;
}

export interface Arm {
  name: string;
  strategy_id: string;
  starting: Dec;
  peak_equity: Dec;
  exposure_pct: Dec | null;
  drawdown_pct: Dec | null;
  trading_blocked: boolean;
  block_reason: string | null;
  completed_cycles: number;
  open_lots: number;
  active_symbols: string[];
  entries_allowed: number;
  entries_rejected: number;
  paused_entry_pct: Dec;
  reject_reasons: Record<string, number>;
  strategy_skips: Record<string, string[]>;
  working: Working[];
  lots: Lot[];
  fills: Fill[];
  equity?: Dec;
  cash?: Dec;
  free_quote: Dec;
  reserved_quote: Dec;
  gross_position_value?: Dec;
  estimated_exit_fees?: Dec;
  net_position_value?: Dec;
  realized_pnl?: Dec;
  unrealized_pnl?: Dec;
  fees_by_currency?: Record<string, Dec>;
  error?: string;
}

export interface NewsEvent {
  canonical_event_id: string;
  headline: string;
  summary: string;
  symbols: string[];
  sources: string[];
  links: string[];
  published_at: string | null;
  first_seen_at: string;
  fetched_at: string;
  seen_count: number;
  regime: string;
  weight: Dec;
  illustrative: boolean;
  price_reaction: Record<string, Dec | null>;
}

export interface Decision {
  at: string;
  arm: string;
  client_order_id: string | null;
  side: string;
  symbol: string;
  allowed: boolean;
  reason: string;
  regime: string | null;
  state: string | null;
  repeats?: number;
  last_at?: string;
}

export interface Veto {
  at: string;
  client_order_id: string;
  symbol: string;
  reason: string;
  baseline_took_it: boolean;
  baseline_lot_net: Dec | null;
  verdict: "saved" | "missed" | "flat" | null;
}

export interface State {
  generated_at: string;
  mode: string;
  live_trading: boolean;
  live_adapter: string;
  quote_currency: string;
  exchange: string;
  feed: {
    name: string;
    illustrative: boolean;
    poll_interval_sec: number;
    symbols: Record<string, FeedSymbol>;
  };
  controls: {
    pause_new_buys: boolean;
    halt_all: boolean;
    liquidate_confirmed: boolean;
    history: { action: string; at: string }[];
  };
  runtime: {
    started_at: string;
    tick_count: number;
    last_tick_at: string | null;
    errors: { at: string; error: string }[];
    persisted: boolean;
  };
  news: {
    last_ok_at: string | null;
    analyst_calls: number;
    events: NewsEvent[];
    regimes: Record<string, string>;
    exchange_status: unknown;
    source: string | null;
  };
  arms: { baseline: Arm; ai: Arm };
  ab: {
    baseline_equity: Dec | null;
    ai_equity: Dec | null;
    equity_delta: Dec | null;
    ai_vetoes: Veto[];
    saved: number;
    missed: number;
    independent_events: number;
  };
  decisions: Decision[];
  paused_reasons: string[];
}
