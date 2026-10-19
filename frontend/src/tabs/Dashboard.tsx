import type { Arm, State } from "../types";
import { num, pct, signed, when } from "../format";

function ArmCards({ arm, quote }: { arm: Arm; quote: string }) {
  const fees = arm.fees_by_currency ?? {};
  return (
    <div className="panel">
      <h2>
        {arm.name} · {arm.strategy_id}
        {arm.trading_blocked && <span className="error"> · BLOCKED {arm.block_reason}</span>}
      </h2>
      {arm.error && <div className="error">{arm.error}</div>}
      <div className="grid">
        <div>
          <div className="muted">True equity ({quote})</div>
          <div className="big">{num(arm.equity)}</div>
        </div>
        <div>
          <div className="muted">Free / Reserved</div>
          <div>
            {num(arm.free_quote)} / {num(arm.reserved_quote)}
          </div>
        </div>
        <div>
          <div className="muted">Exposure</div>
          <div>
            {num(arm.gross_position_value)} ({pct(arm.exposure_pct)})
          </div>
        </div>
        <div>
          <div className="muted">Realized / Unrealized net</div>
          <div>
            <span className={Number(arm.realized_pnl) >= 0 ? "pos" : "neg"}>{signed(arm.realized_pnl)}</span>
            {" / "}
            <span className={Number(arm.unrealized_pnl) >= 0 ? "pos" : "neg"}>{signed(arm.unrealized_pnl)}</span>
          </div>
        </div>
        <div>
          <div className="muted">Fees paid</div>
          <div>
            {Object.keys(fees).length === 0
              ? "0"
              : Object.entries(fees)
                  .map(([cur, amount]) => `${num(amount, 4)} ${cur}`)
                  .join(", ")}
          </div>
        </div>
        <div>
          <div className="muted">Drawdown / peak</div>
          <div>
            {pct(arm.drawdown_pct)} / {num(arm.peak_equity)}
          </div>
        </div>
        <div>
          <div className="muted">Cycles / open lots</div>
          <div>
            {arm.completed_cycles} / {arm.open_lots}
          </div>
        </div>
        <div>
          <div className="muted">Active pairs</div>
          <div>{arm.active_symbols.join(", ") || "—"}</div>
        </div>
      </div>
    </div>
  );
}

export default function Dashboard({ state }: { state: State }) {
  const feeds = Object.entries(state.feed.symbols);
  const okFeeds = feeds.filter(([, s]) => s.ok && !s.stale).length;
  return (
    <>
      <div className="two">
        <ArmCards arm={state.arms.baseline} quote={state.quote_currency} />
        <ArmCards arm={state.arms.ai} quote={state.quote_currency} />
      </div>
      <div className="grid">
        <div className="panel">
          <h2>Feed status</h2>
          <div className="big">
            {okFeeds}/{feeds.length}
          </div>
          <div className="muted small">
            {state.feed.name} · опрос каждые {state.feed.poll_interval_sec} с
          </div>
        </div>
        <div className="panel">
          <h2>News</h2>
          <div>last ok: {when(state.news.last_ok_at)}</div>
          <div className="muted small">
            событий {state.news.events.length} · вызовов аналитика {state.news.analyst_calls}
          </div>
        </div>
        <div className="panel">
          <h2>A/B equity delta (AI − baseline)</h2>
          <div className={`big ${Number(state.ab.equity_delta ?? 0) >= 0 ? "pos" : "neg"}`}>{signed(state.ab.equity_delta)}</div>
          <div className="muted small">
            saved {state.ab.saved} · missed {state.ab.missed}
          </div>
        </div>
        <div className="panel">
          <h2>Runtime</h2>
          <div>started {when(state.runtime.started_at)}</div>
          <div className="muted small">
            persisted {String(state.runtime.persisted)} · errors {state.runtime.errors.length}
          </div>
        </div>
      </div>
      <div className="panel">
        <h2>Paused reasons</h2>
        {state.paused_reasons.length === 0 ? (
          <span className="muted">нет</span>
        ) : (
          <ul className="plain">
            {state.paused_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
