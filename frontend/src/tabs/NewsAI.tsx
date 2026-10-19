import type { State } from "../types";
import { num, when } from "../format";

export default function NewsAI({ state }: { state: State }) {
  const { news } = state;
  return (
    <>
      <div className="grid">
        <div className="panel">
          <h2>Source</h2>
          <div>{news.source ?? "—"}</div>
          <div className="muted small">только фикстуры; реальные источники не подключены</div>
        </div>
        <div className="panel">
          <h2>Last OK</h2>
          <div>{when(news.last_ok_at)}</div>
        </div>
        <div className="panel">
          <h2>Analyst calls</h2>
          <div className="big">{news.analyst_calls}</div>
          <div className="muted small">MockAIAnalyst, без API-ключа и без стоимости</div>
        </div>
        <div className="panel">
          <h2>Regimes</h2>
          <div>
            {Object.entries(news.regimes).map(([base, regime]) => (
              <span key={base} className={`tag ${regime}`}>
                {base} {regime}
              </span>
            ))}
          </div>
        </div>
      </div>
      <div className="panel">
        <h2>События по времени поступления (first_seen_at)</h2>
        <table>
          <thead>
            <tr>
              <th>first_seen</th>
              <th>published</th>
              <th>Headline</th>
              <th>Symbols</th>
              <th>Regime</th>
              <th className="num">Weight</th>
              <th className="num">Reprints</th>
              <th>Sources / links</th>
              <th>Bid now</th>
            </tr>
          </thead>
          <tbody>
            {news.events.length === 0 && (
              <tr>
                <td colSpan={9} className="muted">
                  событий нет
                </td>
              </tr>
            )}
            {news.events.map((e) => (
              <tr key={e.canonical_event_id}>
                <td className="small">{when(e.first_seen_at)}</td>
                <td className="small">{when(e.published_at)}</td>
                <td>
                  {e.headline}
                  {e.illustrative && <span className="tag">illustrative</span>}
                  <div className="muted small">{e.summary}</div>
                </td>
                <td>{e.symbols.join(", ")}</td>
                <td>
                  <span className={`tag ${e.regime}`}>{e.regime}</span>
                </td>
                <td className="num">{num(e.weight, 3)}</td>
                <td className="num">{e.seen_count}</td>
                <td className="small">
                  {e.sources.join(", ")}
                  <div className="muted">{e.links.length} ссылок</div>
                </td>
                <td className="small">
                  {Object.entries(e.price_reaction)
                    .map(([base, bid]) => `${base}: ${num(bid, 4)}`)
                    .join(" · ") || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
