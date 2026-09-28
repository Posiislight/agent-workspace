"""Best-of-N (spec phase-4 §9): N harness candidates race in their own persistent
workspaces — the lease pool admits 2 awake at a time — then a judge picks the
winner, whose workspace becomes the task's workspace."""
import asyncio
import json
import re
import time

from langgraph.types import Command

from app.graph.nodes.coding_agent import _brief, build_context, openrouter_loop
from app.graph.nodes.helpers import (
    apply_llm_cost,
    budget_stop,
    chat_with_retry,
    check_budget,
    emit,
)
from app.graph.state import TestResult
from app.harness import CLI_HARNESSES, run_cli_harness

JUDGE_PROMPT = """You are the judge in a best-of-N coding contest. Several candidate diffs
implement the same task; all of them pass the test suite. Pick the one a senior reviewer
would merge: correct, minimal, idiomatic for this codebase, no unrelated changes.

Reply with ONLY: {"winner": <candidate index>, "scores": {"<index>": <0-10>, ...},
"rationale": "<one or two sentences>"}"""


def candidate_slots(candidates: list[str]) -> list[tuple[str, int]]:
    """Duplicate harnesses get distinct VMs: codex, codex, dsh -> codex/0, codex/1, dsh/0."""
    seen: dict[str, int] = {}
    out = []
    for h in candidates:
        out.append((h, seen.get(h, 0)))
        seen[h] = seen.get(h, 0) + 1
    return out


def _workspace_for(services, state, harness: str, slot: int):
    reg = services.workspaces
    if reg is not None and hasattr(reg, "for_candidate"):
        return reg.for_candidate(state, harness, slot)
    return services.sandbox_factory({**state, "harness": harness, "workspace_slot": slot})


