"""Communication domain.

Chat, notifications, email, push and realtime share one durable delivery
foundation (``delivery``) but keep separate services; shared code lives in
``shared`` (provider ports, event envelope, policy) and ``utils`` (pure
helpers). Routers are composed by ``router`` and mounted at
``/api/v1/communication``.
"""
