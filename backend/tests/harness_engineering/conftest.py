"""Shared fixtures for strict Skill Studio Harness Engineering E2E tests.

These tests target a live backend because they validate DB-backed replay,
SSE reconnect, cancellation, supersede, handoff, and bind-back behavior.
They are skipped unless HARNESS_ENGINEERING_E2E=1 is set.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx
import pytest


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _required_int(name: str) -> int:
    raw = os.getenv(name)
    if not raw:
        pytest.skip(f"{name} is required for Harness Engineering E2E tests")
    try:
        return int(raw)
    except ValueError:
        pytest.fail(f"{name} must be an integer, got {raw!r}")


@dataclass(frozen=True)
class HarnessE2EConfig:
    base_url: str
    token: str | None
    username: str | None
    password: str | None
    skill_id: int
    conversation_id: int
    timeout_s: float
    max_stream_events: int
    evidence_dir: Path
    database_url: str | None


class EvidenceRecorder:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def write_sse(self, name: str, events: Iterable[dict[str, Any]]) -> Path:
        lines: list[str] = []
        for event in events:
            lines.append(f"event: {event.get('event')}")
            lines.append(f"data: {json.dumps(event.get('data') or {}, ensure_ascii=False)}")
            lines.append("")
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def parse_sse_text(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    event_name = "message"
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = "message"
            return
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw": raw}
        events.append({"event": event_name, "data": data})
        event_name = "message"
        data_lines = []

    for line in text.splitlines():
        if not line:
            flush()
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    flush()
    return events


class HarnessApiClient:
    def __init__(self, config: HarnessE2EConfig):
        self.config = config
        self.client = httpx.Client(base_url=config.base_url, timeout=config.timeout_s)
        self._token = config.token

    def close(self) -> None:
        self.client.close()

    @property
    def headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def token(self) -> str | None:
        if self._token:
            return self._token
        if not self.config.username or not self.config.password:
            pytest.skip("Set HARNESS_E2E_TOKEN or HARNESS_E2E_USERNAME/HARNESS_E2E_PASSWORD")
        response = self.client.post(
            "/api/auth/login",
            json={"username": self.config.username, "password": self.config.password},
        )
        response.raise_for_status()
        self._token = response.json()["access_token"]
        return self._token

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.client.get(path, params=params or None, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.post(path, json=payload or {}, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def init_session(self, skill_id: int, session_mode: str = "optimize") -> dict[str, Any]:
        return self.post(f"/api/skills/{skill_id}/studio/session/init", {"session_mode": session_mode})

    def get_session(self, skill_id: int) -> dict[str, Any]:
        return self.get(f"/api/skills/{skill_id}/studio/session")

    def create_card(
        self,
        skill_id: int,
        *,
        title: str,
        summary: str,
        target_file: str | None = None,
        activate: bool = True,
    ) -> dict[str, Any]:
        return self.post(
            f"/api/skills/{skill_id}/studio/cards",
            {
                "card_type": "governance",
                "title": title,
                "summary": summary,
                "phase": "phase_2_what",
                "priority": "high",
                "target_file": target_file,
                "origin": "harness_engineering_e2e",
                "activate": activate,
            },
        )

    def handoff_card(self, skill_id: int, card_id: str) -> dict[str, Any]:
        return self.post(
            f"/api/skills/{skill_id}/studio/cards/{card_id}/handoff",
            {
                "target_role": "tool",
                "target_file": "tools/harness_engineering_fixture.py",
                "handoff_policy": "open_development_studio",
                "route_kind": "external",
                "destination": "dev_studio",
                "return_to": "bind_back",
                "summary": "Harness Engineering E2E external tool implementation handoff.",
                "handoff_summary": "Implement a deterministic tool fixture and return evidence.",
                "acceptance_criteria": [
                    "tool call has audit summary",
                    "bind-back enters confirm or validate state",
                ],
                "activate_target": True,
            },
        )

    def bind_back_card(self, skill_id: int, card_id: str) -> dict[str, Any]:
        return self.post(
            f"/api/skills/{skill_id}/studio/cards/{card_id}/bind-back",
            {
                "source": "harness_engineering_e2e",
                "summary": "External implementation returned with deterministic evidence.",
                "required_checks": ["contract", "audit", "validation"],
            },
        )

    def stream_studio_message(
        self,
        conversation_id: int,
        *,
        skill_id: int,
        content: str,
        max_events: int | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        payload = {
            "content": content,
            "selected_skill_id": skill_id,
            "editor_is_dirty": False,
        }
        events: list[dict[str, Any]] = []
        run_id: str | None = None
        with self.client.stream(
            "POST",
            f"/api/conversations/{conversation_id}/messages/stream",
            json=payload,
            headers=self.headers,
        ) as response:
            response.raise_for_status()
            run_id = response.headers.get("X-Studio-Run-Id")
            buffer = ""
            limit = max_events or self.config.max_stream_events
            for chunk in response.iter_text():
                buffer += chunk
                if "\n\n" not in buffer:
                    continue
                parts = buffer.split("\n\n")
                buffer = parts.pop() or ""
                for part in parts:
                    events.extend(parse_sse_text(part + "\n\n"))
                    if len(events) >= limit or any(item["event"] in {"done", "error"} for item in events):
                        return run_id, events
        return run_id, events

    def stream_run_events(
        self,
        conversation_id: int,
        run_id: str,
        *,
        after: int = 0,
        max_events: int | None = None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        with self.client.stream(
            "GET",
            f"/api/conversations/{conversation_id}/studio-runs/{run_id}/events",
            params={"after": after},
            headers=self.headers,
        ) as response:
            response.raise_for_status()
            buffer = ""
            limit = max_events or self.config.max_stream_events
            for chunk in response.iter_text():
                buffer += chunk
                if "\n\n" not in buffer:
                    continue
                parts = buffer.split("\n\n")
                buffer = parts.pop() or ""
                for part in parts:
                    events.extend(parse_sse_text(part + "\n\n"))
                    if len(events) >= limit or any(item["event"] in {"done", "error"} for item in events):
                        return events
        return events

    def active_run(self, conversation_id: int, skill_id: int) -> dict[str, Any]:
        return self.get(f"/api/conversations/{conversation_id}/studio-runs/active", skill_id=skill_id)

    def cancel_run(self, conversation_id: int, run_id: str) -> dict[str, Any]:
        return self.post(f"/api/conversations/{conversation_id}/studio-runs/{run_id}/cancel")


@pytest.fixture(scope="session")
def harness_e2e_config(tmp_path_factory: pytest.TempPathFactory) -> HarnessE2EConfig:
    if not _enabled("HARNESS_ENGINEERING_E2E"):
        pytest.skip("Set HARNESS_ENGINEERING_E2E=1 to run live Harness Engineering E2E tests")

    timestamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    evidence_root = Path(
        os.getenv(
            "HARNESS_E2E_EVIDENCE_DIR",
            str(tmp_path_factory.mktemp(f"harness-engineering-{timestamp}")),
        )
    )
    return HarnessE2EConfig(
        base_url=os.getenv("HARNESS_E2E_BASE_URL", "http://localhost:8000").rstrip("/"),
        token=os.getenv("HARNESS_E2E_TOKEN"),
        username=os.getenv("HARNESS_E2E_USERNAME"),
        password=os.getenv("HARNESS_E2E_PASSWORD"),
        skill_id=_required_int("HARNESS_E2E_SKILL_ID"),
        conversation_id=_required_int("HARNESS_E2E_CONVERSATION_ID"),
        timeout_s=float(os.getenv("HARNESS_E2E_TIMEOUT_SECONDS", "120")),
        max_stream_events=int(os.getenv("HARNESS_E2E_MAX_STREAM_EVENTS", "2000")),
        evidence_dir=evidence_root,
        database_url=os.getenv("HARNESS_E2E_DATABASE_URL"),
    )


@pytest.fixture
def harness_client(harness_e2e_config: HarnessE2EConfig):
    client = HarnessApiClient(harness_e2e_config)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def evidence(harness_e2e_config: HarnessE2EConfig, request: pytest.FixtureRequest) -> EvidenceRecorder:
    safe_name = request.node.name.replace("/", "_").replace(" ", "_")
    return EvidenceRecorder(harness_e2e_config.evidence_dir / safe_name)
