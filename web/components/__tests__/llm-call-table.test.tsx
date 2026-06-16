import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LlmCallTable } from "@/components/llm-call-table";
import { fetchLlmCalls } from "@/lib/api";
import type { LlmCallRow } from "@/lib/types";
import { renderWithClient } from "@/test/render";

vi.mock("@/lib/api", () => ({ fetchLlmCalls: vi.fn() }));

const mockFetchLlmCalls = vi.mocked(fetchLlmCalls);

const ROW: LlmCallRow = {
  trace_id: "trace-abc",
  span_id: "span-123",
  run_id: "run-xyz",
  provider: "anthropic",
  model: "claude-sonnet-4-20250514",
  prompt_tokens: 512,
  completion_tokens: 128,
  total_tokens: 640,
  cost_usd: 0.003,
  start_time: "2026-06-15T10:00:00Z",
  duration_ms: 1500,
};

beforeEach(() => {
  mockFetchLlmCalls.mockReset();
});

describe("LlmCallTable", () => {
  it("renders an LLM call row with all fields", async () => {
    mockFetchLlmCalls.mockResolvedValue([ROW]);
    renderWithClient(<LlmCallTable />);
    expect(await screen.findByText("claude-sonnet-4-20250514")).toBeInTheDocument();
    expect(screen.getByText("anthropic")).toBeInTheDocument();
    expect(screen.getByText("512")).toBeInTheDocument();
    expect(screen.getByText("run-xyz")).toBeInTheDocument();
  });

  it("shows empty state when no rows", async () => {
    mockFetchLlmCalls.mockResolvedValue([]);
    renderWithClient(<LlmCallTable />);
    expect(await screen.findByText("No LLM call data yet.")).toBeInTheDocument();
  });

  it("shows 503 message on service unavailable error", async () => {
    mockFetchLlmCalls.mockRejectedValue(new Error("request failed (503): clickhouse not set"));
    renderWithClient(<LlmCallTable />);
    expect(await screen.findByText(/ClickHouse is not configured/)).toBeInTheDocument();
  });

  it("shows generic error message on other failures", async () => {
    mockFetchLlmCalls.mockRejectedValue(new Error("network timeout"));
    renderWithClient(<LlmCallTable />);
    expect(await screen.findByText(/Failed to load LLM calls/)).toBeInTheDocument();
  });
});
