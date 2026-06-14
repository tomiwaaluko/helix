import type { NextRequest } from "next/server";

import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

// GET /api/runs?status= — proxies to the orchestrator with the bearer token.
export async function GET(req: NextRequest): Promise<Response> {
  const status = req.nextUrl.searchParams.get("status");
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  const res = await orchestratorFetch(`/api/v1/runs${qs}`);
  return proxyJSON(res);
}
