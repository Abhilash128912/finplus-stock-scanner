"""
Architecture Conformance Test:
Ensures ZERO files under src/ import yfinance.
yfinance is strictly banished from the live path.
"""

import os
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

def test_zero_yfinance_imports_in_src():
    violations = []
    for root, dirs, files in os.walk(SRC_DIR):
        for f in files:
            if f.endswith(".py"):
                p = Path(root) / f
                with open(p, "r", encoding="utf-8", errors="ignore") as fp:
                    for idx, line in enumerate(fp, 1):
                        stripped = line.strip()
                        if stripped.startswith("#"):
                            continue
                        if stripped.startswith("import yfinance") or stripped.startswith("from yfinance"):
                            violations.append(f"{p}:{idx} -> {stripped}")

    assert not violations, f"yfinance imports found in src/:\n" + "\n".join(violations)
