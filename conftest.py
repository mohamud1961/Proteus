"""Repository-wide pytest configuration for the combined public harness."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = ROOT / "proteus"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))
