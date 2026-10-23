"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const ui = require("../portal/nesting/portal-ui.js");

test("stageLabel returns Chinese labels for workflow and special stages", () => {
  assert.equal(ui.stageLabel("uploaded"), "已上传");
  assert.equal(ui.stageLabel("audited"), "审图确认");
  assert.equal(ui.stageLabel("nested_reported"), "排板报告");
  assert.equal(ui.stageLabel("postprocess_confirmed"), "后处理确认");
  assert.equal(ui.stageLabel("completed"), "完成");
  assert.equal(ui.stageLabel("blocked"), "已阻塞");
  assert.equal(ui.stageLabel("cancelled"), "已取消");
  assert.equal(ui.stageLabel("unknown"), "unknown");
});

test("latestJobStatus and latestJobId find the newest message with a job id", () => {
  const messages = [
    { role: "user", content: "开始" },
    { role: "assistant", content: "任务1", result: { job_id: "job-1", stage: "audited" } },
    { role: "assistant", content: "任务2", result: { job_id: "job-2", stage: "nested_reported" } },
  ];

  assert.equal(ui.latestJobStatus(messages).job_id, "job-2");
  assert.equal(ui.latestJobId(messages), "job-2");
  assert.equal(ui.latestJobStatus([]), null);
  assert.equal(ui.latestJobId([]), null);
});

test("actionAvailability disables actions without a job", () => {
  const availability = ui.actionAvailability(null, false);
  assert.equal(availability.status.disabled, true);
  assert.equal(availability.confirm.disabled, true);
  assert.equal(availability.run.disabled, true);
  assert.equal(availability.review.disabled, true);
  assert.equal(availability.cancel.disabled, true);
});

test("actionAvailability disables everything while busy", () => {
  const status = { job_id: "job-1", stage: "audited", confirmation_required: true };
  const availability = ui.actionAvailability(status, true);
  for (const action of Object.values(availability)) {
    assert.equal(action.disabled, true);
  }
});

test("actionAvailability enables confirm at gate stages and review after nesting", () => {
  const audited = { job_id: "job-1", stage: "audited", confirmation_required: true };
  assert.equal(ui.actionAvailability(audited).confirm.disabled, false);
  assert.equal(ui.actionAvailability(audited).review.disabled, true);

  const nested = { job_id: "job-1", stage: "nested_reported", confirmation_required: true };
  assert.equal(ui.actionAvailability(nested).confirm.disabled, false);
  assert.equal(ui.actionAvailability(nested).review.disabled, false);

  const completed = { job_id: "job-1", stage: "completed", completed: true };
  assert.equal(ui.actionAvailability(completed).cancel.disabled, true);
  assert.equal(ui.actionAvailability(completed).run.disabled, true);
  assert.equal(ui.actionAvailability(completed).review.disabled, false);
});

test("nextAction explains the next user step for each state", () => {
  assert.equal(ui.nextAction(null).type, "create");

  const audited = { job_id: "job-1", stage: "audited", confirmation_required: true };
  assert.equal(ui.nextAction(audited).type, "confirm");

  const nested = { job_id: "job-1", stage: "nested_reported", confirmation_required: true };
  assert.equal(ui.nextAction(nested).type, "confirm");

  const blocked = { job_id: "job-1", stage: "blocked", blocked: true };
  assert.equal(ui.nextAction(blocked).type, "run");

  const completed = { job_id: "job-1", stage: "completed", completed: true };
  assert.equal(ui.nextAction(completed).type, "complete");

  const cancelled = { job_id: "job-1", stage: "cancelled" };
  assert.equal(ui.nextAction(cancelled).type, "cancelled");
});

test("stageProgress marks completed, current and pending steps", () => {
  const status = { job_id: "job-1", stage: "nested_reported", confirmation_required: true };
  const steps = ui.stageProgress(status);
  const nestedIndex = ui.FLOW_STAGES.findIndex((step) => step.id === "nested_reported");

  assert.equal(steps[nestedIndex].state, "current");
  assert.equal(steps[0].state, "done");
  assert.equal(steps[steps.length - 1].state, "pending");

  const completed = ui.stageProgress({ job_id: "job-1", stage: "completed", completed: true });
  assert.equal(completed[completed.length - 1].state, "done");
});

test("jobMetrics extracts key values from tool results", () => {
  const status = {
    job_id: "job-1",
    last_result: {
      tool_results: [
        { tool: "read_dxf", ok: true, output: { parts: 203 } },
        { tool: "audit_drawing", ok: true, output: { issues: 4 } },
        { tool: "nest_parts", ok: true, output: { total_sheets: 50, total_parts: 203, yield_rate: 72.95 } },
        { tool: "postprocess", ok: true, output: { geometry_errors: 0 } },
      ],
    },
  };

  const metrics = ui.jobMetrics(status);
  assert.equal(metrics.parts, 203);
  assert.equal(metrics.audit_issues, 4);
  assert.equal(metrics.total_sheets, 50);
  assert.equal(metrics.total_parts, 203);
  assert.equal(metrics.yield_rate, 72.95);
  assert.equal(metrics.geometry_errors, 0);
});

