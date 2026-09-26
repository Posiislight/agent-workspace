import type { Artifacts } from "../api/types";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="card artifact">
      <h4>{title}</h4>
      {children}
    </section>
  );
}

export default function ArtifactsView({ artifacts }: { artifacts: Artifacts }) {
  const a = artifacts;
  const empty =
    !a.plan && !a.research_notes && !a.code_diff && !a.review_comments?.length && !a.test_results;
  if (empty) return <p className="muted">No artifacts yet.</p>;
  return (
    <div className="artifacts">
      {a.plan && (
        <Section title="Plan">
          <pre>{a.plan}</pre>
        </Section>
      )}
      {a.research_notes && (
        <Section title="Research notes">
          <pre>{a.research_notes}</pre>
        </Section>
      )}
      {a.code_diff && (
        <Section title="Code diff">
          <pre className="diff">{a.code_diff}</pre>
        </Section>
      )}
      {a.test_results && (
        <Section title={`Tests — ${a.test_results.passed ? "passed" : "failed"}`}>
          {a.test_results.failing_tests?.length > 0 && (
            <ul className="failing">
              {a.test_results.failing_tests.map((t) => (
                <li key={t} className="mono">
                  {t}
                </li>
              ))}
            </ul>
          )}
          {a.test_results.failing_output && <pre>{a.test_results.failing_output}</pre>}
        </Section>
      )}
      {a.review_comments && a.review_comments.length > 0 && (
        <Section title="Review comments">
          <ul>
            {a.review_comments.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}
