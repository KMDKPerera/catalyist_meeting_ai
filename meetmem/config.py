import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    """Read KEY=VALUE lines from .env so API keys never have to live in code."""
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

DATA = Path(os.environ.get("MEETMEM_DATA", ROOT / "data"))
DB_PATH = Path(os.environ.get("MEETMEM_DB", ROOT / "memory.db"))
RUNS = ROOT / "runs"

# Words of transcript sent to the model per extraction call. One meeting is
# 1 to 4 calls at this size; small enough for free-tier token-per-minute caps.
EXTRACT_WORDS = int(os.environ.get("EXTRACT_WORDS", "3000"))
# Words per retrieval chunk in the transcript index.
CHUNK_WORDS = 200
