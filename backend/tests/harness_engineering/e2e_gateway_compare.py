#!/usr/bin/env python3
"""Phase 8 前后端联调脚本 — 对比 legacy vs gateway 路径的 SSE 事件流。

用法:
  1. 先跑 legacy（默认）:
       python tests/harness_engineering/e2e_gateway_compare.py
  2. 开启灰度后跑 gateway:
       GATEWAY_MAIN_CHAIN_ENABLED=true python tests/harness_engineering/e2e_gateway_compare.py
  3. 或者直接对比两条路径:
       python tests/harness_engineering/e2e_gateway_compare.py --compare
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE_URL = os.getenv("E2E_BASE_URL", "http://localhost:8000")
USERNAME = os.getenv("E2E_USERNAME", "admin")
PASSWORD = os.getenv("E2E_PASSWORD", "admin123")
CONV_ID = int(os.getenv("E2E_CONV_ID", "598"))
SKILL_ID = int(os.getenv("E2E_SKILL_ID", "1"))
MAX_EVENTS = int(os.getenv("E2E_MAX_EVENTS", "200"))
TIMEOUT = float(os.getenv("E2E_TIMEOUT", "60"))
MESSAGE = os.getenv("E2E_MESSAGE", "你好，请简短回复一个词")


def login(client: httpx.Client) -> str:
    resp = client.post(f"{BASE_URL}/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    resp.raise_for_status()
    return resp.json()["access_token"]


def parse_sse_stream(text: str) -> list[dict]:
    events = []
    event_name = "message"
    data_lines = []

    def flush():
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


def stream_message(client: httpx.Client, token: str) -> tuple[str | None, list[dict], float]:
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "content": MESSAGE,
        "selected_skill_id": SKILL_ID,
        "editor_is_dirty": False,
    }

    events = []
    run_id = None
    t0 = time.monotonic()

    with client.stream(
        "POST",
        f"{BASE_URL}/api/conversations/{CONV_ID}/messages/stream",
        json=payload,
        headers=headers,
        timeout=TIMEOUT,
    ) as resp:
        resp.raise_for_status()
        run_id = resp.headers.get("X-Studio-Run-Id")
        buffer = ""
        for chunk in resp.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                parts = buffer.split("\n\n", 1)
                segment = parts[0] + "\n\n"
                buffer = parts[1]
                events.extend(parse_sse_stream(segment))
                # 检查终止
                if len(events) >= MAX_EVENTS:
                    elapsed = time.monotonic() - t0
                    return run_id, events, elapsed
                for evt in events:
                    if evt["event"] in ("done", "error"):
                        elapsed = time.monotonic() - t0
                        return run_id, events, elapsed

    elapsed = time.monotonic() - t0
    return run_id, events, elapsed


def analyze(events: list[dict], label: str) -> dict:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    event_types = [e["event"] for e in events]
    unique_types = list(dict.fromkeys(event_types))
    type_counts = {}
    for t in event_types:
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"Total events: {len(events)}")
    print(f"Unique event types ({len(unique_types)}): {unique_types}")
    print(f"Event counts:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c}")

    # 关键 lifecycle
    has = {
        "studio_run": "studio_run" in event_types,
        "status_preparing": any(
            e["event"] == "status" and e["data"].get("stage") == "preparing"
            for e in events
        ),
        "run_started": "run_started" in event_types,
        "route_status": "route_status" in event_types,
        "workflow_state": "workflow_state" in event_types,
        "delta_or_replace": "delta" in event_types or "replace" in event_types,
        "done": "done" in event_types,
        "error": "error" in event_types,
        "patch_applied": "patch_applied" in event_types,
    }

    print(f"\nLifecycle checks:")
    for k, v in has.items():
        status = "PASS" if v else ("WARN" if k == "error" else "FAIL")
        print(f"  {k}: {status}")

    # 提取最终内容
    content = ""
    for e in events:
        if e["event"] == "replace":
            content = e["data"].get("text", content)
        elif e["event"] == "delta":
            content += e["data"].get("text", "")
    if content:
        print(f"\nFinal content preview ({len(content)} chars):")
        print(f"  {content[:200]}...")
    else:
        print("\nNo content found (delta/replace events)")

    return has


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", action="store_true", help="Run both legacy and gateway, then compare")
    args = parser.parse_args()

    with httpx.Client(timeout=TIMEOUT) as client:
        token = login(client)
        print(f"Logged in. Token: {token[:30]}...")

        if args.compare:
            print("\n--- Phase 1: Legacy path (GATEWAY_MAIN_CHAIN_ENABLED=false) ---")
            run_id1, events1, elapsed1 = stream_message(client, token)
            result1 = analyze(events1, f"Legacy Path (run_id={run_id1}, {elapsed1:.1f}s)")

            # 开灰度：需要修改 .env 并重启后端
            print("\n\n!!! 请开启 GATEWAY_MAIN_CHAIN_ENABLED=true 并重启后端，然后按 Enter 继续...")
            input()

            token = login(client)
            run_id2, events2, elapsed2 = stream_message(client, token)
            result2 = analyze(events2, f"Gateway Path (run_id={run_id2}, {elapsed2:.1f}s)")

            print(f"\n{'='*60}")
            print("  COMPARISON")
            print(f"{'='*60}")
            for key in result1:
                v1, v2 = result1[key], result2[key]
                match = "MATCH" if v1 == v2 else "DIFF"
                print(f"  {key}: legacy={v1} gateway={v2} [{match}]")
        else:
            gw_enabled = os.getenv("GATEWAY_MAIN_CHAIN_ENABLED", "").lower() in ("true", "1", "yes")
            label = "Gateway" if gw_enabled else "Legacy"
            run_id, events, elapsed = stream_message(client, token)
            analyze(events, f"{label} Path (run_id={run_id}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
