from functools import partial

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    coding_agent,
    commit_pr,
    human_approval,
    needs_human,
    reviewer,
    tester,
)
from app.graph.state import TaskState


def build_graph(services, checkpointer=None):
    b = StateGraph(TaskState)
    b.add_node("coding_agent", partial(coding_agent.coding_agent_node, services=services))
    b.add_node("tester", partial(tester.tester_node, services=services))
    b.add_node("reviewer", partial(reviewer.reviewer_node, services=services))
    b.add_node("human_approval", partial(human_approval.human_approval_node, services=services))
    b.add_node("needs_human", partial(needs_human.needs_human_node, services=services))
    b.add_node("commit_pr", partial(commit_pr.commit_pr_node, services=services))
    # One harness run does planning, research and coding: separate planner and
    # researcher runs cost extra Maritime agent quota for text that was only
    # pasted into the coding prompt.
    b.add_edge(START, "coding_agent")
    b.add_edge("needs_human", END)
    b.add_edge("commit_pr", END)
    return b.compile(checkpointer=checkpointer)
