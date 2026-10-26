export interface DemoTrade {
  symbol: string;
  side: 'buy' | 'sell' | 'unknown';
  volume: number | null;
  entry: number | null;
  exit: number | null;
  profit: number | null;
  opened_at: string | null;
  closed_at: string | null;
}
export interface DemoSnapshot {
  account: {
    balance: number;
    equity: number;
    floating_profit: number | null;
    currency: string;
    mode: 'real' | 'demo' | 'unknown';
  } | null;
  account_updated_at: number | null;
  history_updated_at: number | null;
  copytrader: {
    closed: number;
    settled: number;
    wins: number;
    losses: number;
    profit: number | null;
    win_rate: number | null;
    open: number;
    curve: { date: string; profit: number }[];
    trades: DemoTrade[];
  } | null;
  status: { broker: boolean; telegram: boolean; running: boolean };
  generated_at: number;
}
