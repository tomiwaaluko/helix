import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SpanTree } from "@/components/span-tree";
import { fetchTrace } from "@/lib/api";
import type { TraceResponse } from "@/lib/types";
import { renderWithClient } from "@/test/render";

vi.mock("@/lib/api", () => ({ fetchTrace: vi.fn() }));

const mockFetchTrace = vi.mocked(fetchTrace);

function makeTrace(overrides: Partial<TraceResponse> = {}): TraceResponse {
  return {
    trace_id: "trace-abc",
    spans: [
      {
        span_id: "span-1",
        parent_span_id: "",
        run_id: "run-1",
        task_id: "task-1",
        attempt_number: 1,
        name: "task.root",
        kind: "task",
        start_time: "2026-06-15T00:00:00Z",
        end_time: "2026-06-15T00:00:01Z",
        duration_ms: 1000,
        status: "ok",
        attributes: { model: "claude-sonnet-4-6" },
      },
      {
        span_id: "span-2",
        parent_span_id: "span-1",
        run_id: "run-1",
        task_id: "task-1",
        attempt_number: 1,
        name: "llm.completion",
        kind: "llm",
        start_time: "2026-06-15T00:00:00.1Z",
        end_time: "2026-06-15T00:00:00.9Z",
        duration_ms: 800,
        status: "ok",
        attributes: {
          cost_usd: "0.0023",
          prompt_url: "https://minio.example.com/presigned/prompt",
        },
      },
    ],
    ...overrides,
  };
}

describe("SpanTree", () => {
  it("shows skeletons while loading", () => {
    mockFetchTrace.mockImplementation(() => new Promise(() => {}));
    renderWithClient(<SpanTree runId="run-1" />);
    const skeletons = document.querySelectorAll(".animate-pulse");
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it("renders root span names after load", async () => {
    mockFetchTrace.mockResolvedValue(makeTrace());
    renderWithClient(<SpanTree runId="run-1" />);
    await waitFor(() => screen.getByText("task.root"));
    // Child spans are hidden until the parent is expanded.
    expect(screen.getByText("task.root")).toBeInTheDocument();
    expect(screen.queryByText("llm.completion")).not.toBeInTheDocument();
  });

  it("shows trace_id in the header", async () => {
    mockFetchTrace.mockResolvedValue(makeTrace());
    renderWithClient(<SpanTree runId="run-1" />);
    await waitFor(() => screen.getByText(/trace-abc/));
  });

  it("shows 503 message when trace service is unavailable", async () => {
    mockFetchTrace.mockRejectedValue(
      new Error("request failed (503): trace service not configured"),
    );
    renderWithClient(<SpanTree runId="run-1" />);
    await waitFor(() => screen.getByText(/Trace not available/));
  });

  it("shows generic error for non-503 failures", async () => {
    mockFetchTrace.mockRejectedValue(new Error("request failed (500): internal error"));
    renderWithClient(<SpanTree runId="run-1" />);
    await waitFor(() => screen.getByText(/Failed to load trace/));
  });

  it("shows empty message when there are no spans", async () => {
    mockFetchTrace.mockResolvedValue({ trace_id: "trace-abc", spans: [] });
    renderWithClient(<SpanTree runId="run-1" />);
    await waitFor(() => screen.getByText(/No spans recorded/));
  });

  it("expands span to show attributes on click", async () => {
    const user = userEvent.setup();
    mockFetchTrace.mockResolvedValue(makeTrace());
    renderWithClient(<SpanTree runId="run-1" />);
    await waitFor(() => screen.getByText("task.root"));

    // Attributes are hidden until the span row is clicked.
    expect(screen.queryByText("claude-sonnet-4-6")).not.toBeInTheDocument();
    await user.click(screen.getByText("task.root"));
    await waitFor(() => screen.getByText("claude-sonnet-4-6"));
  });

  it('renders "Load payload" button for presigned URL attributes', async () => {
    const user = userEvent.setup();
    mockFetchTrace.mockResolvedValue(makeTrace());
    renderWithClient(<SpanTree runId="run-1" />);

    // Expand the parent span first so the child span row becomes visible.
    await waitFor(() => screen.getByText("task.root"));
    await user.click(screen.getByText("task.root"));

    // Now the child span row is visible — expand it to see its attributes.
    await waitFor(() => screen.getByText("llm.completion"));
    await user.click(screen.getByText("llm.completion"));
    await waitFor(() => screen.getByText("Load payload"));
  });
});
