"""
Architecture Conformance Test:
Ensures src/gateway/yfinance_gateway.py is the ONLY file in src/ that imports yfinance.
Fails immediately if any other production file directly imports yfinance.
"""

import os
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
GATEWAY_FILE = (SRC_DIR / "gateway" / "yfinance_gateway.py").resolve()

def test_yfinance_gateway_is_sole_importer():
    violations = []
    for root, dirs, files in os.walk(SRC_DIR):
        for f in files:
            if f.endswith(".py"):
                p = Path(root) / f
                if p.resolve() == GATEWAY_FILE:
                    continue
                with open(p, "r", encoding="utf-8", errors="ignore") as fp:
                    for idx, line in enumerate(fp, 1):
                        stripped = line.strip()
                        if stripped.startswith("import yfinance") or stripped.startswith("from yfinance"):
                            violations.append(f"{p}:{idx} -> {stripped}")

    assert not violations, f"Direct yfinance imports found outside gateway:\n" + "\n".join(violations)

