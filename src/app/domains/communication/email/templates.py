"""Approved email templates with explicit escaping.

Only templates registered here can be sent. Interpolated values are
HTML-escaped for the HTML body, emitted verbatim in the text body, and
newline/header-injection is rejected in subjects/names.
"""

from dataclasses import dataclass
from html import escape as html_escape
from typing import Any

from .schemas import reject_header_injection


class EmailTemplateError(ValueError):
    code = "EMAIL_TEMPLATE_ERROR"


class UnsupportedTemplate(EmailTemplateError):
    code = "UNSUPPORTED_TEMPLATE"


class InvalidTemplateContext(EmailTemplateError):
    code = "INVALID_TEMPLATE_CONTEXT"


@dataclass(frozen=True)
class RenderedTemplate:
    subject: str
    html_body: str
    text_body: str


@dataclass(frozen=True)
class TemplateSpec:
    code: str
    allowed_fields: frozenset[str]
    required_fields: frozenset[str]
    secret_fields: frozenset[str]
    subject_template: str
    html_template: str
    text_template: str

    def render(self, context: dict[str, Any]) -> RenderedTemplate:
        values = {field: _stringify(context.get(field, "")) for field in self.allowed_fields}
        subject = reject_header_injection(self.subject_template.format(**values))
        html_body = self.html_template.format(
            **{field: html_escape(value) for field, value in values.items()}
        )
        text_body = self.text_template.format(**values)
        return RenderedTemplate(subject=subject, html_body=html_body, text_body=text_body)


def _stringify(value: Any) -> str:
    return "" if value is None else str(value)


TEMPLATES: dict[str, TemplateSpec] = {
    "email_test": TemplateSpec(
        code="email_test",
        allowed_fields=frozenset({"note"}),
        required_fields=frozenset({"note"}),
        secret_fields=frozenset(),
        subject_template="HealthOS email test",
        html_template="<p>HealthOS email test.</p><p>{note}</p>",
        text_template="HealthOS email test.\n{note}\n",
    ),
    "staff_invitation": TemplateSpec(
        code="staff_invitation",
        allowed_fields=frozenset(
            {"recipient_name", "organization_name", "role_label", "invite_url"}
        ),
        required_fields=frozenset({"organization_name", "invite_url"}),
        secret_fields=frozenset({"invite_url"}),
        subject_template="You have been invited to {organization_name} on HealthOS",
        html_template=(
            "<p>Hello {recipient_name},</p>"
            "<p>{organization_name} has invited you to join HealthOS as {role_label}.</p>"
            '<p><a href="{invite_url}">Accept your invitation</a></p>'
            "<p>This link expires soon. If you did not expect it, ignore this email.</p>"
        ),
        text_template=(
            "Hello {recipient_name},\n\n"
            "{organization_name} has invited you to join HealthOS as {role_label}.\n"
            "Accept your invitation: {invite_url}\n\n"
            "This link expires soon. If you did not expect it, ignore this email.\n"
        ),
    ),
    "password_changed": TemplateSpec(
        code="password_changed",
        allowed_fields=frozenset({"recipient_name"}),
        required_fields=frozenset(),
        secret_fields=frozenset(),
        subject_template="Your HealthOS password was changed",
        html_template=(
            "<p>Hello {recipient_name},</p>"
            "<p>Your HealthOS password was changed. If this was not you, contact your administrator immediately.</p>"
        ),
        text_template=(
            "Hello {recipient_name},\n\n"
            "Your HealthOS password was changed. "
            "If this was not you, contact your administrator immediately.\n"
        ),
    ),
    "password_reset_code": TemplateSpec(
        code="password_reset_code",
        allowed_fields=frozenset({"recipient_name", "code", "expires_minutes"}),
        required_fields=frozenset({"code"}),
        secret_fields=frozenset({"code"}),
        subject_template="Your HealthOS password reset code",
        html_template=(
            "<p>Hello {recipient_name},</p>"
            "<p>Your password reset code is <strong>{code}</strong>.</p>"
            "<p>It expires in {expires_minutes} minutes. If you did not request this, ignore this email.</p>"
        ),
        text_template=(
            "Hello {recipient_name},\n\n"
            "Your password reset code is {code}.\n"
            "It expires in {expires_minutes} minutes. "
            "If you did not request this, ignore this email.\n"
        ),
    ),
}


def get_spec(code: str) -> TemplateSpec | None:
    return TEMPLATES.get(code)


def secret_fields_for(code: str) -> frozenset[str]:
    spec = get_spec(code)
    return spec.secret_fields if spec else frozenset()


def validate_template_context(code: str, context: dict[str, Any]) -> None:
    spec = get_spec(code)
    if spec is None:
        raise UnsupportedTemplate(f"Template '{code}' is not approved.")
    unknown = set(context) - spec.allowed_fields
    if unknown:
        raise InvalidTemplateContext(
            f"Template '{code}' does not accept fields: {', '.join(sorted(unknown))}."
        )
    missing = {
        field
        for field in spec.required_fields
        if context.get(field) in (None, "")
    }
    if missing:
        raise InvalidTemplateContext(
            f"Template '{code}' is missing fields: {', '.join(sorted(missing))}."
        )


def render_template(code: str, context: dict[str, Any]) -> RenderedTemplate:
    spec = get_spec(code)
    if spec is None:
        raise UnsupportedTemplate(f"Template '{code}' is not approved.")
    validate_template_context(code, context)
    return spec.render(context)
