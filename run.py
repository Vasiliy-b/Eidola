#!/usr/bin/env python3
"""Convenience wrapper to run eidola from project root."""
import sys
from pathlib import Path

# Add src/ to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

# Run main
from eidola.main import main
import asyncio

if __name__ == "__main__":
    asyncio.run(main())
