"""Print the HTML report to PDF with headless Chrome, so the PDF matches the
web page (charts are drawn by the page's JavaScript)."""
import datetime
import os
import pathlib
import shutil
import subprocess
import tempfile
import time

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge",
]
# Landscape Letter minus the @page margins (11in - 22mm), in CSS pixels, so
# the charts are drawn at the width they are printed at.
PRINT_WIDTH = 973
TIMEOUT = 120


def find_chrome():
    """Chrome/Chromium/Edge binary: CHROME_PATH, then common install paths."""
    for candidate in [os.environ.get("CHROME_PATH"), *CHROME_CANDIDATES]:
        if not candidate:
            continue
        path = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if path and os.path.exists(path):
            return path
    raise RuntimeError("Chrome, Chromium or Edge is needed to write the PDF; "
                       "install one or set CHROME_PATH")


def report_filename(now=None):
    """Equinix_Report_{Date}_{Time}.pdf, e.g. Equinix_Report_2026-10-05_1512.pdf"""
    now = now or datetime.datetime.now()
    return f"Equinix_Report_{now:%Y-%m-%d}_{now:%H%M}.pdf"


def write_pdf(html, out_dir="."):
    """Render `html` to <out_dir>/Equinix_Report_{Date}_{Time}.pdf; returns the path."""
    out_dir = pathlib.Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = (out_dir / report_filename()).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        page = pathlib.Path(tmp) / "report.html"
        page.write_text(html, encoding="utf-8")
        # Headless Chrome on macOS can keep running after writing the PDF, so
        # wait for the file to finish writing and then close Chrome ourselves.
        chrome = subprocess.Popen([
            find_chrome(), "--headless=new", "--disable-gpu",
            "--no-pdf-header-footer", "--hide-scrollbars",
            f"--window-size={PRINT_WIDTH},1200",
            # let fonts load and the charts draw before printing
            "--virtual-time-budget=15000",
            f"--user-data-dir={tmp}/profile",
            f"--print-to-pdf={pdf_path}",
            page.as_uri() + "#pdf",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for_file(chrome, pdf_path)
        finally:
            if chrome.poll() is None:
                chrome.terminate()
                try:
                    chrome.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    chrome.kill()
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RuntimeError(f"Chrome exited with code {chrome.returncode} "
                           "without writing the PDF")
    return pdf_path


def wait_for_file(proc, path):
    """Return once Chrome exits or `path` has stopped growing."""
    deadline = time.monotonic() + TIMEOUT
    last_size = -1
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        size = path.stat().st_size if path.exists() else -1
        if size > 0 and size == last_size:
            return
        last_size = size
        time.sleep(1)
    raise RuntimeError(f"Chrome did not finish the PDF within {TIMEOUT} s")
