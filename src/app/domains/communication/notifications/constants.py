"""Pure notification catalog: priorities, categories and workflow event types.

No P0 in this release. P1 = action soon, P2 = targeted update, P3 = information.
"""

PRIORITY_P1 = "P1"
PRIORITY_P2 = "P2"
PRIORITY_P3 = "P3"
PRIORITIES = (PRIORITY_P1, PRIORITY_P2, PRIORITY_P3)

KIND_TASK = "task"
KIND_UPDATE = "update"
KIND_INFO = "info"

CATEGORY_QUEUE = "queue"
CATEGORY_APPOINTMENTS = "appointments"
CATEGORY_CONSULTATION = "consultation"
CATEGORY_MESSAGES = "messages"
CATEGORY_TEAM = "team"

# code -> (label, browser_default, locked, default_priority)
CATEGORY_CATALOG: dict[str, dict] = {
    CATEGORY_QUEUE: {"label": "Queue & tokens", "browser_default": True, "locked": True, "priority": PRIORITY_P1},
    CATEGORY_APPOINTMENTS: {"label": "Appointments", "browser_default": True, "locked": True, "priority": PRIORITY_P2},
    CATEGORY_CONSULTATION: {"label": "Consultation", "browser_default": True, "locked": False, "priority": PRIORITY_P2},
    CATEGORY_MESSAGES: {"label": "Messages", "browser_default": True, "locked": False, "priority": PRIORITY_P2},
    CATEGORY_TEAM: {"label": "Team updates", "browser_default": False, "locked": False, "priority": PRIORITY_P3},
}

# Committed workflow transitions wired in BE03.
EVENT_TOKEN_ASSIGNED = "queue.token_assigned"
EVENT_QUEUE_READY = "queue.ready"
EVENT_APPOINTMENT_RESCHEDULED = "appointment.same_day_rescheduled"
EVENT_CONSULTATION_COMPLETED = "consultation.completed"
EVENT_CHAT_DIRECT = "chat.message.direct"
EVENT_CHAT_TEAM = "chat.message.team"

CHAT_EVENT_TYPES = (EVENT_CHAT_DIRECT, EVENT_CHAT_TEAM)

# Priority/category/kind per event type (kept here so it is one auditable table).
EVENT_CATALOG: dict[str, dict] = {
    EVENT_TOKEN_ASSIGNED: {"category": CATEGORY_QUEUE, "priority": PRIORITY_P2, "kind": KIND_UPDATE},
    EVENT_QUEUE_READY: {"category": CATEGORY_QUEUE, "priority": PRIORITY_P1, "kind": KIND_TASK},
    EVENT_APPOINTMENT_RESCHEDULED: {"category": CATEGORY_APPOINTMENTS, "priority": PRIORITY_P2, "kind": KIND_UPDATE},
    EVENT_CONSULTATION_COMPLETED: {"category": CATEGORY_CONSULTATION, "priority": PRIORITY_P2, "kind": KIND_UPDATE},
    EVENT_CHAT_DIRECT: {"category": CATEGORY_MESSAGES, "priority": PRIORITY_P2, "kind": KIND_UPDATE},
    EVENT_CHAT_TEAM: {"category": CATEGORY_MESSAGES, "priority": PRIORITY_P3, "kind": KIND_UPDATE},
}

NOTIFICATION_TTL_MINUTES = 24 * 60
NOTIFICATION_COALESCE_SECONDS = 120
REPLAY_WINDOW_DAYS = 7

# Categories whose browser alerts are never user-disabled.
LOCKED_CATEGORIES = frozenset(
    code for code, meta in CATEGORY_CATALOG.items() if meta["locked"]
)
