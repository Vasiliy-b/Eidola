#!/usr/bin/env python3
"""Run the uniqualization worker (processes pending content)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from eidola.content.uniqualization_worker import main

if __name__ == "__main__":
    main()
