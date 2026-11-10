from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from app.server import serve  # noqa: E402

if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else None)
