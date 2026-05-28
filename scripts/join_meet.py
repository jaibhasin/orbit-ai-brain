from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbit.meet import main


if __name__ == "__main__":
    asyncio.run(main())
