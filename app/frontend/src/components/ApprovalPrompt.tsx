import { useState } from "react";

export default function ApprovalPrompt({
  onDecide,
  busy,
}: {
  onDecide: (decision: "approve" | "reject", feedback: string) => void;
  busy: boolean;
}) {
  const [feedback, setFeedback] = useState("");
  return (
    <section className="approval card">
      <h3>Approval required</h3>
      <p className="muted">
        The pipeline is paused. Review the plan, diff and test results below, then approve to
        continue to commit &amp; PR — or reject with corrections.
      </p>
      <textarea
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        placeholder="Optional feedback (required corrections if rejecting)…"
        rows={4}
      />
      <div className="row buttons">
        <button className="btn success" disabled={busy} onClick={() => onDecide("approve", feedback)}>
          Approve
        </button>
        <button className="btn danger" disabled={busy} onClick={() => onDecide("reject", feedback)}>
          Reject
        </button>
      </div>
    </section>
  );
}
