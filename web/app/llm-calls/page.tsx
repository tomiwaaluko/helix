import { LlmCallTable } from "@/components/llm-call-table";

export default function LlmCallsPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">LLM Calls</h1>
      <LlmCallTable />
    </div>
  );
}
