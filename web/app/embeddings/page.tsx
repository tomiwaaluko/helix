import { EmbeddingJobTable } from "@/components/embedding-job-table";

export default function EmbeddingsPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Embedding Jobs</h1>
      <EmbeddingJobTable />
    </div>
  );
}
