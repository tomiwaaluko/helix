import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** Pretty-print JSON bytes that Go marshalled as a base64 string ([]byte). */
export function decodeJsonBytes(b64: string | undefined): string {
  if (!b64) return "";
  try {
    const decoded = atob(b64);
    return JSON.stringify(JSON.parse(decoded), null, 2);
  } catch {
    return b64;
  }
}

/** Format an ISO-8601 timestamp for display; returns the raw string on failure. */
export function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}
