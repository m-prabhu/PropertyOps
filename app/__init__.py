"""Load .env before any submodule reads os.environ.

Several modules (notably `agora_agent.service`) read configuration at import
time, so this has to happen here rather than inside `main`.
"""
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
except Exception:  # pragma: no cover - dotenv is optional at import time
    pass
