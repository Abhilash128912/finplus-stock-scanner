"""
Test to enforce zero yfinance imports and zero Yahoo URLs under src/.
Guarantees that yfinance is completely absent from the live production codebase.
"""

import os
import re
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

YAHOO_URL_REGEX = re.compile(r"https?://[^\s'\"<>]*yahoo[^\s'\"<>]*", re.IGNORECASE)
YFINANCE_IMPORT_REGEX = re.compile(r"^\s*(import\s+yfinance|from\s+yfinance\b|from\s+.*yfinance_gateway\b)", re.MULTILINE)


def test_zero_yfinance_and_zero_yahoo_urls_in_src():
    """Fails if any Python file under src/ imports yfinance or contains a Yahoo URL."""
    violations = []

    py_files = list(SRC_DIR.rglob("*.py"))
    assert len(py_files) > 0, "No python files found under src/!"

    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            content = py_file.read_text(encoding="latin-1")

        # 1. Check for yfinance imports
        for line_num, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            # Ignore comments that explicitly discuss removing or testing yfinance
            if stripped.startswith("#"):
                continue

            if YFINANCE_IMPORT_REGEX.search(line):
                violations.append(f"[IMPORT] {py_file.relative_to(SRC_DIR)}:{line_num} -> {line.strip()}")

            # 2. Check for Yahoo URLs
            if YAHOO_URL_REGEX.search(line):
                violations.append(f"[YAHOO_URL] {py_file.relative_to(SRC_DIR)}:{line_num} -> {line.strip()}")

    assert not violations, (
        f"Found {len(violations)} yfinance/Yahoo violations under src/:\n" + "\n".join(violations)
    )
