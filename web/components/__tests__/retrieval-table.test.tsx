import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RetrievalTable } from "@/components/retrieval-table";
import { fetchRetrievals } from "@/lib/api";
import type { RetrievalRow } from "@/lib/types";
import { renderWithClient } from "@/test/render";

vi.mock("@/lib/api", () => ({ fetchRetrievals: vi.fn() }));

const mockFetchRetrievals = vi.mocked(fetchRetrievals);

const ROW: RetrievalRow = {
  trace_id: "trace-abc",
  span_id: "span-123",
  run_id: "run-xyz",
  query: "Who wrote Hamlet?",
  retriever: "hybrid+reranked",
  top_k: 10,
  recall_at_k: 1,
  start_time: "2026-06-15T10:00:00Z",
  duration_ms: 42,
};

beforeEach(() => {
  mockFetchRetrievals.mockReset();
});

describe("RetrievalTable", () => {
  it("renders a retrieval row with all fields", async () => {
    mockFetchRetrievals.mockResolvedValue([ROW]);
    renderWithClient(<RetrievalTable />);
    expect(await screen.findByText("Who wrote Hamlet?")).toBeInTheDocument();
    expect(screen.getByText("hybrid+reranked")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("run-xyz")).toBeInTheDocument();
  });

  it("shows empty state when no rows", async () => {
    mockFetchRetrievals.mockResolvedValue([]);
    renderWithClient(<RetrievalTable />);
    expect(await screen.findByText("No retrieval data yet.")).toBeInTheDocument();
  });

  it("shows 503 message on service unavailable error", async () => {
    mockFetchRetrievals.mockRejectedValue(new Error("request failed (503): clickhouse not set"));
    renderWithClient(<RetrievalTable />);
    expect(
      await screen.findByText(/ClickHouse is not configured/),
    ).toBeInTheDocument();
  });

  it("shows generic error message on other failures", async () => {
    mockFetchRetrievals.mockRejectedValue(new Error("network timeout"));
    renderWithClient(<RetrievalTable />);
    expect(await screen.findByText(/Failed to load retrievals/)).toBeInTheDocument();
  });
});
