"""Render a markdown document in reports/ to PDF, including mermaid diagrams and figures.

Requires Microsoft Edge (or Chrome) and an internet connection for mermaid.js.

Run: python reports/build_pdf.py data_pipeline.md
"""

import html
import re
import subprocess
import sys
from pathlib import Path

import markdown

HERE = Path(__file__).resolve().parent
BROWSERS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
]

CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #222; }
h1 { font-size: 20pt; border-bottom: 2px solid #2c3e50; padding-bottom: 4px; }
h2 { font-size: 15pt; color: #2c3e50; margin-top: 26px; border-bottom: 1px solid #ccc; page-break-after: avoid; }
h3 { font-size: 12pt; color: #34495e; page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 14px; font-size: 9pt; page-break-inside: avoid; }
th, td { border: 1px solid #bbb; padding: 4px 6px; text-align: left; vertical-align: top; }
th { background: #ecf0f1; }
code { font-family: Consolas, monospace; font-size: 9pt; background: #f4f4f4; padding: 0 2px; }
pre { background: #f4f4f4; padding: 8px; font-size: 8.5pt; page-break-inside: avoid; white-space: pre-wrap; }
img { max-width: 100%; display: block; margin: 10px auto 2px; page-break-inside: avoid; }
.mermaid { text-align: center; margin: 12px 0; page-break-inside: avoid; }
hr { border: 0; border-top: 1px solid #ddd; }
"""


def to_html(md_text: str) -> str:
    blocks = []

    def stash(m):
        blocks.append(m.group(1))
        return f"MERMAIDBLOCK{len(blocks) - 1}"

    md_text = re.sub(r"```mermaid\n(.*?)```", stash, md_text, flags=re.S)
    body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
    for i, src in enumerate(blocks):
        body = body.replace(f"<p>MERMAIDBLOCK{i}</p>", f'<div class="mermaid">{html.escape(src)}</div>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad: true, theme: "neutral", flowchart: {{useMaxWidth: true}}}});</script>
</head><body>{body}</body></html>"""


def main() -> None:
    src = HERE / (sys.argv[1] if len(sys.argv) > 1 else "data_pipeline.md")
    html_path, pdf_path = src.with_suffix(".html"), src.with_suffix(".pdf")
    html_path.write_text(to_html(src.read_text(encoding="utf-8")), encoding="utf-8")

    tmp_pdf = pdf_path.with_suffix(".tmp.pdf")
    tmp_pdf.unlink(missing_ok=True)
    browser = next(b for b in BROWSERS if b.exists())
    subprocess.run([
        str(browser), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
        "--virtual-time-budget=15000", f"--print-to-pdf={tmp_pdf}", html_path.as_uri(),
    ], check=True)
    html_path.unlink()
    if not tmp_pdf.exists():
        raise RuntimeError("the browser did not produce a PDF")
    tmp_pdf.replace(pdf_path)
    print(f"wrote {pdf_path}")


if __name__ == "__main__":
    main()
