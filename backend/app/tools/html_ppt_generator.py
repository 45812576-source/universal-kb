"""HTML PPT generator — renders slides using file-based brand templates.

Templates live in app/tools/ppt_templates/*.html.
Adding a new template = drop an HTML file there, no code changes needed.

Each template file must have a <body> tag; the tool extracts everything
up to and including <body> as the head, then appends slides_html + </body></html>.

Input params:
{
  "template": "sketch",          # filename without .html
  "title": "演示标题",
  "slides_html": "<div class=\"slide-label\">...</div><div class=\"slide ...\">...</div>..."
}

Output: {"file_id": "xxx.html", "download_url": "/api/files/xxx.html", "filename": "xxx.html"}
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

_UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "./uploads")
_GENERATED_DIR = Path(_UPLOAD_DIR) / "generated"
_TEMPLATES_DIR = Path(__file__).parent / "ppt_templates"


def _list_templates() -> list[str]:
    """Return available template names (filenames without .html)."""
    return [p.stem for p in _TEMPLATES_DIR.glob("*.html")]


def _load_template_head(template: str) -> str:
    """Extract the head portion (up to and including <body>) from a template file."""
    tmpl_path = _TEMPLATES_DIR / f"{template}.html"
    if not tmpl_path.exists():
        available = ", ".join(_list_templates())
        raise ValueError(f"Template '{template}' not found. Available: {available}")

    content = tmpl_path.read_text(encoding="utf-8")
    # Split at <body> tag (keep the tag itself)
    match = re.search(r"<body[^>]*>", content, re.IGNORECASE)
    if not match:
        raise ValueError(f"Template '{template}' has no <body> tag")

    # Return everything up to and including <body>
    head = content[: match.end()]
    # Replace the original title with the user-provided one
    head = re.sub(r"<title>[^<]*</title>", "<title>{title}</title>", head, count=1)
    return head


def execute(params: dict) -> dict:
    """Assemble HTML PPT from template head + LLM-generated slides body."""
    _GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    template = params.get("template", "sketch").lower()
    title = params.get("title", "演示文稿")
    slides_html = params.get("slides_html", "")

    try:
        head_template = _load_template_head(template)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    head = head_template.replace("{title}", title)
    html = head + "\n" + slides_html + "\n<div style=\"height:60px;\"></div>\n</body>\n</html>"

    file_id = f"{uuid.uuid4().hex}.html"
    file_path = _GENERATED_DIR / file_id
    file_path.write_text(html, encoding="utf-8")

    safe_title = "".join(c for c in title if c not in r'\/:*?"<>|')[:40]
    filename = f"{safe_title}.html"

    return {
        "file_id": file_id,
        "download_url": f"/api/files/{file_id}",
        "filename": filename,
        "message": f"PPT已生成（{template}风格），点击下载按钮在浏览器打开查看",
    }
