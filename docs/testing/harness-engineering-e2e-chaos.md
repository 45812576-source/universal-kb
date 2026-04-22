# Skill Studio Harness Engineering E2E / Chaos Test Scaffold

This scaffold validates the release gates from `2026-04-22-skill-studio-harness-engineering-test-validation-plan.md` against a live environment.

## Required Environment

Set these variables before running backend or frontend tests:

```bash
export HARNESS_ENGINEERING_E2E=1
export HARNESS_E2E_BASE_URL=http://localhost:8000
export HARNESS_E2E_BACKEND_URL=http://localhost:8000
export HARNESS_E2E_FRONTEND_URL=http://localhost:5023
export HARNESS_E2E_SKILL_ID=<seeded-skill-id>
export HARNESS_E2E_CONVERSATION_ID=<skill-studio-conversation-id>
export HARNESS_E2E_TOKEN=<access-token>
export HARNESS_E2E_MAX_STREAM_EVENTS=2000
```

Alternatively use `HARNESS_E2E_USERNAME` and `HARNESS_E2E_PASSWORD` instead of `HARNESS_E2E_TOKEN`.

For DB-level chaos gates, also set:

```bash
export HARNESS_E2E_DATABASE_URL=mysql+pymysql://user:pass@host:3306/dbname
```

## Backend Gates

Run from `project/universal-kb/backend`:

```bash
pytest tests/harness_engineering -q
```

The backend suite covers:

- live Studio run creation through `/api/conversations/{conv_id}/messages/stream`
- `public_run_id` propagation through stream and replay
- `patch_applied` envelope validation
- `after_sequence` replay gap recovery
- cancel lifecycle and terminal replay state
- consecutive run supersede isolation
- tool handoff and bind-back round trip
- DB `agent_run_events` monotonic sequence and `idempotency_key`
- manual post-restart cold replay gate

## Frontend Gates

Run from `project/le-desk`:

```bash
npm run test:e2e:harness
```

The Playwright suite covers:

- Skill Studio route rendering with an injected live auth token
- queue/session recovery after browser refresh
- frontend proxy preservation of `X-Studio-Run-Id`
- backend replay validation from a frontend-triggered run
- structured external handoff and bind-back API behavior
- old-run replay identity isolation

## Manual Restart Gate

The backend restart gate is intentionally manual so the test runner does not restart developer services.

1. Run any E2E scenario and copy its `public_run_id`.
2. Restart the backend process.
3. Run:

```bash
export HARNESS_ENGINEERING_RESTART_VALIDATION=1
export HARNESS_E2E_EXISTING_RUN_ID=<run-created-before-restart>
pytest tests/harness_engineering/test_skill_studio_harness_chaos.py::test_manual_backend_restart_replay_gate -q
```

## Evidence

Backend tests write SSE and JSON evidence under `HARNESS_E2E_EVIDENCE_DIR` if set. Without it, pytest creates a temporary evidence directory.

Frontend tests write screenshots and traces under `project/le-desk/test-results/harness-engineering*`.

Each evidence package should include:

- environment and timestamp
- `public_run_id`
- live SSE stream
- replay SSE stream
- `studio/session` JSON
- handoff and bind-back responses
- DB event log sample when `HARNESS_E2E_DATABASE_URL` is available
