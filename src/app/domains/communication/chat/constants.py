"""Chat constants and realtime event types (pure)."""

CONVERSATION_DIRECT = "direct"
CONVERSATION_TEAM = "team"
CONVERSATION_KINDS = (CONVERSATION_DIRECT, CONVERSATION_TEAM)

MESSAGE_TEXT = "text"
MESSAGE_KIND_HANDOVER = "handover"

EVENT_MESSAGE_CREATED = "message.created"
EVENT_CONVERSATION_UPDATED = "conversation.updated"
EVENT_CONVERSATION_READ = "conversation.read"

# Realtime delivery channel/payload discriminator.
REALTIME_KIND_NOTIFICATION = "notification"
REALTIME_KIND_CONVERSATION = "conversation"

# Catch-up marker for clients that reconnect.
CATCHUP_EVENT = "conversation.catchup"
