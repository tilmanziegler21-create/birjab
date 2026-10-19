import type { State } from "./types";

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${text}`);
  }
  return (await response.json()) as T;
}

export async function fetchState(): Promise<State> {
  return json<State>(await fetch("/api/state"));
}

export async function postControl(action: "pause" | "halt" | "resume"): Promise<void> {
  await json(await fetch(`/api/controls/${action}`, { method: "POST" }));
}

export async function postLiquidate(confirmation: string): Promise<void> {
  await json(
    await fetch("/api/controls/liquidate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation }),
    }),
  );
}

export async function postTick(): Promise<void> {
  await json(await fetch("/api/tick", { method: "POST" }));
}
