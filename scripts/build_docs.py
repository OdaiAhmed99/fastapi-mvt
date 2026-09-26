"""Build the single-file HTML documentation site from the Markdown docs.

    pip install markdown
    python scripts/build_docs.py          # writes docs/site/index.html
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).with_name("docs_template.html")
OUTPUT = ROOT / "docs" / "site" / "index.html"

# (page id, nav label, source file)
PAGES = [
    ("overview", "Overview", ROOT / "README.md"),
    ("tutorial", "Tutorial", ROOT / "docs" / "tutorial.md"),
    ("orm", "ORM guide", ROOT / "docs" / "orm.md"),
    ("migrations", "Migrations", ROOT / "docs" / "migrations.md"),
    ("testing", "Testing", ROOT / "docs" / "testing.md"),
    ("auth-and-admin", "Auth & admin", ROOT / "docs" / "auth-and-admin.md"),
    ("cli", "Command line", ROOT / "docs" / "cli.md"),
    ("deployment", "Deployment", ROOT / "docs" / "deployment.md"),
    ("comparison", "Comparison", ROOT / "docs" / "comparison.md"),
    ("design", "Design notes", ROOT / "docs" / "design.md"),
    ("upgrading-from-0.1", "Upgrading from 0.1", ROOT / "docs" / "upgrading-from-0.1.md"),
    ("changelog", "Changelog", ROOT / "CHANGELOG.md"),
]
PAGE_IDS = {path.name: page_id for page_id, _, path in PAGES}
PAGE_IDS["README.md"] = "overview"


def slugify(value: str, separator: str) -> str:
    value = re.sub(r"<[^>]+>", "", value).lower()
    value = re.sub(r"[^\w\s-]", "", value).strip()
    return re.sub(r"[\s]+", separator, value)


def rewrite_links(text: str, page_id: str) -> str:
    def replace(match: re.Match) -> str:
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            return match.group(0)
        file, _, fragment = target.partition("#")
        name = Path(file).name if file else ""
        if name and name not in PAGE_IDS:
            return match.group(0)
        page = PAGE_IDS.get(name, page_id)
        anchor = f"#{page}--{fragment}" if fragment else f"#{page}"
        return f'href="{anchor}"'

    return re.sub(r'href="([^"]+)"', replace, text)


def render_page(page_id: str, source: Path) -> tuple[str, list[tuple[str, str]]]:
    text = source.read_text(encoding="utf-8")
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", "sane_lists"],
        extension_configs={"toc": {"slugify": slugify, "permalink": False}},
    )
    body = md.convert(text)
    # Prefix heading ids so anchors are unique across pages: id="orm--querying".
    body = re.sub(r'<(h[1-4]) id="([^"]+)"', lambda m: f'<{m.group(1)} id="{page_id}--{m.group(2)}"', body)
    body = rewrite_links(body, page_id)
    # Fenced blocks without a language are command output: style them as a terminal.
    body = body.replace("<pre><code>", '<pre class="output"><code>')
    sections = [
        (f"{page_id}--{token['id']}", html.unescape(re.sub(r"<[^>]+>", "", token["name"])))
        for top in md.toc_tokens
        for token in (top.get("children") or [top])
        if token["level"] == 2
    ]
    return body, sections


def build() -> Path:
    nav, pages = [], []
    for page_id, label, source in PAGES:
        body, sections = render_page(page_id, source)
        subnav = "".join(f'<li><a href="#{sid}">{html.escape(name)}</a></li>' for sid, name in sections)
        nav.append(
            f'<li data-page="{page_id}"><a class="page-link" href="#{page_id}">{label}</a>'
            + (f'<ol class="sections">{subnav}</ol>' if subnav else "")
            + "</li>"
        )
        pages.append(f'<article class="page" id="{page_id}" data-label="{label}" hidden>{body}</article>')
    output = TEMPLATE.read_text(encoding="utf-8").replace("{{NAV}}", "\n".join(nav)).replace("{{PAGES}}", "\n".join(pages))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(output, encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    print(f"Wrote {build()}")
