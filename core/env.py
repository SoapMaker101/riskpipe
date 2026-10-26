"""Настройки окружения: переменная процесса, иначе строка из riskpipe/.env (в git не попадает)."""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def getenv(name: str, env_file: Path | None = None) -> str | None:
    v = os.environ.get(name)
    if v:
        return v.strip()
    p = env_file or ENV_FILE
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None
