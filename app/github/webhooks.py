import hashlib
import hmac


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def parse_comment(text: str) -> tuple[str, str] | None:
    stripped = (text or "").strip()
    lowered = stripped.lower()
    if lowered.startswith("approve"):
        return "approved", ""
    if lowered.startswith("reject:"):
        return "rejected", stripped[len("reject:"):].strip()
    if lowered.startswith("reject"):
        return "rejected", ""
    return None


def classify_event(event: str, payload: dict) -> dict | None:
    pr_number = None
    if event == "pull_request_review":
        action = payload.get("action")
        review = payload.get("review") or {}
        state = review.get("state")
        if action != "submitted" or state not in ("approved", "changes_requested"):
            return None
        pr_number = (payload.get("pull_request") or {}).get("number")
        decision = "approved" if state == "approved" else "rejected"
        return {"pr_number": pr_number, "decision": decision,
                "feedback": review.get("body") or ""}
    if event == "pull_request":
        if payload.get("action") != "ready_for_review":
            return None
        return {"pr_number": (payload.get("pull_request") or {}).get("number"),
                "decision": "approved", "feedback": ""}
    if event == "issue_comment":
        issue = payload.get("issue") or {}
        if payload.get("action") != "created" or "pull_request" not in issue:
            return None
        comment = payload.get("comment") or {}
        if (comment.get("user") or {}).get("type") == "Bot":
            return None
        parsed = parse_comment(comment.get("body") or "")
        if not parsed:
            return None
        return {"pr_number": issue.get("number"),
                "decision": parsed[0], "feedback": parsed[1]}
    return None

FOLLOWUP_PREFIX = "/aw "
CI_FAILED = ("failure", "timed_out")


def classify_followup(event: str, payload: dict) -> dict | None:
    """Follow-up triggers (phase-4 §7): `/aw ...` PR comments and failed CI checks."""
    if event in ("issue_comment", "pull_request_review_comment"):
        if payload.get("action") != "created":
            return None
        comment = payload.get("comment") or {}
        if (comment.get("user") or {}).get("type") == "Bot":
            return None
        body = (comment.get("body") or "").strip()
        if not body.lower().startswith(FOLLOWUP_PREFIX):
            return None
        instruction = body[len(FOLLOWUP_PREFIX):].strip()
        if not instruction:
            return None
        if event == "issue_comment":
            issue = payload.get("issue") or {}
            if "pull_request" not in issue:
                return None
            pr_number = issue.get("number")
        else:
            pr_number = (payload.get("pull_request") or {}).get("number")
            where = comment.get("path")
            if where:
                line = comment.get("line") or comment.get("original_line")
                instruction += f"\n(Review comment on {where}{f':{line}' if line else ''})"
        return {"kind": "comment", "pr_number": pr_number, "instruction": instruction,
                "source": "pr_comment"}
    if event == "check_run":
        run = payload.get("check_run") or {}
        if payload.get("action") != "completed" or run.get("conclusion") not in CI_FAILED:
            return None
        suite = run.get("check_suite") or {}
        branch = suite.get("head_branch") or ""
        if not branch.startswith("aw/"):
            return None
        return {"kind": "ci", "task_id": branch[len("aw/"):], "sha": run.get("head_sha") or "",
                "name": run.get("name") or "check", "job_id": run.get("id"),
                "app": (run.get("app") or {}).get("slug"),
                "summary": ((run.get("output") or {}).get("summary") or "")[:2000],
                "details_url": run.get("details_url"), "source": "ci"}
    return None
