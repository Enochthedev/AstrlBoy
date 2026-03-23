"""
HTML email templates for professional outbound formatting.

All emails from astrlboy should use these templates for consistent,
clean formatting. The base template handles layout, fonts, and
responsive design. Content templates handle specific email types.

Usage:
    from core.email_templates import render_email
    html = render_email("general", subject="Hello", body="...")
"""

from html import escape

# Base HTML template — clean, minimal, responsive.
# Uses system fonts, no external resources, works in all email clients.
_BASE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{subject}</title>
<style>
  body {{
    margin: 0;
    padding: 0;
    background-color: #f6f6f6;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    font-size: 15px;
    line-height: 1.6;
    color: #1a1a1a;
  }}
  .wrapper {{
    max-width: 600px;
    margin: 0 auto;
    padding: 20px;
  }}
  .content {{
    background-color: #ffffff;
    border-radius: 8px;
    padding: 32px;
  }}
  .body-text {{
    white-space: pre-line;
  }}
  .body-text p {{
    margin: 0 0 16px 0;
  }}
  .signature {{
    margin-top: 24px;
    padding-top: 16px;
    border-top: 1px solid #e5e5e5;
    color: #666666;
    font-size: 13px;
  }}
  .signature a {{
    color: #1a1a1a;
    text-decoration: none;
  }}
  .footer {{
    text-align: center;
    padding: 16px 0;
    color: #999999;
    font-size: 11px;
  }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="content">
    {content}
    <div class="signature">
      <strong>astrlboy</strong><br>
      Autonomous AI Agent<br>
      <a href="https://astrlboy.xyz">astrlboy.xyz</a> &middot;
      <a href="https://x.com/astrlboy_">@astrlboy_</a>
    </div>
  </div>
  <div class="footer">
    Sent by astrlboy &mdash; an autonomous AI agent operated by
    <a href="https://wavedidwhat.com" style="color: #999;">Wave</a>
  </div>
</div>
</body>
</html>"""

# Application email template — for job applications
_APPLICATION_TEMPLATE = """\
<div class="body-text">{body}</div>"""

# Briefing email template — structured sections with headers
_BRIEFING_TEMPLATE = """\
<h2 style="margin: 0 0 8px 0; font-size: 18px; color: #1a1a1a;">
  Weekly Briefing &mdash; {contract_slug}
</h2>
<p style="color: #666; margin: 0 0 24px 0; font-size: 13px;">
  Week of {week_of}
</p>
{sections}"""

# Follow-up email template — for replies to incoming emails
_FOLLOW_UP_TEMPLATE = """\
<div class="body-text">{body}</div>"""

# General email template — for everything else
_GENERAL_TEMPLATE = """\
<div class="body-text">{body}</div>"""


def _text_to_html_body(text: str) -> str:
    """Convert plain text to simple HTML paragraphs.

    Splits on double newlines for paragraphs, preserves single newlines
    as line breaks. Escapes HTML entities.

    Args:
        text: Plain text body.

    Returns:
        HTML string with <p> tags.
    """
    escaped = escape(text)
    paragraphs = escaped.split("\n\n")
    html_parts = []
    for p in paragraphs:
        p = p.strip()
        if p:
            # Convert single newlines within paragraphs to <br>
            p = p.replace("\n", "<br>")
            html_parts.append(f"<p>{p}</p>")
    return "\n".join(html_parts)


def _render_briefing_sections(body: str) -> str:
    """Parse a briefing body into styled HTML sections.

    Looks for markdown-style headers (## Section Name) and formats them
    with styled headers and section dividers.

    Args:
        body: Plain text briefing with ## headers.

    Returns:
        HTML formatted sections.
    """
    import re

    sections = []
    current_header = ""
    current_content: list[str] = []

    for line in body.split("\n"):
        header_match = re.match(r"^##?\s+(.+)$", line.strip())
        if header_match:
            # Flush previous section
            if current_header or current_content:
                content_html = _text_to_html_body("\n".join(current_content))
                section = (
                    f'<div style="margin-bottom: 24px;">'
                    f'<h3 style="margin: 0 0 8px 0; font-size: 15px; '
                    f'color: #1a1a1a; text-transform: uppercase; '
                    f'letter-spacing: 0.5px;">{escape(current_header)}</h3>'
                    f'{content_html}'
                    f'</div>'
                )
                sections.append(section)
            current_header = header_match.group(1)
            current_content = []
        else:
            current_content.append(line)

    # Flush last section
    if current_header or current_content:
        content_html = _text_to_html_body("\n".join(current_content))
        section = (
            f'<div style="margin-bottom: 24px;">'
            f'<h3 style="margin: 0 0 8px 0; font-size: 15px; '
            f'color: #1a1a1a; text-transform: uppercase; '
            f'letter-spacing: 0.5px;">{escape(current_header)}</h3>'
            f'{content_html}'
            f'</div>'
        )
        sections.append(section)

    return "\n".join(sections) if sections else _text_to_html_body(body)


def render_email(
    email_type: str,
    *,
    subject: str = "",
    body: str = "",
    contract_slug: str = "",
    week_of: str = "",
) -> str:
    """Render a complete HTML email from a template.

    Args:
        email_type: One of 'application', 'briefing', 'follow_up', 'general'.
        subject: Email subject (used in HTML title).
        body: Plain text body to format.
        contract_slug: Client slug (for briefings).
        week_of: Week date string (for briefings).

    Returns:
        Complete HTML string ready to send.
    """
    body_html = _text_to_html_body(body)

    if email_type == "application":
        content = _APPLICATION_TEMPLATE.format(body=body_html)
    elif email_type == "briefing":
        sections = _render_briefing_sections(body)
        content = _BRIEFING_TEMPLATE.format(
            contract_slug=escape(contract_slug),
            week_of=escape(week_of),
            sections=sections,
        )
    elif email_type == "follow_up":
        content = _FOLLOW_UP_TEMPLATE.format(body=body_html)
    else:
        content = _GENERAL_TEMPLATE.format(body=body_html)

    return _BASE_TEMPLATE.format(
        subject=escape(subject),
        content=content,
    )
