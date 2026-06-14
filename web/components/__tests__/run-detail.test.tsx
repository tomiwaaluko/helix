import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RunDetail } from "@/components/run-detail";
import { cancelRun, fetchRun } from "@/lib/api";
import type { RunDetail as RunDetailType } from "@/lib/types";
import { renderWithClient } from "@/test/render";

vi.mock("@/lib/api", () => ({ fetchRun: vi.fn(), cancelRun: vi.fn() }));

const mockFetchRun = vi.mocked(fetchRun);
const mockCancelRun = vi.mocked(cancelRun);

function runDetail(overrides: Partial<RunDetailType> = {}): RunDetailType {
  return {
    id: "run-1",
    workflow_id: "deep_research",
    status: "running",
    submitted_at: "2026-06-14T12:00:00Z",
    submitted_by: "tomiwa",
    trace_id: "trace-1",
    tasks: [{ id: "task-1", run_id: "run-1", node_id: "root", status: "running", attempts: 1 }],
    ...overrides,
  };
}

beforeEach(() => {
  mockFetchRun.mockReset();
  mockCancelRun.mockReset();
});

describe("RunDetail", () => {
  it("renders run metadata and the task tree", async () => {
    mockFetchRun.mockResolvedValue(runDetail());
    renderWithClient(<RunDetail id="run-1" />);
    expect(await screen.findByText("run-1")).toBeInTheDocument();
    expect(screen.getByText("root")).toBeInTheDocument();
    expect(screen.getByText("task-1")).toBeInTheDocument();
  });

  it("shows a Cancel button for a non-terminal run and calls cancelRun", async () => {
    mockFetchRun.mockResolvedValue(runDetail({ status: "running" }));
    mockCancelRun.mockResolvedValue(undefined);
    renderWithClient(<RunDetail id="run-1" />);

    const button = await screen.findByRole("button", { name: "Cancel" });
    await userEvent.click(button);
    await waitFor(() => expect(mockCancelRun).toHaveBeenCalledWith("run-1"));
  });

  it("hides the Cancel button for a terminal run", async () => {
    mockFetchRun.mockResolvedValue(runDetail({ status: "succeeded" }));
    renderWithClient(<RunDetail id="run-1" />);
    await screen.findByText("run-1");
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });
});
