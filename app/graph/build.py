from functools import partial

from langgraph.graph import END, START, StateGraph

from app.graph.edges import route_after_researcher, route_start
from app.graph.nodes import (
    best_of_n,
    coding_agent,
    commit_pr,
    human_approval,
    needs_human,
    planner,
    publish_pr,
    researcher,
    reviewer,
    tester,
    visual_proof,
)
from app.graph.state import TaskState


def build_graph(services, checkpointer=None):
    b = StateGraph(TaskState)
    b.add_node("planner", partial(planner.planner_node, services=services))
    b.add_node("researcher", partial(researcher.researcher_node, services=services))
    b.add_node("coding_agent", partial(coding_agent.coding_agent_node, services=services))
    b.add_node("tester", partial(tester.tester_node, services=services))
    b.add_node("reviewer", partial(reviewer.reviewer_node, services=services))
    b.add_node("human_approval", partial(human_approval.human_approval_node, services=services))
    b.add_node("needs_human", partial(needs_human.needs_human_node, services=services))
    b.add_node("commit_pr", partial(commit_pr.commit_pr_node, services=services))
    b.add_node("best_of_n", partial(best_of_n.best_of_n_node, services=services))
    b.add_node("visual_proof", partial(visual_proof.visual_proof_node, services=services))
    b.add_node("publish_pr", partial(publish_pr.publish_pr_node, services=services))
    b.add_conditional_edges(START, route_start, ["planner", "coding_agent"])
    b.add_edge("planner", "researcher")
    b.add_conditional_edges("researcher", route_after_researcher,
                            ["coding_agent", "best_of_n"])
    b.add_edge("visual_proof", "publish_pr")
    b.add_edge("publish_pr", "human_approval")
    b.add_edge("needs_human", END)
    b.add_edge("commit_pr", END)
    return b.compile(checkpointer=checkpointer)
