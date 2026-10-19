import type { Arm, State } from "../types";
import { num, pct, signed, when } from "../format";

function Row({ label, a, b }: { label: string; a: string; b: string }) {
  return (
    <tr>
      <td>{label}</td>
      <td className="num">{a}</td>
      <td className="num">{b}</td>
    </tr>
  );
}

export default function PaperAB({ state }: { state: State }) {
  const base: Arm = state.arms.baseline;
  const ai: Arm = state.arms.ai;
  const fees = (arm: Arm) => num(arm.fees_by_currency?.[state.quote_currency] ?? "0", 4);
  return (
    <>
      <div className="panel">
        <h2>Baseline vs AI · одна лента, одинаковый стартовый капитал · счёт по equity после комиссий</h2>
        <table>
          <thead>
            <tr>
              <th>Метрика</th>
              <th className="num">baseline</th>
              <th className="num">ai</th>
            </tr>
          </thead>
          <tbody>
            <Row label={`Total equity (${state.quote_currency})`} a={num(base.equity)} b={num(ai.equity)} />
            <Row label="Realized net" a={signed(base.realized_pnl)} b={signed(ai.realized_pnl)} />
            <Row label="Unrealized net" a={signed(base.unrealized_pnl)} b={signed(ai.unrealized_pnl)} />
            <Row label="Max drawdown" a={pct(base.drawdown_pct)} b={pct(ai.drawdown_pct)} />
            <Row label="Fees (quote)" a={fees(base)} b={fees(ai)} />
            <Row label="Completed cycles" a={String(base.completed_cycles)} b={String(ai.completed_cycles)} />
            <Row label="Entries allowed / rejected" a={`${base.entries_allowed} / ${base.entries_rejected}`} b={`${ai.entries_allowed} / ${ai.entries_rejected}`} />
            <Row label="Paused entries" a={pct(base.paused_entry_pct)} b={pct(ai.paused_entry_pct)} />
            <Row label="Open lots" a={String(base.open_lots)} b={String(ai.open_lots)} />
          </tbody>
        </table>
        <div style={{ marginTop: 8 }}>
          equity delta (AI − baseline):{" "}
          <span className={`big ${Number(state.ab.equity_delta ?? 0) >= 0 ? "pos" : "neg"}`}>{signed(state.ab.equity_delta)}</span>
          <span className="muted small">
            {" "}
            · независимых событий {state.ab.independent_events} · saved {state.ab.saved} · missed {state.ab.missed}
          </span>
        </div>
        <div className="muted small">
          Паузы AI: «saved» — baseline взял тот же вход и сейчас в минусе; «missed» — baseline в плюсе. Для вывода нужны недели данных, не
          часы.
        </div>
      </div>
      <div className="panel">
        <h2>AI vetoes</h2>
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Intent</th>
              <th>Pair</th>
              <th>Reason</th>
              <th>Baseline took it</th>
              <th className="num">Baseline lot net</th>
              <th>Verdict</th>
            </tr>
          </thead>
          <tbody>
            {state.ab.ai_vetoes.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  вето не было
                </td>
              </tr>
            )}
            {state.ab.ai_vetoes.map((v) => (
              <tr key={v.client_order_id}>
                <td className="small">{when(v.at)}</td>
                <td className="small">{v.client_order_id}</td>
                <td>{v.symbol}</td>
                <td>{v.reason}</td>
                <td>{v.baseline_took_it ? "yes" : "no"}</td>
                <td className={`num ${Number(v.baseline_lot_net ?? 0) >= 0 ? "pos" : "neg"}`}>{signed(v.baseline_lot_net)}</td>
                <td className={v.verdict === "saved" ? "pos" : v.verdict === "missed" ? "neg" : "muted"}>{v.verdict ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
