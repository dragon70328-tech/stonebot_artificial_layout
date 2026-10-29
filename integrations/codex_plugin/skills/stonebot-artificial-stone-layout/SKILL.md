---
name: stonebot-artificial-stone-layout
description: Use Stonebot to audit a DXF drawing, apply structured drawing corrections, configure sheet and process parameters, nest parts, validate the result, and obtain explicit human confirmation before production files are written.
---

# StoneBot-Artificial-Stone-Layout

Use the `stonebot_*` MCP tools for every drawing and nesting action. The
deterministic Stonebot service owns workflow state, geometry checks and
production writes. Do not substitute shell commands, infer geometry from prose,
or claim a successful result without checking the tool's structured response.

## Conversation flow

1. Ask for the local DXF path if it is not already known.
2. Call `stonebot_analyze_drawing`.
3. Report the drawing profile, panel count, hole count, numbered count and
   audit-issue severity totals.
4. If the user reports wrong panels, holes, materials or numbers, call
   `stonebot_list_correction_targets`, map the user's description to stable
   target IDs, then call `stonebot_correct_drawing` with a structured
   `CorrectionPlan`. Prefer `stonebot_rename_panels` for simple numbering
   changes.
5. Never invent target IDs. If an exact target is ambiguous, show candidates
   and ask the user.
6. A correction may return `profile_patches`. These are project-scoped
   candidates, not active rules. Explain the before/after values and evidence.
   Call `stonebot_review_profile_patch` before asking whether to activate it,
   and summarize current/historical replay results.
   Call `stonebot_accept_profile_patch` only after explicit user approval;
   call `stonebot_reject_profile_patch` only when the user rejects it.
   Use `stonebot_list_profile_overrides` to inspect active project overrides
   and `stonebot_rollback_profile_override` only when the user explicitly asks
   to roll back.
7. Before nesting, collect:
   - nominal sheet width and height;
   - thickness;
   - process: bridge, waterjet or laser;
   - kerf for bridge;
   - rotation/pattern constraints;
   - oversize and usable-size policy when present;
   - optimizer preferences such as quick mode, budget and pairing.
8. Call `stonebot_configure_nesting`.
9. Show the normalized parameters before asking for drawing confirmation.
10. Call `stonebot_confirm_drawing` only after the user explicitly confirms the
   recognized drawing and nesting parameters.
11. Report board count, yield rate, elapsed time and validation errors from
    the result. Do not claim that deliverable files have been saved yet.
12. Call `stonebot_confirm_nesting` only after the user accepts the board count.
13. When the user confirms final output, call
    `stonebot_choose_output_directory` and wait for the user to select a folder
    with the native picker. If canceled, stop without finalizing. Then call
    `stonebot_finalize_job` with that exact `output_directory`.
14. The default delivery is exactly two files: the in-place DXF and nested
    DXF. Do not generate a Word report unless the user explicitly requests
    one. For that request, choose a folder with purpose `word_report`, then
    call `stonebot_export_word_report`.

## Safety and state rules

- The server's `stage` and `next_actions` are authoritative.
- `blocked`, `validation_errors` or `errors` means the requested step did not
  complete; explain the cause and stop.
- A tool result with `ok: false` or MCP `isError: true` is not success.
- Do not call `stonebot_cancel_job` unless the user explicitly asks to cancel.
- Never finalize without a user-selected save folder.
- Never infer, reuse or silently default a save folder.
- Do not generate Word reports proactively.
- A drawing confirmation triggers nesting. A nesting confirmation triggers
  postprocessing and hard validation. Finalization copies only the two DXF
  deliverables into the folder selected by the user.
- Do not modify source DXF files or artifact files directly.
- Do not apply a profile-learning change automatically. Corrections generate
  evidence and candidate patches; project activation requires explicit
  approval.

## Response style

Keep the user-facing response concise and manufacturing-oriented:

- what was recognized;
- what blocks the next stage;
- what parameters were normalized;
- what result was produced;
- exact artifact paths when available;
- the one next action the user needs to take.

Use `stonebot_list_drawing_issues` and
`stonebot_list_correction_targets` with pagination when there are many items.
Never dump a large target or issue list into the conversation at once.
