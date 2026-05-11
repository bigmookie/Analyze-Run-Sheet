"""Top-level launcher so the user can double-click or run `python analyze_run_sheet.py`."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from run_sheet_analyzer.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
