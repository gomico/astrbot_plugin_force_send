"use strict";

const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

async function init() {
  try {
    if (!bridge) {
      throw new Error("AstrBotPluginPage bridge 未注入，请通过 AstrBot 插件 Pages 打开本页面");
    }
    await bridge.ready();
  } catch (err) {
    showFatal("页面初始化失败: " + (err.message || err));
    return;
  }

  $("syncBtn").addEventListener("click", onSync);
  await loadJobs();
}

// -------- API 调用 --------

async function loadJobs() {
  try {
    const data = await bridge.apiGet("jobs");
    renderJobs(data);
  } catch (err) {
    showError("加载任务列表失败: " + (err.message || err));
    showTableMessage("加载任务列表失败，请查看上方错误信息");
  }
}

async function onSync() {
  const btn = $("syncBtn");
  btn.disabled = true;
  btn.textContent = "同步中...";
  hideToast();

  try {
    const result = await bridge.apiPost("sync", {});
    const stats = result.stats || {};
    showSuccess(
      `同步完成。新增 ${stats.added || 0}，更新 ${stats.updated || 0}，` +
      `移除 ${stats.removed || 0}`
    );
    await loadJobs();
  } catch (err) {
    showError("同步失败: " + (err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = "立即同步";
  }
}

async function toggleForceSend(jobId, checked) {
  try {
    hideToast();
    await bridge.apiPost(`jobs/${encodeURIComponent(jobId)}/force-send`, {
      force_send: checked,
    });
    showSuccess(checked ? "已开启强制发送" : "已关闭强制发送");
  } catch (err) {
    showError("操作失败: " + (err.message || err));
    await loadJobs();
  }
}

// -------- 渲染 --------

function renderJobs(data) {
  const tbody = $("jobTableBody");
  const jobs = data.jobs || [];

  $("lastSyncAt").textContent = data.last_sync_at || "-";
  $("jobCount").textContent = jobs.length;

  if (jobs.length === 0) {
    showTableMessage("暂无任务，请先同步");
    return;
  }

  tbody.innerHTML = jobs
    .map((job) => renderRow(job))
    .join("");

  jobs.forEach((job) => {
    const toggle = document.getElementById(`toggle-${job.job_id}`);
    if (toggle) {
      toggle.addEventListener("change", function () {
        toggleForceSend(job.job_id, this.checked);
      });
    }
  });
}

function renderRow(job) {
  const toggleId = `toggle-${job.job_id}`;
  const disabled = !job.can_force_send;
  const checked = job.force_send && !disabled;

  const sessionDisplay = job.session
    ? escapeHtml(job.session.length > 30 ? job.session.slice(0, 28) + "..." : job.session)
    : '<span class="muted">无会话</span>';

  const resultHtml = renderLastResult(job.last_result);

  const nextRun = job.next_run_time
    ? formatTime(job.next_run_time)
    : "-";

  return `
      <tr>
        <td class="col-name" title="${escapeHtml(job.description)}">${escapeHtml(job.name || "-")}</td>
        <td class="col-cron"><code>${escapeHtml(job.cron_expression || "-")}</code></td>
        <td class="col-enabled">${job.enabled ? "是" : "否"}</td>
        <td class="col-next">${nextRun}</td>
        <td class="col-session">${sessionDisplay}</td>
        <td class="col-toggle">
          <label class="switch ${disabled ? "disabled" : ""}">
            <input type="checkbox" id="${toggleId}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}>
            <span class="slider round"></span>
          </label>
          ${disabled ? '<div class="toggle-hint">缺少投递会话</div>' : ""}
        </td>
        <td class="col-result">${resultHtml}</td>
      </tr>
    `;
}

function renderLastResult(result) {
  if (!result) {
    return '<span class="muted">无记录</span>';
  }
  if (result.skipped) {
    return `<span class="badge badge-warn">跳过 (${escapeHtml(result.reason)})</span>`;
  }
  if (result.success) {
    return `<span class="badge badge-success">成功 (${result.attempts} 次)</span>`;
  }
  return `<span class="badge badge-error">失败 (${result.attempts} 次)</span>`;
}

// -------- 工具函数 --------

function formatTime(isoStr) {
  if (!isoStr) return "-";
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    const pad = (n) => String(n).padStart(2, "0");
    return (
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}`
    );
  } catch {
    return isoStr;
  }
}

function escapeHtml(str) {
  if (!str) return str;
  const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(str).replace(/[&<>"']/g, (c) => map[c]);
}

function showError(msg) {
  const toast = $("errorToast");
  toast.textContent = msg;
  toast.classList.remove("hidden");
  $("successToast").classList.add("hidden");
  setTimeout(() => toast.classList.add("hidden"), 5000);
}

function showSuccess(msg) {
  const toast = $("successToast");
  toast.textContent = msg;
  toast.classList.remove("hidden");
  $("errorToast").classList.add("hidden");
  setTimeout(() => toast.classList.add("hidden"), 3000);
}

function hideToast() {
  $("errorToast").classList.add("hidden");
  $("successToast").classList.add("hidden");
}

function showTableMessage(message) {
  $("jobTableBody").innerHTML =
    `<tr><td colspan="7" class="loading">${escapeHtml(message)}</td></tr>`;
}

function showFatal(message) {
  showError(message);
  showTableMessage(message);
  const btn = $("syncBtn");
  if (btn) {
    btn.disabled = true;
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
