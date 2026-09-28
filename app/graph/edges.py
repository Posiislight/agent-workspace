MAX_TESTING_RETRIES = 3
MAX_CODING_RETRIES = 2


def route_after_tester(state) -> str:
    if state["test_results"]["passed"]:
        return "reviewer"
    if state["retry_counts"].get("testing", 0) >= MAX_TESTING_RETRIES:
        return "needs_human"
    return "coding_agent"


def route_start(state) -> str:
    """A follow-up on a finished thread skips planning and goes straight to code."""
    return "coding_agent" if state.get("followup_request") else "planner"


def route_after_researcher(state) -> str:
    return "best_of_n" if len(state.get("candidates") or []) > 1 else "coding_agent"


def route_after_reviewer(state) -> str:
    if state["approval_status"] == "approved":
        return "visual_proof"
    if state["retry_counts"].get("coding", 0) >= MAX_CODING_RETRIES:
        return "needs_human"
    return "coding_agent"


def route_after_human(state) -> str:
    return "commit_pr" if state["approval_status"] == "approved" else "planner"
