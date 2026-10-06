from langgraph.graph import END, START, StateGraph

from app.agents.nodes.finance import FinanceNodes, guarded
from app.agents.state import WorkflowState


def next_step(target: str):
    return lambda state: "error" if state.get("error") else target


def base_graph(nodes: FinanceNodes) -> StateGraph:
    graph = StateGraph(WorkflowState)
    graph.add_node("error", nodes.error)
    graph.add_edge("error", END)
    return graph


def chain(graph, nodes, names):
    for name in names:
        graph.add_node(name, guarded(getattr(nodes, name)))
    for source, dest in zip(names, names[1:], strict=False):
        graph.add_conditional_edges(source, next_step(dest), {"error": "error", dest: dest})
    graph.add_conditional_edges(names[-1], next_step(END), {"error": "error", END: END})


def receipt_graph(nodes: FinanceNodes):
    graph = base_graph(nodes)
    chain(graph, nodes, ["ocr", "extract", "validate", "stage", "preview"])
    graph.add_edge(START, "ocr")
    return graph.compile()


def transaction_graph(nodes: FinanceNodes):
    graph = base_graph(nodes)
    graph.add_node("extract", guarded(nodes.extract))
    graph.add_node("unknown", nodes.unknown)
    chain(graph, nodes, ["validate", "stage", "preview"])
    chain(graph, nodes, ["gather", "advise", "render"])
    graph.add_edge(START, "extract")

    def route(state):
        if state.get("error"):
            return "error"
        intent = state["extraction"].intent
        return (
            "validate"
            if intent in {"expense", "income"}
            else "gather"
            if intent in {"report", "advice"}
            else "unknown"
        )

    graph.add_conditional_edges(
        "extract", route, {n: n for n in ("error", "validate", "gather", "unknown")}
    )
    graph.add_edge("unknown", END)
    return graph.compile()


def advisor_graph(nodes: FinanceNodes):
    graph = base_graph(nodes)
    chain(graph, nodes, ["gather", "advise", "render"])
    graph.add_edge(START, "gather")
    return graph.compile()


def visualization_graph(nodes: FinanceNodes):
    graph = base_graph(nodes)
    chain(
        graph,
        nodes,
        ["parse_visualize", "aggregate_visualize", "analyze_visualize", "render_visualize"],
    )
    graph.add_edge(START, "parse_visualize")
    return graph.compile()


def confirmation_graph(nodes: FinanceNodes):
    graph = base_graph(nodes)
    chain(graph, nodes, ["callback"])
    chain(graph, nodes, ["edit", "preview"])
    graph.add_conditional_edges(
        START,
        lambda state: "callback" if state.get("callback_token") else "edit",
        {"callback": "callback", "edit": "edit"},
    )
    return graph.compile()
