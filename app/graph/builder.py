"""图装配。节点统一经 AuditLogger.wrap 包装，人工审批节点通过 interrupt 挂起，checkpointer 保存挂起状态。"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.graph.nodes import RFQNodes
from app.graph.state import RFQState
from app.services import Services

PIPELINE = ("intake", "parse", "memory_gate", "normalize", "price", "risk", "approval")


def build_graph(svc: Services, checkpointer=None):
    n = RFQNodes(svc)
    g = StateGraph(RFQState)
    for name in (*PIPELINE, "commit", "render", "reject"):
        g.add_node(name, svc.audit.wrap(name, getattr(n, name)))
    g.add_edge(START, PIPELINE[0])
    for a, b in zip(PIPELINE, PIPELINE[1:]):
        g.add_edge(a, b)
    g.add_conditional_edges("approval", lambda s: "commit" if s["decision"]["action"] == "approve" else "reject",
                            ["commit", "reject"])
    g.add_edge("commit", "render")
    g.add_edge("render", END)
    g.add_edge("reject", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())
