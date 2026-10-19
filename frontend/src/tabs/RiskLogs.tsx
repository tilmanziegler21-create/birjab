import type { State } from "../types";
import { when } from "../format";

export default function RiskLogs({ state }: { state: State }) {
  const decisions = [...state.decisions].reverse();
  return (
    <>
      <div className="two">
        <div className="panel">
          <h2>Reject reasons</h2>
          <table>
            <thead>
              <tr>
                <th>Reason</th>
                <th className="num">baseline</th>
                <th className="num">ai</th>
              </tr>
            </thead>
            <tbody>
              {Array.from(
                new Set([...Object.keys(state.arms.baseline.reject_reasons), ...Object.keys(state.arms.ai.reject_reasons)]),
              ).map((reason) => (
                <tr key={reason}>
                  <td>{reason}</td>
                  <td className="num">{state.arms.baseline.reject_reasons[reason] ?? 0}</td>
                  <td className="num">{state.arms.ai.reject_reasons[reason] ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h2 style={{ marginTop: 12 }}>Strategy skips</h2>
          <div className="small">
            {(["baseline", "ai"] as const).map((name) => (
              <div key={name}>
                <b>{name}</b>:{" "}
                {Object.entries(state.arms[name].strategy_skips)
                  .filter(([, skips]) => skips.length > 0)
                  .map(([symbol, skips]) => `${symbol} → ${skips.join(", ")}`)
                  .join("; ") || "—"}
              </div>
            ))}
          </div>
        </div>
        <div className="panel">
          <h2>Controls history</h2>
          <ul className="plain small">
            {state.controls.history.length === 0 && <li className="muted">—</li>}
            {[...state.controls.history].reverse().map((h, i) => (
              <li key={i}>
                {when(h.at)} · {h.action}
              </li>
            ))}
          </ul>
          <h2 style={{ marginTop: 12 }}>Runtime errors</h2>
          <ul className="plain small">
            {state.runtime.errors.length === 0 && <li className="muted">нет</li>}
            {[...state.runtime.errors].reverse().map((e, i) => (
              <li key={i} className="error">
                {when(e.at)} · {e.error}
              </li>
            ))}
          </ul>
        </div>
      </div>
      <div className="panel">
        <h2>Decisions (RiskManager)</h2>
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Arm</th>
              <th>Intent</th>
              <th>Pair</th>
              <th>Side</th>
              <th>Allowed</th>
              <th>Reason</th>
              <th>Regime</th>
              <th>State</th>
              <th className="num">Repeats</th>
            </tr>
          </thead>
          <tbody>
            {decisions.length === 0 && (
              <tr>
                <td colSpan={10} className="muted">
                  решений ещё не было
                </td>
              </tr>
            )}
            {decisions.map((d, i) => (
              <tr key={`${d.client_order_id}-${d.at}-${i}`}>
                <td className="small">{when(d.at)}</td>
                <td>{d.arm}</td>
                <td className="small">{d.client_order_id}</td>
                <td>{d.symbol}</td>
                <td>{d.side}</td>
                <td className={d.allowed ? "pos" : "neg"}>{d.allowed ? "allow" : "reject"}</td>
                <td>{d.reason}</td>
                <td>{d.regime && <span className={`tag ${d.regime}`}>{d.regime}</span>}</td>
                <td>{d.state ?? "—"}</td>
                <td className="num" title={d.last_at ? `последний: ${when(d.last_at)}` : undefined}>
                  {d.repeats ?? 1}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
