import type { Arm, State } from "../types";
import { num, qty } from "../format";

function LotTable({ arm }: { arm: Arm }) {
  const open = arm.lots.filter((lot) => lot.status !== "closed");
  return (
    <div className="panel">
      <h2>
        {arm.name} · open lots {open.length}
      </h2>
      <table>
        <thead>
          <tr>
            <th>Lot</th>
            <th>Pair</th>
            <th className="num">Entry</th>
            <th className="num">Mark bid</th>
            <th className="num">Remaining</th>
            <th className="num">Held for exit</th>
            <th className="num">Cost remaining</th>
            <th className="num">Entry fee</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {open.length === 0 && (
            <tr>
              <td colSpan={9} className="muted">
                нет открытых лотов
              </td>
            </tr>
          )}
          {open.map((lot) => (
            <tr key={lot.lot_id}>
              <td className="small">{lot.lot_id}</td>
              <td>{lot.symbol}</td>
              <td className="num">{num(lot.entry_price, 4)}</td>
              <td className="num">{num(lot.mark_bid, 4)}</td>
              <td className="num">{qty(lot.remaining_base_qty)}</td>
              <td className="num">{qty(lot.reserved_base_qty)}</td>
              <td className="num">{num(lot.quote_cost_remaining, 4)}</td>
              <td className="num">
                {num(lot.entry_fee_amount, 6)} {lot.entry_fee_currency}
              </td>
              <td>{lot.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h2 style={{ marginTop: 12 }}>working intents</h2>
      <table>
        <thead>
          <tr>
            <th>client_order_id</th>
            <th>Pair</th>
            <th>Side</th>
            <th>Purpose</th>
            <th>State</th>
            <th className="num">Limit</th>
            <th className="num">Budget</th>
            <th className="num">Filled</th>
          </tr>
        </thead>
        <tbody>
          {arm.working.filter((w) => !["FILLED", "CANCELED", "REJECTED"].includes(w.state)).map((w) => (
            <tr key={w.client_order_id}>
              <td className="small">{w.client_order_id}</td>
              <td>{w.symbol}</td>
              <td>{w.side}</td>
              <td>{w.purpose}</td>
              <td>{w.state}</td>
              <td className="num">{num(w.limit_price, 4)}</td>
              <td className="num">{w.side === "buy" ? num(w.quote_budget) : qty(w.base_budget)}</td>
              <td className="num">{w.side === "buy" ? num(w.filled_quote) : qty(w.filled_base)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function OpenLots({ state }: { state: State }) {
  return (
    <div className="two">
      <LotTable arm={state.arms.baseline} />
      <LotTable arm={state.arms.ai} />
    </div>
  );
}
