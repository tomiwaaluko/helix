import { RunsTable } from "@/components/runs-table";

export default function RunsPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Runs</h1>
      <RunsTable />
    </div>
  );
}
