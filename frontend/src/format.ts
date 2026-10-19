import type { Dec } from "./types";

export function num(value: Dec | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return n.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function qty(value: Dec | null | undefined): string {
  return num(value, 6);
}

export function pct(value: Dec | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${num(value, 3)} %`;
}

export function when(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString("ru-RU", { hour12: false });
}

export function signed(value: Dec | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  const text = num(value, 4);
  return n > 0 ? `+${text}` : text;
}
