"""
JobBot shared AI layer (function-based).

    from ai.provider_result import AIRequest
    from ai.provider_router import execute_ai_task

    outcome = execute_ai_task(AIRequest(...), validator=my_validator)

Providers run only if their USE_* flag in .env is true, in the fixed order
Claude API -> ChatGPT API -> Claude Web -> ChatGPT Web.
See README.md for the architecture.
"""