async def best_of_n_node(state, *, services):
    cands = list(state.get("candidates") or [])
    await emit(services, state, "best_of_n", "node_started", {"candidates": cands})
    budget, stop = await check_budget(state, services, "best_of_n")
    if stop:
        return budget_stop(state, "best_of_n")
    state = {**state, **budget}

    # Free the planner/researcher workspace's lease so candidates can use both slots.
    try:
        await services.sandbox_factory(state).sleep()
    except Exception:  # noqa: BLE001, S110 - best-effort
        pass

    context, consumed = build_context(state)
    brief_state = {**state, **consumed}
    rows = [{"index": i, "harness": h, "slot": k, "status": "queued", "tests_passed": None,
             "files": 0, "insertions": 0, "deletions": 0, "seconds": 0.0,
             "vm_seconds": 0.0, "score": None, "winner": False}
            for i, (h, k) in enumerate(candidate_slots(cands))]
    diffs: dict[int, str] = {}
    llm_results: list = []

    async def publish(row):
        await emit(services, state, "best_of_n", "candidate_update", dict(row))

    for row in rows:
        await publish(row)

    async def run(row):
        ws = _workspace_for(services, state, row["harness"], row["slot"])
        t0 = time.monotonic()
        try:
            await ws.ensure()          # waits here while both VM slots are busy
            row["status"] = "coding"
            await publish(row)
            if row["harness"] in CLI_HARNESSES:
                res, _cmd = await run_cli_harness(
                    services.settings, ws, row["harness"], _brief(brief_state, context),
                    timeout=services.settings.sandbox_run_timeout_seconds)
                ok, err = res.exit_code == 0, res.combined[-400:]
            else:
                outcome = await openrouter_loop(brief_state, services, ws, context,
                                                node="best_of_n")
                llm_results.extend(outcome.results)
                ok, err = outcome.ok, outcome.error
            if not ok:
                row.update(status="failed", error=err, tests_passed=False)
                return
            diff = await ws.diff()
            if not diff.strip():
                row.update(status="failed", error="empty diff", tests_passed=False)
                return
            diffs[row["index"]] = diff
            row["status"] = "testing"
            await publish(row)
            cmd = state.get("test_command") or services.settings.test_command
            tr = await ws.run_long(cmd, timeout=services.settings.sandbox_run_timeout_seconds)
            row["tests_passed"] = tr.exit_code == 0
            row["test_output"] = tr.combined[-2000:]
            stat = getattr(ws, "shortstat", None)
            if stat is not None:
                row.update(await stat())
            row["status"] = "passed" if row["tests_passed"] else "tests_failed"
        except Exception as e:  # noqa: BLE001 - one candidate crashing must not sink the race
            row.update(status="error", error=repr(e)[:400], tests_passed=False)
        finally:
            row["seconds"] = round(time.monotonic() - t0, 1)
            take = getattr(ws, "take_vm_seconds", None)
            try:
                await ws.sleep()       # frees the slot for the next candidate
            except Exception:  # noqa: BLE001, S110 - best-effort
                pass
            if take is not None:
                row["vm_seconds"] = round(take(), 1)
            await publish(row)

    await asyncio.gather(*(run(r) for r in rows))

    updates: dict = {**budget, **consumed, "status": "reviewing"}
    for res in llm_results:
        updates = await apply_llm_cost(updates, {**state, **updates}, services, res,
                                       "best_of_n")
    vm_secs = sum(r["vm_seconds"] for r in rows)
    rate = services.settings.vm_cost_per_minute
    updates["vm_seconds"] = round((state.get("vm_seconds") or 0.0) + vm_secs, 3)
    updates["vm_cost"] = round((state.get("vm_cost") or 0.0) + vm_secs / 60 * rate, 6)
    updates["cost_so_far"] = round((updates.get("cost_so_far", state.get("cost_so_far"))
                                    or 0.0) + vm_secs / 60 * rate, 6)

    passing = [r for r in rows if r["tests_passed"]]
    if not passing:
        err = list(state["error_log"]) + [
            "best_of_n: no candidate passed the tests: "
            + "; ".join(f"#{r['index']} {r['harness']}: {r['status']}" for r in rows)]
        updates["candidate_results"] = rows
        return Command(update={**updates, "error_log": err, "status": "needs_human"},
                       goto="needs_human")

    winner, rationale = await _judge(services, state, passing, diffs, updates)
    updates = winner[1]
    win = winner[0]
    for r in rows:
        r["winner"] = r["index"] == win["index"]
    updates.update({
        "candidate_results": rows,
        "harness": win["harness"],
        "workspace_slot": win["slot"],
        "code_diff": diffs[win["index"]],
        "test_results": TestResult(passed=True,
                                   failing_output=win.get("test_output", "")).to_dict(),
    })
    await emit(services, state, "best_of_n", "node_completed",
               {"winner": win["index"], "harness": win["harness"], "rationale": rationale,
                "scoreboard": rows})
    return Command(update=updates, goto="reviewer")


async def _judge(services, state, passing, diffs, updates):
    """Returns ((winning row, updates), rationale)."""
    if len(passing) == 1:
        passing[0]["score"] = 10
        return (passing[0], updates), "only passing candidate"
    listing = "\n\n".join(f"### Candidate {r['index']} ({r['harness']})\n{diffs[r['index']][:6000]}"
                          for r in passing)
    messages = [{"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content":
                    f"TASK:\n{state['task_description']}\n\nPLAN:\n{state.get('plan') or ''}"
                    f"\n\nCANDIDATES:\n{listing}"}]
    rationale = ""
    pick = None
    try:
        result = await chat_with_retry(services, state, "reviewer", messages)
        updates = await apply_llm_cost(updates, {**state, **updates}, services, result,
                                       "best_of_n")
        m = re.search(r"\{.*\}", result.text, re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
        scores = {int(k): v for k, v in (d.get("scores") or {}).items()}
        for r in passing:
            r["score"] = scores.get(r["index"])
        pick = next((r for r in passing if r["index"] == int(d.get("winner", -1))), None)
        rationale = str(d.get("rationale") or "")
    except Exception as e:  # noqa: BLE001 - judge failure falls back to a heuristic
        rationale = f"judge unavailable ({e!r}); picked the smallest passing diff"
    if pick is None:
        pick = min(passing, key=lambda r: (r["insertions"] + r["deletions"], r["index"]))
        rationale = rationale or "judge gave no valid winner; picked the smallest passing diff"
    return (pick, updates), rationale
