async def needs_human_node(state, *, services):
    line = (f"needs_human: retry bounds reached "
            f"(testing={state['retry_counts'].get('testing', 0)}, "
            f"coding={state['retry_counts'].get('coding', 0)})")
    return {"status": "needs_human", "error_log": list(state["error_log"]) + [line]}
