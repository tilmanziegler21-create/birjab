import type { Arm, State } from "../types";
import { num, qty } from "../format";

function Fills({ arm }: { arm: Arm }) {
  const closed = arm.lots.filter((lot) => lot.status === "closed");
  return (
    <div className="panel">
      <h2>
        {arm.name} · fills {arm.fills.length} · closed lots {closed.length}
      </h2>
      <table>
        <thead>
          <tr>
            <th>fill_id</th>
            <th>Side</th>
            <th>Pair</th>
            <th className="num">Price</th>
            <th className="num">Qty</th>
            <th className="num">Fee</th>
            <th className="num">Quote</th>
            <th>Lot</th>
          </tr>
        </thead>
        <tbody>
          {arm.fills.length === 0 && (
            <tr>
              <td colSpan={8} className="muted">
                исполнений нет
              </td>
            </tr>
          )}
          {[...arm.fills].reverse().map((f) => (
            <tr key={f.fill_id}>
              <td className="small">{f.fill_id}</td>
              <td className={f.side === "buy" ? "pos" : "neg"}>{f.side}</td>
              <td>{f.symbol}</td>
              <td className="num">{num(f.price, 4)}</td>
              <td className="num">{qty(f.quantity)}</td>
              <td className="num">
                {num(f.fee_amount, 6)} {f.fee_currency}
              </td>
              <td className="num">{num(f.quote_amount, 4)}</td>
              <td className="small">{f.lot_id}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {closed.length > 0 && (
        <>
          <h2 style={{ marginTop: 12 }}>closed lots · realized</h2>
          <table>
            <thead>
              <tr>
                <th>Lot</th>
                <th className="num">Entry</th>
                <th className="num">Realized net</th>
              </tr>
            </thead>
            <tbody>
              {closed.map((lot) => (
                <tr key={lot.lot_id}>
                  <td className="small">{lot.lot_id}</td>
                  <td className="num">{num(lot.entry_price, 4)}</td>
                  <td className={`num ${Number(lot.realized_pnl) >= 0 ? "pos" : "neg"}`}>{num(lot.realized_pnl, 4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export default function Trades({ state }: { state: State }) {
  return (
    <div className="two">
      <Fills arm={state.arms.baseline} />
      <Fills arm={state.arms.ai} />
    </div>
  );
}
