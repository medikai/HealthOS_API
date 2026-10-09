"""Approved email templates with explicit escaping.

Only templates registered here can be sent. Interpolated values are
HTML-escaped for the HTML body, emitted verbatim in the text body, and
newline/header-injection is rejected in subjects/names.

Every template is rendered inside the shared MedikAI transactional layout
(table-based, inline CSS, no remote images) so all outbound mail carries the
approved brand header, palette and footer.
"""

from dataclasses import dataclass
from html import escape as html_escape
from typing import Any

from .schemas import reject_header_injection

BRAND_NAME = "MedikAI"
BRAND_TAGLINE = "Happy clinics. Happier doctors. Happiest patients."
BRAND_SITE_URL = "https://medikai.in"
BRAND_SITE_LABEL = "medikai.in"

# Brand palette (BRAND-DESIGN-RULES.md): navy #0B2033, teal #0E8F8A,
# turquoise #18C7B1, background #F7FAFC, secondary text #475569.
_HTML_DOCUMENT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="light only" />
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:#F7FAFC;">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{subject}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F7FAFC;">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background-color:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;overflow:hidden;font-family:Inter,'Segoe UI',Arial,Helvetica,sans-serif;color:#0B2033;">
        <tr>
          <td style="background-color:#0B2033;padding:22px 28px;">
            <span style="font-size:22px;font-weight:700;letter-spacing:-0.5px;color:#FFFFFF;">Medik<span style="color:#18C7B1;">AI</span></span>
          </td>
        </tr>
        <tr>
          <td style="height:4px;line-height:4px;font-size:0;background-color:#0E8F8A;">&nbsp;</td>
        </tr>
        <tr>
          <td style="padding:28px 28px 8px 28px;font-size:16px;line-height:1.6;color:#0B2033;">
{body}
          </td>
        </tr>
        <tr>
          <td style="padding:0 28px 28px 28px;font-size:13px;line-height:1.6;color:#475569;">
            This is a transactional email from {brand}. If you did not expect it, you can safely ignore it.
          </td>
        </tr>
        <tr>
          <td style="background-color:#F7FAFC;border-top:1px solid #E2E8F0;padding:16px 28px;font-size:12px;line-height:1.6;color:#64748B;">
            <strong style="color:#0B2033;">{brand}</strong> &middot; {tagline}<br />
            <a href="{site}" style="color:#0E8F8A;text-decoration:none;">{site_label}</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""


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


def _branded_html(inner_html: str, subject: str) -> str:
    return _HTML_DOCUMENT.format(
        subject=html_escape(subject),
        body=inner_html,
        brand=BRAND_NAME,
        tagline=BRAND_TAGLINE,
        site=BRAND_SITE_URL,
        site_label=BRAND_SITE_LABEL,
    )


def _branded_text(inner_text: str) -> str:
    return (
        f"{BRAND_NAME}\n"
        f"{'=' * len(BRAND_NAME)}\n\n"
        f"{inner_text.rstrip()}\n\n"
        f"--\n"
        f"{BRAND_NAME} · {BRAND_TAGLINE}\n"
        f"{BRAND_SITE_URL} · This is a transactional email.\n"
    )


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
        inner_html = self.html_template.format(
            **{field: html_escape(value) for field, value in values.items()}
        )
        html_body = _branded_html(inner_html, subject)
        text_body = _branded_text(self.text_template.format(**values))
        return RenderedTemplate(subject=subject, html_body=html_body, text_body=text_body)


def _stringify(value: Any) -> str:
    return "" if value is None else str(value)


TEMPLATES: dict[str, TemplateSpec] = {
    "email_test": TemplateSpec(
        code="email_test",
        allowed_fields=frozenset({"note"}),
        required_fields=frozenset({"note"}),
        secret_fields=frozenset(),
        subject_template="MedikAI email test",
        html_template="<p>MedikAI email test.</p><p>{note}</p>",
        text_template="MedikAI email test.\n{note}\n",
    ),
    "staff_invitation": TemplateSpec(
        code="staff_invitation",
        allowed_fields=frozenset(
            {"recipient_name", "organization_name", "role_label", "invite_url"}
        ),
        required_fields=frozenset({"organization_name", "invite_url"}),
        secret_fields=frozenset({"invite_url"}),
        subject_template="You have been invited to {organization_name} on MedikAI",
        html_template=(
            "<p>Hello {recipient_name},</p>"
            "<p>{organization_name} has invited you to join MedikAI as {role_label}.</p>"
            '<p style="margin:24px 0;">'
            '<a href="{invite_url}" style="display:inline-block;background-color:#0E8F8A;color:#FFFFFF;font-size:15px;font-weight:600;padding:12px 22px;border-radius:8px;text-decoration:none;">Accept your invitation</a>'
            "</p>"
            '<p style="color:#475569;font-size:13px;">This link expires soon. If you did not expect it, ignore this email.</p>'
        ),
        text_template=(
            "Hello {recipient_name},\n\n"
            "{organization_name} has invited you to join MedikAI as {role_label}.\n"
            "Accept your invitation: {invite_url}\n\n"
            "This link expires soon. If you did not expect it, ignore this email.\n"
        ),
    ),
    "welcome": TemplateSpec(
        code="welcome",
        allowed_fields=frozenset({"recipient_name"}),
        required_fields=frozenset(),
        secret_fields=frozenset(),
        subject_template="Welcome to MedikAI",
        html_template=(
            "<p>Hello {recipient_name},</p>"
            "<p>Welcome to MedikAI. Your account is ready.</p>"
            "<p>If you did not create this account, contact your administrator.</p>"
        ),
        text_template=(
            "Hello {recipient_name},\n\n"
            "Welcome to MedikAI. Your account is ready.\n"
            "If you did not create this account, contact your administrator.\n"
        ),
    ),
    "password_changed": TemplateSpec(
        code="password_changed",
        allowed_fields=frozenset({"recipient_name"}),
        required_fields=frozenset(),
        secret_fields=frozenset(),
        subject_template="Your MedikAI password was changed",
        html_template=(
            "<p>Hello {recipient_name},</p>"
            "<p>Your MedikAI password was changed. If this was not you, contact your administrator immediately.</p>"
        ),
        text_template=(
            "Hello {recipient_name},\n\n"
            "Your MedikAI password was changed. "
            "If this was not you, contact your administrator immediately.\n"
        ),
    ),
    "password_reset_code": TemplateSpec(
        code="password_reset_code",
        allowed_fields=frozenset({"recipient_name", "code", "expires_minutes"}),
        required_fields=frozenset({"code"}),
        secret_fields=frozenset({"code"}),
        subject_template="Your MedikAI password reset code",
        html_template=(
            "<p>Hello {recipient_name},</p>"
            "<p>Your password reset code is "
            '<strong style="font-size:26px;letter-spacing:6px;color:#0B2033;">{code}</strong>.</p>'
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
