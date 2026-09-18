(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  }
  if (root) {
    root.StonebotUI = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const FLOW_STAGES = [
    { id: "uploaded", label: "已上传", description: "接收 DXF" },
    { id: "analyzed", label: "画像分析", description: "分析图纸特征" },
    { id: "profile_matched", label: "匹配画像", description: "匹配图纸画像" },
    { id: "read", label: "读取图形", description: "提取面板与孔洞" },
    { id: "audited", label: "审图确认", description: "确认识别结果" },
    { id: "numbering_confirmed", label: "编号确认", description: "编号已确认" },
    { id: "nested_reported", label: "排板报告", description: "确认板数" },
    { id: "postprocess_confirmed", label: "后处理确认", description: "确认推边与间距" },
    { id: "completed", label: "完成", description: "产物已生成" },
  ];

  const SPECIAL_STAGES = {
    blocked: { id: "blocked", label: "已阻塞", description: "任务中断，可继续运行" },
    cancelled: { id: "cancelled", label: "已取消", description: "任务已取消" },
  };

  const GATE_STAGES = new Set(["audited", "nested_reported", "postprocess_confirmed"]);
  const TERMINAL_STAGES = new Set(["completed", "cancelled"]);
  const REVIEW_STAGES = new Set(["nested_reported", "postprocess_confirmed", "completed"]);
  const ARTIFACT_LABELS = {
    numbered_dxf: "编号原位DXF",
    nested_dxf: "排板DXF",
    report: "排板报告",
    check: "编号检查",
    reviewed_dxf: "回看排板DXF",
    review_report: "回看报告",
  };

  function stageMeta(stage) {
    const value = String(stage || "");
    return (
      FLOW_STAGES.find((item) => item.id === value)
      || SPECIAL_STAGES[value]
      || { id: value, label: value || "未知阶段", description: "" }
    );
  }

  function stageLabel(stage) {
    return stageMeta(stage).label;
  }

  function stageDescription(stage) {
    return stageMeta(stage).description;
  }

  function latestJobStatus(messages) {
    const items = Array.isArray(messages) ? messages : [];
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const result = items[index] && items[index].result;
      if (result && result.job_id) return result;
    }
    return null;
  }

  function latestJobId(messages) {
    const status = latestJobStatus(messages);
    return status ? status.job_id : null;
  }

  function actionAvailability(status, busy = false) {
    const hasJob = Boolean(status && status.job_id);
    const stage = status ? status.stage : null;
    const terminal = Boolean(stage && TERMINAL_STAGES.has(stage));
    const waiting = Boolean(status && status.confirmation_required === true);
    const reviewReady = Boolean(stage && REVIEW_STAGES.has(stage));

    return {
      status: {
        disabled: Boolean(busy || !hasJob),
        reason: busy
          ? "请求处理中，请稍候"
          : (hasJob ? "查看任务状态与产物" : "还没有任务，先上传 DXF 并发送排板指令"),
      },
      confirm: {
        disabled: Boolean(busy || !waiting),
        reason: busy
          ? "请求处理中，请稍候"
          : (waiting ? "确认当前步骤并继续" : "当前阶段没有等待确认的操作"),
      },
      run: {
        disabled: Boolean(busy || !hasJob || terminal || waiting),
        reason: busy
          ? "请求处理中，请稍候"
          : (!hasJob ? "还没有任务" : waiting ? "当前等待确认，请点击“确认下一步”" : terminal ? "任务已结束，无需继续" : "继续运行后台任务"),
      },
      review: {
        disabled: Boolean(busy || !hasJob || !reviewReady),
        reason: busy
          ? "请求处理中，请稍候"
          : (!hasJob ? "还没有任务" : reviewReady ? "尝试回看并减少板数" : "排板报告生成后才能回看减板"),
      },
      cancel: {
        disabled: Boolean(busy || !hasJob || terminal),
        reason: busy
          ? "请求处理中，请稍候"
          : (!hasJob ? "还没有任务" : terminal ? "任务已结束，无法取消" : "取消当前任务"),
      },
    };
  }

  function nextAction(status) {
    if (!status || !status.job_id) {
      return {
        type: "create",
        text: "上传 DXF，然后发送排板指令",
        hint: "选择 DXF 文件后，在输入框输入大板尺寸，例如：3200x1800 排板",
      };
    }
    if (status.stage === "completed") {
      return { type: "complete", text: "任务已完成", hint: "可下载排板 DXF 与报告，或回看减板" };
    }
    if (status.stage === "cancelled") {
      return { type: "cancelled", text: "任务已取消", hint: "如需重做，请新建会话并重新上传 DXF" };
    }
    if (status.blocked) {
      return { type: "run", text: "继续运行任务", hint: "任务已阻塞，可点击继续运行尝试恢复" };
    }
    if (status.confirmation_required) {
      const hints = {
        audited: "请核对编号原位图，确认识别无误后继续排板",
        nested_reported: "请核对板数与排板结果，确认后进入后处理",
        postprocess_confirmed: "请核对后处理结果，确认后生成最终 DXF",
      };
      return {
        type: "confirm",
        text: "确认下一步",
        hint: hints[status.stage] || "请核对当前步骤，确认后继续",
      };
    }
    return { type: "wait", text: "后台处理中", hint: "任务正在后台执行，可点击“查看状态”刷新" };
  }

  function jobStatusText(status) {
    if (!status) return "尚未创建任务";
    const label = stageLabel(status.stage);
    if (status.completed) return `已完成：${label}`;
    if (status.cancelled) return `已取消：${label}`;
    if (status.blocked) return `已阻塞：${label}`;
    if (status.confirmation_required) return `等待确认：${label}`;
    return `处理中：${label}`;
  }

  function stageProgress(status) {
    if (!status) {
      return FLOW_STAGES.map((step) => ({ ...step, state: "pending" }));
    }
    if (SPECIAL_STAGES[status.stage]) {
      return FLOW_STAGES.map((step) => ({ ...step, state: "pending" }));
    }
    const currentIndex = FLOW_STAGES.findIndex((step) => step.id === status.stage);
    return FLOW_STAGES.map((step, index) => {
      let state = "pending";
      if (currentIndex >= 0 && index < currentIndex) state = "done";
      if (currentIndex >= 0 && index === currentIndex) {
        state = status.completed ? "done" : "current";
      }
      return { ...step, state };
    });
  }

  function jobMetrics(status) {
    const metrics = {};
    const toolResults = (
      status
      && status.last_result
      && Array.isArray(status.last_result.tool_results)
    )
      ? status.last_result.tool_results
      : [];

    for (const item of toolResults) {
      if (!item || item.ok === false) continue;
      const output = item.output || {};
      if (item.tool === "read_dxf" && output.parts !== undefined) {
        metrics.parts = output.parts;
      }
      if (item.tool === "audit_drawing" && output.issues !== undefined) {
        metrics.audit_issues = output.issues;
      }
      if (item.tool === "nest_parts") {
        if (output.total_sheets !== undefined) metrics.total_sheets = output.total_sheets;
        if (output.total_parts !== undefined) metrics.total_parts = output.total_parts;
        if (output.yield_rate !== undefined) metrics.yield_rate = output.yield_rate;
        if (output.elapsed_seconds !== undefined) metrics.elapsed_seconds = output.elapsed_seconds;
        if (output.template !== undefined) metrics.template = output.template;
      }
      if (item.tool === "postprocess" && output.geometry_errors !== undefined) {
        metrics.geometry_errors = output.geometry_errors;
      }
    }
    return metrics;
  }

  function jobArtifacts(status) {
    if (!status) return {};
    return { ...(status.session_artifacts || {}), ...(status.artifacts || {}) };
  }

  function artifactLabel(key) {
    return ARTIFACT_LABELS[key] || key;
  }

  function effectiveSheetDimensions(input) {
    const value = input || {};
    const usableWidth = Number(value.usableWidth) || 0;
    const usableHeight = Number(value.usableHeight) || 0;
    const oversizeWidth = Number(value.oversizeWidth) || 0;
    const oversizeHeight = Number(value.oversizeHeight) || 0;
    const enabled = Boolean(value.oversizeEnabled);
    const multiplier = enabled && value.oversizeMode === "per_side" ? 2 : 1;
    const addedWidth = enabled ? oversizeWidth * multiplier : 0;
    const addedHeight = enabled ? oversizeHeight * multiplier : 0;
    return {
      addedWidth,
      addedHeight,
      effectiveWidth: usableWidth + addedWidth,
      effectiveHeight: usableHeight + addedHeight,
    };
  }

  function validateNestingParameters(input) {
    const value = input || {};
    const errors = [];
    if (!(Number(value.width) > 0) || !(Number(value.height) > 0)) {
      errors.push("大板标称尺寸必须大于 0");
    }
    if (!(Number(value.thickness) > 0)) {
      errors.push("大板厚度必须大于 0");
    }
    if (!(Number(value.usableWidth) > 0) || !(Number(value.usableHeight) > 0)) {
      errors.push("大板可利用尺寸必须大于 0");
    }
    if (value.oversizeEnabled) {
      if (Number(value.oversizeWidth) < 0 || Number(value.oversizeHeight) < 0) {
        errors.push("让尺不能小于 0");
      }
    }
    if (value.process === "bridge" && !(Number(value.kerf) > 0)) {
      errors.push("桥切必须输入大于 0 的刀厚或有效刀缝");
    }
    return errors;
  }

  function isDxfFile(name) {
    return typeof name === "string" && name.toLowerCase().endsWith(".dxf");
  }

  function formatAttachmentPath(path) {
    if (!path) return "";
    const parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || String(path);
  }

  function parseApiError(payload, statusCode) {
    if (payload && typeof payload === "object" && payload.detail) {
      return String(payload.detail);
    }
    if (payload && typeof payload !== "object") {
      return String(payload);
    }
    return `请求失败：HTTP ${statusCode}`;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  return {
    FLOW_STAGES,
    SPECIAL_STAGES,
    GATE_STAGES,
    TERMINAL_STAGES,
    REVIEW_STAGES,
    ARTIFACT_LABELS,
    stageMeta,
    stageLabel,
    stageDescription,
    latestJobStatus,
    latestJobId,
    actionAvailability,
    nextAction,
    jobStatusText,
    stageProgress,
    jobMetrics,
    jobArtifacts,
    artifactLabel,
    effectiveSheetDimensions,
    validateNestingParameters,
    isDxfFile,
    formatAttachmentPath,
    parseApiError,
    escapeHtml,
  };
});
