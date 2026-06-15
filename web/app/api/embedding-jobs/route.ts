import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

export async function GET(): Promise<Response> {
  const res = await orchestratorFetch("/api/v1/embedding-jobs");
  return proxyJSON(res);
}
