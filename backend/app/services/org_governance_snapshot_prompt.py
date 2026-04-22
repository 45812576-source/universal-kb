from __future__ import annotations

from functools import lru_cache
from pathlib import Path


PROMPT_PROFILE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "workspace_org_governance_snapshot.md"


@lru_cache(maxsize=1)
def load_workspace_org_governance_prompt() -> str:
    return PROMPT_PROFILE_PATH.read_text(encoding="utf-8")
