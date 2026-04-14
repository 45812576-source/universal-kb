# Skill Description Generator Design

## Goal

Replace the current placeholder-style fallback description with a controlled generator that produces a publishable `description` based on the skill's actual context, while keeping the existing governance flow, staged edit format, and one-click adoption path unchanged.

## Scope

- In scope:
  - Generate a better fallback `description` when preflight detects `missing_description`.
  - Use only local skill context already available in backend models.
  - Keep output deterministic enough for governance and tests.
- Out of scope:
  - Rebuilding the full Skill Studio editor prompt around a writing skill.
  - Introducing a new interactive writing workflow.
  - Replacing existing staged-edit adoption mechanics.

## Constraints

- Output must remain stable enough that preflight remediation does not fluctuate between runs.
- Generated text should be concise and reusable in list/search/review contexts.
- The generator should not require a separate assist-skill runtime dependency.

## Chosen Approach

Use a rule-based description generator in backend governance:

1. Read these inputs from `Skill` and latest prompt context already present in governance:
   - `skill.name`
   - `description`
   - latest `system_prompt`
   - `knowledge_tags`
   - `data_queries`
   - `source_files`
   - bound `tools`
2. Infer a few semantic facets:
   - primary scenario
   - likely inputs
   - output style
   - capability sources (knowledge/data/tools)
3. Render a short deterministic Chinese description using fixed sentence templates.

This is intentionally not a generic writing model call. The goal is governance-grade output, not open-ended copywriting.

## Rendering Rules

- Length target: roughly 35-90 Chinese characters.
- Preferred structure:
  - scene
  - evidence/input source
  - output/result
- Example shape:
  - `用于 X 场景，结合 Y 与 Z，输出结构化结论和下一步建议。`
- If signal is weak:
  - fall back to a minimal but still specific sentence, not the current generic placeholder.

## Data Heuristics

- If prompt contains explicit role or scenario wording, use that first.
- If there are `knowledge_tags`, mention knowledge support.
- If there are `data_queries`, mention data table analysis/query support.
- If there are bound tools, mention tool-assisted execution or retrieval.
- If source files include `knowledge-base`, `reference`, or `example`, treat them as domain support signals.

## Integration Point

- Replace `_default_description(skill)` in `backend/app/services/preflight_governance.py`
- Keep `missing_description` remediation as:
  - `target_type="metadata"`
  - same staged edit mechanism
  - same one-click adopt flow

## Testing

- Add/extend tests for:
  - no context -> concise fallback
  - knowledge-only skill -> mentions knowledge support
  - data-query skill -> mentions data/table analysis
  - tool-bound skill -> mentions tool support
  - existing description still untouched by this generator path

## Risks

- Overfitting to prompt keywords may create awkward wording.
- Excessively dynamic phrasing would reduce remediation stability.

## Mitigation

- Keep phrase inventory small and deterministic.
- Prefer composable templates over freeform generation.
- Cover the main combinations with tests.
