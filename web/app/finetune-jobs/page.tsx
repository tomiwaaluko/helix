import { FinetuneJobTable } from "@/components/finetune-job-table";

export default function FinetuneJobsPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Fine-tune Jobs</h1>
      <FinetuneJobTable />
    </div>
  );
}
