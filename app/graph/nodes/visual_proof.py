"""Visual proof (spec phase-4 §8): before/after screenshots of the running app,
taken on the Maritime computer, stored for the UI and embedded in the PR."""
import base64
import json
import shlex

from app.github.push import branch_for, push_branch
from app.graph.nodes.helpers import apply_vm_cost, budget_stop, check_budget, emit

PROOF_TTL_SECONDS = 30 * 24 * 3600


async def visual_proof_node(state, *, services):
    await emit(services, state, "visual_proof", "node_started", {})
    preview = await resolve_preview(state, services)
    if not preview:
        await emit(services, state, "visual_proof", "node_completed",
                   {"skipped": "no preview config (task preview_command or .aw.json)"})
        return {"visual_proof": None}
    budget, stop = await check_budget(state, services, "visual_proof")
    if stop:
        return budget_stop(state, "visual_proof")
    state = {**state, **budget}

    sb = services.sandbox_factory(state)
    task_id = state["task_id"]
    proof: dict = {"preview": preview, "images": {}}
    updates: dict = {**budget}
    pool = services.pool
    comp_id = None
    try:
        # The computer checks the branch out from GitHub, so publish it first.
        await push_branch(sb, state["repo"], branch_for(task_id), services.settings.github_pat)
        if pool is not None:
            async def _queued():
                await emit(services, state, "visual_proof", "tool_call",
                           {"action": "queued_for_computer"})
            await pool.acquire_computer(task_id, on_wait=_queued)
        comp = await services.computers.ensure(task_id)
        comp_id = comp.computer_id
        for label, ref in (("before", state["base_branch"]), ("after", branch_for(task_id))):
            png, info = await capture(services, comp_id, state, preview, ref)
            proof[label] = info
            if png:
                await _store(services, task_id, label, png)
                proof["images"][label] = f"/tasks/{task_id}/proof/{label}.png"
                await emit(services, state, "visual_proof", "tool_call",
                           {"proof": label, "url": proof["images"][label], "ref": ref})
        if proof["images"].keys() >= {"before", "after"}:
            proof["github"] = await _publish_to_github(services, state, task_id)
    except Exception as e:  # noqa: BLE001 - proof is additive; it never blocks the PR
        msg = str(e).replace(services.settings.github_pat or "\0", "***")[:500]
        proof["error"] = msg
        updates["error_log"] = list(state.get("error_log") or []) + [f"visual_proof: {msg}"]
    finally:
        if comp_id is not None:
            try:
                await services.computers.sleep(comp_id)
            except Exception:  # noqa: BLE001, S110 - best-effort
                pass
        if pool is not None:
            await pool.release_computer(task_id)
    updates["visual_proof"] = proof
    updates = await apply_vm_cost(updates, state, services, "visual_proof", sandbox=sb)
    await emit(services, state, "visual_proof", "node_completed",
               {"images": proof["images"], "error": proof.get("error")})
    return updates


async def resolve_preview(state, services) -> dict | None:
    p = state.get("preview") or {}
    if p.get("command"):
        return _normalize(p)
    try:
        sb = services.sandbox_factory(state)
        res = await sb.exec("cat /data/workspace/.aw.json 2>/dev/null || true")
        cfg = json.loads(res.stdout) if res.stdout.strip() else {}
    except (ValueError, TypeError):
        return None
    except Exception:  # noqa: BLE001 - no config means no proof
        return None
    cfg = cfg.get("preview", cfg) if isinstance(cfg, dict) else {}
    return _normalize(cfg) if cfg.get("command") else None


def _normalize(p: dict) -> dict:
    return {"command": str(p["command"]), "port": int(p.get("port") or 8000),
            "path": str(p.get("path") or "/"), "setup": p.get("setup")}


