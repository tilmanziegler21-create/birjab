import { useCallback, useEffect, useState } from "react";
import { fetchState, postControl, postLiquidate, postTick } from "./api";
import type { State } from "./types";
import { when } from "./format";
import Dashboard from "./tabs/Dashboard";
import Markets from "./tabs/Markets";
import NewsAI from "./tabs/NewsAI";
import OpenLots from "./tabs/OpenLots";
import Trades from "./tabs/Trades";
import PaperAB from "./tabs/PaperAB";
import RiskLogs from "./tabs/RiskLogs";

const TABS = ["Dashboard", "Markets", "News & AI", "Open Lots", "Trades", "Paper A/B", "Risk & Logs"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [tab, setTab] = useState<Tab>("Dashboard");
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState("");

  const refresh = useCallback(async () => {
    try {
      setState(await fetchState());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const controls = state?.controls;

  return (
    <>
      <header>
        <h1>AI Trading System</h1>
        <span className={`banner ${state && !state.live_trading ? "ok" : "bad"}`}>
          {state ? `${state.mode.toUpperCase()} · live ${state.live_adapter}` : "…"}
        </span>
        {state && (
          <span className={`banner ${state.feed.illustrative ? "" : "ok"}`}>
            feed {state.feed.name}
            {state.feed.illustrative ? " (синтетика, не рынок)" : " (public read-only)"}
          </span>
        )}
        {state && <span className="banner">quote {state.quote_currency}</span>}
        {state && (
          <span className="banner">
            tick #{state.runtime.tick_count} · {when(state.runtime.last_tick_at)}
          </span>
        )}
        {error && <span className="banner bad">{error}</span>}
      </header>
      <nav>
        {TABS.map((name) => (
          <button key={name} className={name === tab ? "active" : ""} onClick={() => setTab(name)}>
            {name}
          </button>
        ))}
      </nav>
      <main>
        {state && controls && (
          <div className="panel controls">
            <button className="pause" disabled={busy || controls.pause_new_buys} onClick={() => act(() => postControl("pause"))}>
              PAUSE NEW BUYS
            </button>
            <button disabled={busy || controls.halt_all} onClick={() => act(() => postControl("halt"))}>
              HALT ALL
            </button>
            <button disabled={busy || (!controls.pause_new_buys && !controls.halt_all)} onClick={() => act(() => postControl("resume"))}>
              RESUME
            </button>
            <input
              placeholder="введите CONFIRM_LIQUIDATE"
              value={confirmation}
              onChange={(e) => setConfirmation(e.target.value)}
            />
            <button
              className="stop"
              disabled={busy || confirmation !== "CONFIRM_LIQUIDATE"}
              onClick={() =>
                act(async () => {
                  await postLiquidate(confirmation);
                  setConfirmation("");
                })
              }
            >
              STOP &amp; LIQUIDATE (может закрыть с убытком)
            </button>
            <button disabled={busy} onClick={() => act(postTick)} title="ручной шаг; отклоняется, если фоновый цикл запущен">
              TICK
            </button>
            <span className="muted small">
              pause {String(controls.pause_new_buys)} · halt {String(controls.halt_all)} · liquidate {String(controls.liquidate_confirmed)}
            </span>
          </div>
        )}
        {!state && !error && <div className="panel">Загрузка…</div>}
        {state && tab === "Dashboard" && <Dashboard state={state} />}
        {state && tab === "Markets" && <Markets state={state} />}
        {state && tab === "News & AI" && <NewsAI state={state} />}
        {state && tab === "Open Lots" && <OpenLots state={state} />}
        {state && tab === "Trades" && <Trades state={state} />}
        {state && tab === "Paper A/B" && <PaperAB state={state} />}
        {state && tab === "Risk & Logs" && <RiskLogs state={state} />}
      </main>
    </>
  );
}
