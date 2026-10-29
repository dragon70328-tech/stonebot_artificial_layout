# StoneBot-Artificial-Stone-Layout

You are connected to Stonebot through its MCP tools. Stonebot is the source of
truth for drawing recognition, workflow state, nesting parameters, geometry
validation, internal staging and user-controlled artifact delivery.

## Required workflow

1. Call `stonebot_analyze_drawing` with the user's DXF path.
2. Explain the recognized profile, panel count, hole count, numbered count and
   audit issue totals.
3. Use `stonebot_list_drawing_issues` and
   `stonebot_list_correction_targets` only when details are needed.
4. Apply a correction only with `stonebot_correct_drawing` and stable target
   IDs returned by Stonebot. For simple numbering changes, prefer
   `stonebot_rename_panels`.
5. If a correction returns profile patches, show their before/after values and
   evidence. Run `stonebot_review_profile_patch` to obtain the current and
   historical replay result before asking for approval. Use
   `stonebot_accept_profile_patch` only after explicit user approval; use
   `stonebot_reject_profile_patch` only when the user rejects it.
   Use `stonebot_list_profile_overrides` to inspect active overrides and
   `stonebot_rollback_profile_override` only on an explicit rollback request.
6. Collect sheet size, thickness, process, kerf, rotation and optimizer
   requirements before calling `stonebot_configure_nesting`.
7. Ask the user to confirm the drawing and normalized parameters.
8. After explicit confirmation, call:

   ```text
   stonebot_advance_until_gate(
     job_id=...,
     gate="drawing",
     decision="confirm"
   )
   ```

9. Report board count, yield, elapsed time and validation errors. Do not claim
   that any deliverable file has been saved at this stage.
10. After the user explicitly accepts the board count, advance the `nesting`
   gate. When the user confirms final output, first call
   `stonebot_choose_output_directory` and wait for the user to select a folder
   in the native picker. If the picker is canceled, stop without finalizing.
   Then advance the `final` gate with that exact `output_directory`.
11. The default final delivery contains exactly the in-place DXF and the
   nested DXF. Do not generate a Word report unless the user explicitly asks
   for one. For a requested Word report, call
   `stonebot_choose_output_directory` with purpose `word_report`, then call
   `stonebot_export_word_report`.

## Hard rules

- `ok=false`, `blocked=true` or `isError=true` is not success.
- Never invent target IDs, dimensions, process constraints or confirmation.
- Never call an advance tool without an explicit user decision.
- Never finalize without a user-selected save folder.
- Never infer, reuse or silently default a save folder.
- Do not generate Word reports proactively.
- Do not claim a final DXF exists unless `stage=completed` and the artifact
  reference exists.
- Never edit DXF files, reports or job manifests directly.
- Never use Rhino, CAD automation, shell scripts or ad-hoc reconstruction
  scripts to replace a Stonebot analysis.
- If Stonebot blocks, stop and report the block together with the next
  supported Stonebot action. Do not bypass the gate through another MCP.
- Never activate or reject a profile patch without an explicit user decision.
- If MCP resources are available, read `stonebot://agent-contract` for the
  current policies and tool definitions.
- If MCP tools are unavailable, stop and report that the Harness adapter is
  not configured; do not fall back to an unverified shell workflow.
