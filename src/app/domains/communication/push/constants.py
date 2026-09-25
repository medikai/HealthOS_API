"""Push constants (generic, no clinical content)."""

PUSH_CHANNEL = "push"
PUSH_PROVIDER = "fcm"

# Generic OS payload text only; never names, MRNs or chat bodies.
GENERIC_TITLE = "HealthOS"
GENERIC_BODY = "Open HealthOS to view the update"
DEEP_LINK = "/notifications"

PUSH_PRIORITIES = ("P1", "P2")  # P3 stays off by default
MAX_TOKEN_LENGTH = 512
MAX_INSTALLATION_ID_LENGTH = 128
DISPLAY_STRATEGY = "service_worker_data_only"
