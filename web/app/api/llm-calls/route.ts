import { type NextRequest } from "next/server";

import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

// GET /api/llm-calls — proxies LLM call rows from the orchestrator.
export async function GET(req: NextRequest): Promise<Response> {
  const runId = req.nextUrl.searchParams.get("run_id");
  const qs = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
  const res = await orchestratorFetch(`/api/v1/llm-calls${qs}`);
  return proxyJSON(res);
}
