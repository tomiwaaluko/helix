import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RunsTable } from "@/components/runs-table";
import { fetchRuns } from "@/lib/api";
import type { Run } from "@/lib/types";
import { renderWithClient } from "@/test/render";

vi.mock("@/lib/api", () => ({ fetchRuns: vi.fn() }));

const mockFetchRuns = vi.mocked(fetchRuns);

const RUN: Run = {
  id: "run-1",
  workflow_id: "deep_research",
  status: "running",
  submitted_at: "2026-06-14T12:00:00Z",
  submitted_by: "tomiwa",
  trace_id: "trace-1",
};

beforeEach(() => {
  mockFetchRuns.mockReset();
});

describe("RunsTable", () => {
  it("renders a run row", async () => {
    mockFetchRuns.mockResolvedValue([RUN]);
    renderWithClient(<RunsTable />);
    expect(await screen.findByText("run-1")).toBeInTheDocument();
    expect(screen.getByText("deep_research")).toBeInTheDocument();
    // "running" also appears as a filter <option>; scope to the table to hit the badge.
    expect(within(screen.getByRole("table")).getByText("running")).toBeInTheDocument();
  });

  it("shows an empty state when there are no runs", async () => {
    mockFetchRuns.mockResolvedValue([]);
    renderWithClient(<RunsTable />);
    expect(await screen.findByText("No runs yet.")).toBeInTheDocument();
  });

  it("passes the selected status filter to fetchRuns", async () => {
    mockFetchRuns.mockResolvedValue([]);
    renderWithClient(<RunsTable />);
    await screen.findByText("No runs yet.");

    await userEvent.selectOptions(screen.getByLabelText("Status"), "failed");
    await waitFor(() => expect(mockFetchRuns).toHaveBeenLastCalledWith("failed"));
  });

  it("renders an error message on failure", async () => {
    mockFetchRuns.mockRejectedValue(new Error("boom"));
    renderWithClient(<RunsTable />);
    expect(await screen.findByText(/Failed to load runs/)).toBeInTheDocument();
  });
});
