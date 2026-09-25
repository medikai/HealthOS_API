"""Dev-only ZeptoMail diagnostic.

Sends exactly one synthetic message to an explicit test recipient and prints
only accepted/error metadata. It is never run on startup and there is no public
general-send endpoint.

Usage (from the repository root)::

    venv/bin/python -m src.app.domains.communication.email.diagnostic \
        --to <verified-test-inbox> --yes
"""

import argparse
import asyncio
import sys

from ....core.config import EnvironmentOption, settings
from ..shared.errors import CommunicationError
from .providers.zeptomail import ZeptoMailClient
from .schemas import EmailProviderRequest
from .templates import render_template


def build_diagnostic_request(
    *,
    to_email: str,
    to_name: str,
    note: str,
    from_email: str,
    from_name: str | None,
) -> EmailProviderRequest:
    rendered = render_template("email_test", {"note": note})
    return EmailProviderRequest(
        to_email=to_email,
        to_name=to_name,
        from_email=from_email,
        from_name=from_name,
        subject=rendered.subject,
        html_body=rendered.html_body,
        text_body=rendered.text_body,
        provider="zeptomail",
    )


def _client() -> ZeptoMailClient:
    token = settings.ZEPTOMAIL_SEND_TOKEN
    return ZeptoMailClient(
        base_url=settings.ZEPTOMAIL_API_BASE_URL,
        token=token.get_secret_value() if token else None,
        timeout=settings.EMAIL_TIMEOUT_SECONDS,
    )


async def _run(args: argparse.Namespace) -> int:
    if settings.ENVIRONMENT == EnvironmentOption.PRODUCTION:
        print("Refusing to send a diagnostic from production.", file=sys.stderr)
        return 2
    client = _client()
    if not client.configured:
        print(
            "ZeptoMail is not configured (ZEPTOMAIL_SEND_TOKEN missing).",
            file=sys.stderr,
        )
        return 3
    request = build_diagnostic_request(
        to_email=args.to,
        to_name=args.to_name,
        note=args.note,
        from_email=settings.EMAIL_FROM_ADDRESS,
        from_name=settings.EMAIL_FROM_NAME,
    )
    try:
        result = await client.send(request)
    except CommunicationError as exc:
        reason = getattr(exc, "reason", "")
        print(f"diagnostic failed: {exc.code}:{reason}", file=sys.stderr)
        return 4
    print(
        "diagnostic accepted: "
        f"provider={result.provider} status_code={result.status_code} "
        f"request_id={result.provider_request_id or 'n/a'}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HealthOS ZeptoMail diagnostic (dev only)")
    parser.add_argument("--to", required=True, help="explicit verified test recipient")
    parser.add_argument("--to-name", default="Test user")
    parser.add_argument("--note", default="HealthOS email diagnostic.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm sending exactly one synthetic message",
    )
    args = parser.parse_args(argv)
    if not args.yes:
        print("Refusing to send without --yes.", file=sys.stderr)
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
