"""Routing turns one TelegramCommand into an IntentDecision (intent_router.py,
pure classification) and then, for deterministic routes, executes it without
ever touching the LLM or the LangGraph supervisor (deterministic.py).
"""
