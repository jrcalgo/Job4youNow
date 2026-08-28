"""Typed boundary models. Nothing in this package imports from elsewhere in
`agent.app` — every other layer imports these, never the reverse (see the
plan's "Recommended dependency direction"). Keeping that one-way means the
models stay a stable, dependency-free contract that routing, the graph,
tools, repositories, and presenters all agree on.
"""
