"""
Launches the control panel window. Double-click this file (or a shortcut
to it) instead of using the command line for day-to-day Start/Resume,
Safe Stop, Status, and Run Now actions.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from shenzhen_sorter import gui  # noqa: E402

if __name__ == "__main__":
    gui.main()
