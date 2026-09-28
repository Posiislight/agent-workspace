from app.graph.edges import (
    MAX_CODING_RETRIES,
    MAX_TESTING_RETRIES,
    route_after_human,
    route_after_reviewer,
    route_after_tester,
)
from app.graph.state import initial_state


def base_state(**kw):
    st = dict(initial_state("t1", "desc", "org/repo", "main", "pytest -q", {}))
    st.update(kw)
    return st


def test_bounds():
    assert MAX_TESTING_RETRIES == 3
    assert MAX_CODING_RETRIES == 2


def test_initial_state_defaults():
    st = initial_state("t1", "d", "org/repo", "main", "", {})
    assert st["status"] == "planning"
    assert st["retry_counts"] == {"testing": 0, "coding": 0}
    assert st["approval_status"] == "pending"
    assert st["error_log"] == [] and st["cost_so_far"] == 0.0


def test_tester_pass_routes_to_reviewer():
    assert route_after_tester(base_state(test_results={"passed": True})) == "reviewer"


def test_tester_fail_routes_to_coding():
    st = base_state(test_results={"passed": False}, retry_counts={"testing": 0})
    assert route_after_tester(st) == "coding_agent"


def test_tester_bound_exceeded_routes_to_needs_human():
    st = base_state(test_results={"passed": False}, retry_counts={"testing": MAX_TESTING_RETRIES})
    assert route_after_tester(st) == "needs_human"


def test_reviewer_approved_routes_to_human():
    st = base_state(approval_status="approved", retry_counts={"coding": 0})
    assert route_after_reviewer(st) == "visual_proof"


def test_reviewer_rejected_routes_to_coding():
    st = base_state(approval_status="needs_changes", review_comments=["x"], retry_counts={"coding": 1})
    assert route_after_reviewer(st) == "coding_agent"


def test_reviewer_bound_exceeded_routes_to_needs_human():
    st = base_state(approval_status="needs_changes", review_comments=["x"],
                    retry_counts={"coding": MAX_CODING_RETRIES})
    assert route_after_reviewer(st) == "needs_human"


def test_human_routes():
    assert route_after_human(base_state(approval_status="approved")) == "commit_pr"
    assert route_after_human(base_state(approval_status="rejected")) == "planner"
