import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("orchestratorFetch", () => {
  it("attaches the bearer token and targets the orchestrator URL", async () => {
    vi.stubEnv("ORCHESTRATOR_URL", "http://orch:8080");
    vi.stubEnv("HELIX_API_TOKEN", "secret-token");
    const fetchMock = vi.fn().mockResolvedValue(new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const { orchestratorFetch } = await import("@/lib/orchestrator");
    await orchestratorFetch("/api/v1/runs?status=failed");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://orch:8080/api/v1/runs?status=failed");
    const headers = init.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer secret-token");
    expect(init.cache).toBe("no-store");
  });
});

describe("proxyJSON", () => {
  it("preserves status and body as a JSON response", async () => {
    const { proxyJSON } = await import("@/lib/orchestrator");
    const upstream = new Response('{"error":"nope"}', { status: 404 });
    const out = await proxyJSON(upstream);
    expect(out.status).toBe(404);
    expect(out.headers.get("Content-Type")).toBe("application/json");
    expect(await out.text()).toBe('{"error":"nope"}');
  });
});
