"""In-process TTL/LRU cache for deterministic Aurora reads — see the plan's
"Caching Strategy". Never cache anything needed for exactly-once behavior
(task leases, Telegram offsets, in-progress task state); this package is for
read-mostly, tolerant-of-staleness data only.
"""