test("jobArtifacts merges session artifacts and current artifacts", () => {
  const status = {
    job_id: "job-1",
    artifacts: { nested_dxf: "nested.dxf" },
    session_artifacts: { numbered_dxf: "numbered.dxf" },
  };

  const artifacts = ui.jobArtifacts(status);
  assert.equal(artifacts.numbered_dxf, "numbered.dxf");
  assert.equal(artifacts.nested_dxf, "nested.dxf");
});

test("effectiveSheetDimensions handles total and per-side oversize", () => {
  const total = ui.effectiveSheetDimensions({
    usableWidth: 3200,
    usableHeight: 1800,
    oversizeEnabled: true,
    oversizeMode: "total",
    oversizeWidth: 30,
    oversizeHeight: 20,
  });
  assert.equal(total.effectiveWidth, 3230);
  assert.equal(total.effectiveHeight, 1820);

  const perSide = ui.effectiveSheetDimensions({
    usableWidth: 3200,
    usableHeight: 1800,
    oversizeEnabled: true,
    oversizeMode: "per_side",
    oversizeWidth: 30,
    oversizeHeight: 20,
  });
  assert.equal(perSide.effectiveWidth, 3260);
  assert.equal(perSide.effectiveHeight, 1840);
});

test("validateNestingParameters requires bridge kerf and positive sheets", () => {
  const valid = {
    process: "bridge",
    kerf: 5,
    width: 3200,
    height: 1800,
    thickness: 20,
    usableWidth: 3200,
    usableHeight: 1800,
    oversizeEnabled: false,
  };
  assert.deepEqual(ui.validateNestingParameters(valid), []);
  assert.match(
    ui.validateNestingParameters({ ...valid, kerf: 0 })[0],
    /刀厚/,
  );
  assert.match(
    ui.validateNestingParameters({ ...valid, usableWidth: 0 })[0],
    /可利用尺寸/,
  );
});

test("optimizerParameters maps process-specific controls without conflicts", () => {
  const bridge = ui.optimizerParameters({
    process: "bridge",
    rectFast: true,
    lowFillRework: true,
    lowFillThreshold: 0.5,
  });
  assert.equal(bridge.rect_fast, true);
  assert.equal(bridge.compress, true);
  assert.equal(bridge.low_fill_rework, false);
  assert.equal(bridge.pairing, false);
  assert.equal(bridge.candidate_mode, "bbox");

  const waterjet = ui.optimizerParameters({
    process: "waterjet",
    rectFast: true,
    lowFillRework: true,
    lowFillThreshold: 0.45,
    nfpLite: true,
  });
  assert.equal(waterjet.rect_fast, false);
  assert.equal(waterjet.compress, false);
  assert.equal(waterjet.low_fill_rework, true);
  assert.equal(waterjet.low_fill_threshold, 0.45);
  assert.equal(waterjet.pairing, false);
  assert.equal(waterjet.candidate_mode, "nfp_lite");
});

test("validateNestingParameters checks low-fill threshold", () => {
  const valid = {
    process: "waterjet",
    width: 3200,
    height: 1800,
    thickness: 20,
    usableWidth: 3200,
    usableHeight: 1800,
    lowFillRework: true,
    lowFillThreshold: 0.6,
  };
  assert.deepEqual(ui.validateNestingParameters(valid), []);
  assert.match(
    ui.validateNestingParameters({ ...valid, lowFillThreshold: 0 })[0],
    /低填充阈值/,
  );
});

test("artifactLabel exposes numbered DXF and diff revisions", () => {
  assert.equal(ui.artifactLabel("correction_diff_r3"), "纠正差异 v3");
  assert.equal(ui.artifactLabel("numbered_dxf_r2"), "原位DXF v2");
  assert.equal(ui.artifactLabel("numbered_dxf"), "编号原位DXF");
});

test("parseApiError prefers FastAPI detail and handles text payloads", () => {
  assert.equal(ui.parseApiError({ detail: "file too large" }, 400), "file too large");
  assert.equal(ui.parseApiError("bad request", 400), "bad request");
  assert.equal(ui.parseApiError(null, 500), "请求失败：HTTP 500");
});

test("escapeHtml neutralizes markup", () => {
  assert.equal(ui.escapeHtml('<script>alert("x")</script>'), "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;");
});
test("correctionTargetId returns the stable backend target identifier", () => {
  assert.equal(ui.correctionTargetId({ target_id: "panel-1" }), "panel-1");
  assert.equal(ui.correctionTargetId({ hole_id: "handle:H2" }), "handle:H2");
  assert.equal(ui.correctionTargetId({ entity_handle: "H9" }), "handle:H9");
  assert.equal(ui.correctionTargetId({ panel_id: "panel-3", part_id: "3" }), "panel-3");
  assert.equal(ui.correctionTargetId(null), "");
});

test("previewArtifactKey prefers the current numbered revision", () => {
  assert.equal(
    ui.previewArtifactKey({ numbered_dxf: "base.dxf", numbered_dxf_r2: "r2.dxf" }, 2),
    "numbered_dxf_r2",
  );
  assert.equal(
    ui.previewArtifactKey({ numbered_dxf: "base.dxf" }, 2),
    "numbered_dxf",
  );
  assert.equal(ui.previewArtifactKey({}, 0), null);
});
