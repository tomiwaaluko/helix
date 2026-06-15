import { type NextRequest } from "next/server";

import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

export async function GET(): Promise<Response> {
  const res = await orchestratorFetch("/api/v1/finetune-jobs");
  return proxyJSON(res);
}

export async function POST(req: NextRequest): Promise<Response> {
  const body = await req.text();
  const res = await orchestratorFetch("/api/v1/finetune-jobs", {
    method: "POST",
    body,
  });
  return proxyJSON(res);
}
