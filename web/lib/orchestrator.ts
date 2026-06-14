// Server-only helper for talking to the Go orchestrator REST API.
//
// Imported only by Next.js route handlers (app/api/**), which always run on the
// server, so the bearer token never reaches the browser. Do not import this from a
// client component.

const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL ?? "http://localhost:8080";
const API_TOKEN = process.env.HELIX_API_TOKEN ?? "";

export async function orchestratorFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${API_TOKEN}`);
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(`${ORCHESTRATOR_URL}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });
}

/** Pass an orchestrator Response through as a same-origin JSON response. */
export async function proxyJSON(res: Response): Promise<Response> {
  const body = await res.text();
  return new Response(body, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
