"""Keeps one bad tick from ending an unattended run."""


def guarded_tick(node, session, on_give_up, running=None):
    """Run node._tick_core(); an error inside it must not kill an unattended run. The error is
    logged (once per distinct message), the tick is retried, and only after it has kept failing
    for 20 seconds does `on_give_up()` decide what the node reports."""
    import traceback
    try:
        state = node._tick_core()
        node._errors_in_a_row = 0
        return state
    except Exception as e:
        import time
        n = node.__dict__.get("_errors_in_a_row", 0) + 1
        node._errors_in_a_row = n
        if n == 1:
            node._errors_since = time.time()
        seen = node.__dict__.setdefault("_errors_seen", set())
        if repr(e) not in seen:
            seen.add(repr(e))
            try:
                session.log.event("error", where=type(node).__name__, error=repr(e), trace=traceback.format_exc()[-1500:])
            except Exception:
                pass
        if n >= 60 and time.time() - node.__dict__.get("_errors_since", 0.0) >= 20.0:
            node._errors_in_a_row = 0
            return on_give_up()
        if running is None:
            from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
            running = BehaviorTree.NodeState.RUNNING
        return running
