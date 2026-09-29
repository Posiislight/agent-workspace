import { useEffect, useMemo, useState } from "react";
import type { Artifacts } from "../api/types";

type TabId = "plan" | "research" | "diff" | "tests" | "review" | "reply";

export function testsUnverified(t: Artifacts["test_results"]): boolean {
  return !!t && t.passed && (t.failing_output ?? "").startsWith("no tests collected");
}

function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="diff">
      {diff.split("\n").map((line, i) => {
        let cls = "";
        if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")) cls = "d-file";
        else if (line.startsWith("@@")) cls = "d-hunk";
        else if (line.startsWith("+")) cls = "d-add";
        else if (line.startsWith("-")) cls = "d-del";
        return (
          <span key={i} className={`d-line ${cls}`}>
            {line || " "}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}

export default function ArtifactsView({
  artifacts,
  codingReply,
  preferred,
}: {
  artifacts: Artifacts;
  codingReply?: string | null;
  /** Tab to open by default when available (e.g. "review" when the reviewer is stuck). */
  preferred?: TabId;
}) {
  const a = artifacts;
  const unverified = testsUnverified(a.test_results);

  const tabs = useMemo(() => {
    const t: { id: TabId; label: string; badge?: string; tone?: string }[] = [];
    if (a.plan) t.push({ id: "plan", label: "Plan" });
    if (a.research_notes) t.push({ id: "research", label: "Research" });
    if (a.code_diff) {
      const add = a.code_diff.split("\n").filter((l) => l.startsWith("+") && !l.startsWith("+++")).length;
      const del = a.code_diff.split("\n").filter((l) => l.startsWith("-") && !l.startsWith("---")).length;
      t.push({ id: "diff", label: "Diff", badge: `+${add} −${del}` });
    }
    if (a.test_results)
      t.push({
        id: "tests",
        label: "Tests",
        badge: unverified ? "unverified" : a.test_results.passed ? "passed" : "failed",
        tone: unverified ? "warn" : a.test_results.passed ? "ok" : "bad",
      });
    if (a.review_comments?.length)
      t.push({ id: "review", label: "Review", badge: String(a.review_comments.length), tone: "warn" });
    if (codingReply) t.push({ id: "reply", label: "Agent reply" });
    return t;
  }, [a, codingReply, unverified]);

  const [active, setActive] = useState<TabId | null>(null);
  // Follow the pipeline: jump to the preferred tab when it appears, unless the
  // user has picked a tab themselves.
  const [userPicked, setUserPicked] = useState(false);
  useEffect(() => {
    if (userPicked && active && tabs.some((t) => t.id === active)) return;
    const pick =
      (preferred && tabs.find((t) => t.id === preferred)?.id) ?? tabs[tabs.length - 1]?.id ?? null;
    setActive(pick);
  }, [tabs, preferred, userPicked, active]);

  if (tabs.length === 0)
    return (
      <section className="card artifacts">
        <p className="muted">No artifacts yet. They appear here as each stage finishes.</p>
      </section>
    );

  return (
    <section className="card artifacts">
      <div className="tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={active === t.id}
            className={`tab${active === t.id ? " active" : ""}`}
            onClick={() => {
              setActive(t.id);
              setUserPicked(true);
            }}
          >
            {t.label}
            {t.badge && <span className={`tab-badge ${t.tone ?? ""}`}>{t.badge}</span>}
          </button>
        ))}
      </div>
      <div className="tab-panel" role="tabpanel">
        {active === "plan" && <pre className="prose">{a.plan}</pre>}
        {active === "research" && <pre className="prose">{a.research_notes}</pre>}
        {active === "diff" && a.code_diff && <DiffView diff={a.code_diff} />}
        {active === "tests" && a.test_results && (
          <>
            {unverified && (
              <p className="warn-banner">
                The repo has no tests, so this change wasn't actually tested. The tester treats
                "no tests collected" as a pass.
              </p>
            )}
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
          </>
        )}
        {active === "review" && (
          <ol className="review-list">
            {a.review_comments?.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ol>
        )}
        {active === "reply" && <pre className="prose">{codingReply}</pre>}
      </div>
    </section>
  );
}
