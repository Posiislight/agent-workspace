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