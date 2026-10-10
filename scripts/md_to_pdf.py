"""Render the project markdown documents to A4 PDFs.

Same palette as the one pager: white paper, black text, one red accent. Blockquotes
are the lines to say out loud, so they are set apart rather than indented and grey.

Usage:  python scripts/md_to_pdf.py README.md docs/ENGINEERING_NOTES.md
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
PORT = 8031

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

CSS = """
:root{ --ink:#111; --ink-2:#333; --muted:#6b6b6b; --signal:#E1251B;
       --rule:#DDD; --wash:#F6F6F6; }
@page{ size:A4; margin:16mm 15mm 14mm; }
*{ box-sizing:border-box; }
body{ margin:0; color:var(--ink); background:#fff;
      font:10.5px/1.55 "Helvetica Neue",Helvetica,Arial,sans-serif;
      -webkit-font-smoothing:antialiased; }
.page{ max-width:180mm; margin:0 auto; padding:10mm 0; }

h1{ font-size:21px; letter-spacing:-.4px; margin:0 0 4px; line-height:1.15; }
h1 + p{ color:var(--muted); font-size:10px; margin:0 0 4px; }
h2{ font-size:12.5px; letter-spacing:-.2px; margin:20px 0 7px;
    padding-bottom:4px; border-bottom:1px solid var(--ink);
    break-after:avoid; page-break-after:avoid; }
h3{ font-size:11px; margin:13px 0 5px; }
p{ margin:0 0 7px; color:var(--ink-2); }
strong{ color:var(--ink); }
em{ color:var(--muted); font-style:italic; }
ul,ol{ margin:0 0 8px; padding-left:16px; }
li{ margin-bottom:3px; color:var(--ink-2); }
code{ font-family:"IBM Plex Mono",Consolas,monospace; font-size:9.5px;
      background:var(--wash); padding:1px 4px; border-radius:2px; color:var(--ink); }

/* the lines to say out loud */
blockquote{ margin:8px 0 10px; padding:8px 12px; border-left:2.5px solid var(--signal);
            background:#FDF7F6; break-inside:avoid; page-break-inside:avoid; }
blockquote p{ margin:0 0 6px; color:var(--ink); font-size:11px; line-height:1.5; }
blockquote p:last-child{ margin-bottom:0; }

table{ width:100%; border-collapse:collapse; font-size:9.6px; margin:6px 0 10px;
       break-inside:avoid; page-break-inside:avoid; }
th{ text-align:left; font-size:7.8px; letter-spacing:.1em; text-transform:uppercase;
    color:var(--muted); font-weight:700; padding:6px 8px;
    border-bottom:1px solid var(--ink); }
td{ padding:6px 8px; border-bottom:1px solid var(--rule); vertical-align:top;
    color:var(--ink-2); }
tr:last-child td{ border-bottom:0; }

hr{ border:0; border-top:1px solid var(--rule); margin:16px 0; }
"""

PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<style>{css}</style><div class="page">{body}</div>"""


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    found = shutil.which("chrome") or shutil.which("msedge")
    if not found:
        sys.exit("No Chrome or Edge found for PDF rendering.")
    return found


def render(md_path: Path, out_dir: Path) -> Path:
    text = md_path.read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["tables", "sane_lists", "attr_list"])
    html = PAGE.format(title=md_path.stem, css=CSS, body=body)
    target = out_dir / (md_path.stem + ".html")
    target.write_text(html, encoding="utf-8")
    return target


def main(names: list[str]) -> int:
    chrome = find_chrome()
    tmp = Path(tempfile.mkdtemp(prefix="md2pdf_"))

    pages = []
    for n in names:
        src = ROOT / n
        if not src.exists():
            sys.exit("Not found: {}".format(src))
        pages.append((src, render(src, tmp)))

    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp))
    server = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)

    try:
        for src, page in pages:
            pdf = src.with_suffix(".pdf")
            # A fresh profile dir and no captured pipes: sharing the running
            # browser's profile makes headless Chrome hang instead of printing.
            subprocess.run(
                [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                 "--user-data-dir={}".format(tmp / "profile"),
                 "--print-to-pdf={}".format(pdf),
                 "http://127.0.0.1:{}/{}".format(PORT, page.name)],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=90)
            size = pdf.stat().st_size // 1024 if pdf.exists() else 0
            print("{:<24} -> {:<24} {} KB".format(src.name, pdf.name, size))
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:] or ["README.md", "docs/ENGINEERING_NOTES.md"]
    raise SystemExit(main(args))
