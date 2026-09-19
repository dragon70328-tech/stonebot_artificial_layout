(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  if (root) root.StonebotAdminUI = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function authHeaders(apiKey, tenantId) {
    const headers = {};
    if (apiKey) headers["X-API-Key"] = String(apiKey);
    if (tenantId) headers["X-Tenant-ID"] = String(tenantId);
    return headers;
  }

  function buildOperationPayload(options) {
    const value = options || {};
    return {
      dry_run: Boolean(value.dryRun),
      activate: value.activate !== false,
      apply_retention: Boolean(value.applyRetention),
      force: Boolean(value.force),
    };
  }

  function auditLabel(action, status) {
    const actions = {
      operations_dry_run: "运维预演",
      operations_run: "执行运维周期",
      model_rollback: "模型回滚",
    };
    const statuses = { success: "成功", failed: "失败" };
    return `${actions[action] || action || "操作"} / ${statuses[status] || status || "未知"}`;
  }

  function parseError(payload, statusCode) {
    if (payload && typeof payload === "object" && payload.detail) return String(payload.detail);
    if (typeof payload === "string") return payload;
    return `请求失败：HTTP ${statusCode}`;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  return { authHeaders, buildOperationPayload, auditLabel, parseError, escapeHtml };
});
