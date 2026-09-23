from app.graph.nodes.helpers import apply_llm_cost, emit, model_for

RESEARCHER_PROMPT = """You are the Researcher agent. You are given an implementation plan and must
gather everything the Coding Agent will need to execute it correctly: exact
library APIs and versions in use, precedent from elsewhere in this codebase,
and any external documentation the plan depends on. Cite where each fact came
from (file path or URL). Do not write implementation code - summarize findings
as notes the Coding Agent will consult.

First, list up to 3 documentation URLs worth consulting, one per line, each
prefixed with 'URL: '. Then, after receiving the fetched excerpts, write the
research notes with citations."""


async def researcher_node(state, *, services):
    await emit(services, state, "researcher", "node_started", {})
    comp = await services.computers.ensure(state["task_id"])
    updates = {"computer_id": comp.computer_id, "status": "researching"}
    viewer = await services.computers.viewer_link(comp.computer_id, mode="watch")
    await emit(services, state, "researcher", "tool_call", {"viewer_url": viewer})

    model = model_for(services, state, "researcher")
    base_messages = [
        {"role": "system", "content": RESEARCHER_PROMPT},
        {"role": "user", "content": f"PLAN:\n{state.get('plan') or ''}\n\nREPO: {state['repo']}"},
    ]
    first = await services.llm.chat(model, base_messages)
    urls = [u for u in _urls(first.text)][:3]

    excerpts = []
    for url in urls:
        await services.computers.open_url(comp.computer_id, url)
        await emit(services, state, "researcher", "tool_call",
                   {"url": url, "action": "browser_open"})
        excerpts.append(f"## {url}\n{await _fetch_excerpt(state, services, url)}")

    followup = "TOOL RESULTS:\n" + ("\n\n".join(excerpts) if excerpts else "No URLs found.")
    second = await services.llm.chat(model, base_messages + [{"role": "user", "content": followup}])
    notes = ("### Sources consulted (live, in the headful browser)\n"
             + "\n".join(f"- {u}" for u in urls)
             + "\n\n" + second.text)
    updates.update({"research_notes": notes, "status": "coding"})
    updates = await apply_llm_cost(updates, state, services, first, "researcher")
    updates = await apply_llm_cost(updates, {**state, **updates}, services, second, "researcher")
    await emit(services, state, "researcher", "node_completed", {"urls": urls})
    return updates


def _urls(text: str) -> list[str]:
    import re
    return re.findall(r"URL:\s*(\S+)", text)


async def _fetch_excerpt(state, services, url: str) -> str:
    try:
        sb = services.sandbox_factory(state)
        await sb.ensure()
        res = await sb.exec(
            f"curl -sL --max-time 20 {url} | sed -e 's/<[^>]*>/ /g' | tr -s ' \\n' ' ' | head -c 8000")
        return res.stdout
    except Exception as e:  # noqa: BLE001 - research must not crash the run
        return f"(fetch failed: {e})"