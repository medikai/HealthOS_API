"""Pure channel/client-id/capability builders for realtime authorization.

Clients never construct channel names from route params; the server derives
exact channels from trusted tenant/staff membership and channel generation.
"""

import re
from uuid import UUID

_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def validate_namespace(namespace: str) -> str:
    if not _NAMESPACE_RE.match(namespace):
        raise ValueError("Invalid realtime channel namespace.")
    return namespace


def own_user_channel(
    namespace: str, organization_id: UUID | str, staff_member_id: UUID | str, generation: int
) -> str:
    """Private per-tenant per-staff channel for one authorization generation."""
    return f"{namespace}:t:{organization_id}:u:{staff_member_id}:g:{int(generation)}"


def facility_presence_channel(
    namespace: str, organization_id: UUID | str, facility_id: UUID | str
) -> str:
    """Explicitly authorized facility presence channel."""
    return f"{namespace}:presence:t:{organization_id}:f:{facility_id}"


def build_client_id(organization_id: UUID | str, staff_member_id: UUID | str) -> str:
    """Bind clientId to the stable authenticated tenant/staff identity."""
    return f"staff:{organization_id}:{staff_member_id}"


def build_capabilities(
    own_channel: str, presence_channels: list[str]
) -> dict[str, list[str]]:
    """Browser capabilities: own-user subscribe only, plus authorized presence.

    No publish capability is ever granted to the browser.
    """
    capabilities: dict[str, list[str]] = {own_channel: ["subscribe"]}
    for channel in presence_channels:
        if channel in capabilities:
            continue
        capabilities[channel] = ["subscribe", "presence"]
    return capabilities
