"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../portal/admin/admin-ui.js");

test("authHeaders only sends configured credentials", () => {
  assert.deepEqual(ui.authHeaders("", ""), {});
  assert.deepEqual(ui.authHeaders("secret", "tenant-a"), {
    "X-API-Key": "secret",
    "X-Tenant-ID": "tenant-a",
  });
});

test("buildOperationPayload normalizes admin operation flags", () => {
  assert.deepEqual(ui.buildOperationPayload({ dryRun: true }), {
    dry_run: true,
    activate: true,
    apply_retention: false,
    force: false,
  });
  assert.deepEqual(ui.buildOperationPayload({ applyRetention: true, activate: false }), {
    dry_run: false,
    activate: false,
    apply_retention: true,
    force: false,
  });
});

test("auditLabel and parseError produce readable messages", () => {
  assert.equal(ui.auditLabel("model_rollback", "success"), "模型回滚 / 成功");
  assert.equal(ui.parseError({ detail: "forbidden" }, 403), "forbidden");
  assert.equal(ui.parseError("bad request", 400), "bad request");
});
