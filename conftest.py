"""
conftest.py — shared pytest configuration.

Adds the project root and pipeline/ directory to sys.path so that
`from pipeline.emit import ...` works inside tests/.
"""

import sys
from pathlib import Path

# Project root
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
# Pipeline package
sys.path.insert(0, str(ROOT / "pipeline"))
