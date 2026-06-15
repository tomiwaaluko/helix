import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

// GET /api/evals — proxies list of eval summaries from the orchestrator.
export async function GET(): Promise<Response> {
  const res = await orchestratorFetch("/api/v1/evals");
  return proxyJSON(res);
}
