import type { State } from "../types";
import { num, pct, when } from "../format";

export default function Markets({ state }: { state: State }) {
  const rows = Object.entries(state.feed.symbols);
  return (
    <div className="panel">
      <h2>Markets · {state.feed.name}</h2>
      <table>
        <thead>
          <tr>
            <th>Pair</th>
            <th className="num">Bid</th>
            <th className="num">Ask</th>
            <th className="num">Spread</th>
            <th className="num">Depth bid / ask</th>
            <th className="num">Latency</th>
            <th>Status</th>
            <th>Regime</th>
            <th>Allowed</th>
            <th>Reasons</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={11} className="muted">
                ещё не было тика
              </td>
            </tr>
          )}
          {rows.map(([symbol, s]) => {
            const base = symbol.split("/")[0];
            const regime = state.news.regimes[base] ?? "NORMAL";
            return (
              <tr key={symbol}>
                <td>{symbol}</td>
                <td className="num">{num(s.bid, 4)}</td>
                <td className="num">{num(s.ask, 4)}</td>
                <td className="num">{pct(s.spread_pct)}</td>
                <td className="num">
                  {num(s.bid_depth_quote, 0)} / {num(s.ask_depth_quote, 0)}
                </td>
                <td className="num">{s.latency_ms ?? "—"} ms</td>
                <td>
                  {s.ok ? s.market_status : <span className="error">{s.error}</span>}
                  {s.stale && <span className="warn"> stale</span>}
                </td>
                <td>
                  <span className={`tag ${regime}`}>{regime}</span>
                </td>
                <td className={s.ok && s.allowed ? "pos" : "neg"}>{s.ok ? (s.allowed ? "allowed" : "blocked") : "no feed"}</td>
                <td className="small">{(s.reasons ?? []).join(", ")}</td>
                <td className="small">{when(s.updated_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
