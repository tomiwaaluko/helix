import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => vi.restoreAllMocks());

describe("fetchTrace", () => {
  it("calls the trace BFF path and returns parsed JSON", async () => {
    const payload = { trace_id: "trace-abc", spans: [] };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(payload), { status: 200 }),
    );

    const { fetchTrace } = await import("@/lib/api");
    const result = await fetchTrace("run-xyz");

    expect(fetch).toHaveBeenCalledWith("/api/runs/run-xyz/trace");
    expect(result.trace_id).toBe("trace-abc");
  });

  it("throws on non-2xx responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("not configured", { status: 503 }),
    );

    const { fetchTrace } = await import("@/lib/api");
    await expect(fetchTrace("run-xyz")).rejects.toThrow("503");
  });
});
