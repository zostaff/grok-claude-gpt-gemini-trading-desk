"""Adapters: everything that touches the outside world.

Each subpackage implements one port from `trading_desk.ports`. Nothing here is imported
by the domain, and the orchestrator sees only the ports -- which is what makes a recorded
feed or a real executor a composition change rather than an edit to the pipeline.
"""
