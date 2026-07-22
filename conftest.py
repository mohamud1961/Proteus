"""Repository-wide pytest configuration for the combined public harness."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
BUILD_ROOT = ROOT / "aether_next_build"
if str(BUILD_ROOT) not in sys.path:
    sys.path.insert(0, str(BUILD_ROOT))