def prepare_script(repo: str, pat: str, ref: str, setup: str | None) -> str:
    d = f"$HOME/aw-proof/{repo.replace('/', '__')}"
    url = shlex.quote(f"https://x-access-token:{pat}@github.com/{repo}.git")
    default_setup = ("if [ -f requirements.txt ]; then [ -d .venv ] || python3 -m venv .venv; "
                     ".venv/bin/pip install -q -r requirements.txt; fi")
    return (
        "set -e\n"
        f"D={d}\n"
        f"if [ ! -d \"$D/.git\" ]; then mkdir -p \"$D\" && git clone -q {url} \"$D\"; fi\n"
        "cd \"$D\"\n"
        f"git fetch -q {url} {shlex.quote(f'+refs/heads/{ref}:refs/remotes/origin/{ref}')}\n"
        f"git checkout -q -f {shlex.quote(f'origin/{ref}')}\n"
        "git clean -fdq -e .venv\n"
        f"{setup or default_setup}\n"
        "echo PREPARED\n"
    )


def start_script(repo: str, command: str, port: int) -> str:
    d = f"$HOME/aw-proof/{repo.replace('/', '__')}"
    inner = shlex.quote(f"source .venv/bin/activate 2>/dev/null; exec {command}")
    return (
        f"cd {d}\n"
        "[ -f /tmp/aw-preview.pid ] && kill -- -$(cat /tmp/aw-preview.pid) 2>/dev/null || true\n"
        "pkill -f chromium 2>/dev/null || true\n"
        f"setsid nohup bash -c {inner} > /tmp/aw-preview.log 2>&1 < /dev/null &\n"
        "echo $! > /tmp/aw-preview.pid\n"
        f"for i in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:{port}/ "
        "&& echo UP && exit 0; sleep 1; done\n"
        "echo DOWN; tail -n 30 /tmp/aw-preview.log; exit 1\n"
    )


STOP_SCRIPT = ("[ -f /tmp/aw-preview.pid ] && kill -- -$(cat /tmp/aw-preview.pid) "
               "2>/dev/null; rm -f /tmp/aw-preview.pid; true")


async def capture(services, comp_id: str, state, preview: dict, ref: str):
    """Check out `ref` on the computer, run the app, screenshot it, stop it."""
    c = services.computers
    pat = services.settings.github_pat
    repo = state["repo"]
    prep = await c.shell(comp_id, prepare_script(repo, pat, ref, preview.get("setup")),
                         timeout_s=240)
    if prep.exit_code != 0:
        raise RuntimeError(f"preview prepare failed on {ref}: {prep.combined[-600:]}")
    try:
        up = await c.shell(comp_id, start_script(repo, preview["command"], preview["port"]),
                           timeout_s=60)
        if up.exit_code != 0:
            raise RuntimeError(f"app did not start on {ref}: {up.combined[-600:]}")
        url = f"http://127.0.0.1:{preview['port']}{preview['path']}"
        await c.open_url(comp_id, url)
        await c.act(comp_id, {"action": "wait", "duration": 2})
        png, frame = await c.screenshot(comp_id)
        return png, {"ref": ref, "url": url, "frame_id": frame}
    finally:
        await c.shell(comp_id, STOP_SCRIPT, timeout_s=15)


async def _store(services, task_id: str, label: str, png: bytes):
    if services.redis is not None:
        await services.redis.set(f"aw:{task_id}:proof:{label}",
                                 base64.b64encode(png).decode(), ex=PROOF_TTL_SECONDS)


async def _publish_to_github(services, state, task_id: str) -> dict | None:
    gh = services.github
    if gh is None or not hasattr(gh, "put_file") or services.redis is None:
        return None
    branch = services.settings.proof_artifacts_branch
    out = {}
    for label in ("before", "after"):
        b64 = await services.redis.get(f"aw:{task_id}:proof:{label}")
        if not b64:
            return None
        path = f"proof/{task_id}/{label}.png"
        await gh.put_file(state["repo"], path, b64, branch=branch,
                          message=f"aw: visual proof ({label}) for task {task_id}")
        out[label] = f"https://github.com/{state['repo']}/blob/{branch}/{path}?raw=true"
    return out
