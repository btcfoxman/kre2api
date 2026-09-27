"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {accounts: [], tasks: [], models: [], loggedIn: false, accountFilter: "enabled", detail: null};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const num = (value) => value == null ? "—" : Number(value).toLocaleString("zh-CN", {maximumFractionDigits: 2});
const stamp = (value) => value ? new Date(Number(value) * 1000).toLocaleString("zh-CN", {hour12: false}) : "—";
const icons = () => { if (window.lucide) window.lucide.createIcons(); };
let toastTimer;
function toast(message, error = false) {
  const box = $("#toast"); box.textContent = message; box.style.borderColor = error ? "var(--red)" : "var(--mint)";
  box.classList.add("show"); clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove("show"), 4500);
}
function show(dialog) { if (!dialog.open) dialog.showModal(); }
async function api(path, options = {}) {
  const response = await fetch(path, {credentials: "same-origin", ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})}});
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/admin/login") {
      state.loggedIn = false; show($("#loginDialog"));
    }
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || {}) || "HTTP " + response.status);
  }
  return body;
}
function renderAccounts() {
  const accounts = state.accounts;
  const enabled = accounts.filter((item) => item.enabled);
  $("#enabledCount").textContent = enabled.length;
  $("#disabledCount").textContent = accounts.length - enabled.length;
  $("#metricAccounts").textContent = enabled.filter((item) => item.project_id && item.cookies_ready && item.login_status === "ready").length + " / " + accounts.length;
  $("#metricBalance").textContent = num(accounts.reduce((sum, item) => sum + Number(item.balance || 0), 0));
  const visible = accounts.filter((item) => item.enabled === (state.accountFilter === "enabled"));
  $("#accountsEmpty").classList.toggle("show", !visible.length);
  $("#accountsBody").innerHTML = visible.map((item) => {
    const ready = item.enabled && item.project_id && item.cookies_ready && item.login_status === "ready";
    const labels = {login_pending:"待登录", logging_in:"登录中", challenge_required:"待网页验证", login_failed:"登录失败", network_error:"网络异常", project_required:"缺少项目", session_required:"缺少会话"};
    const status = !item.enabled ? "已停用" : ready ? "可用" : labels[item.login_status] || "待检测";
    const statusClass = ready ? "active" : "failed";
    return '<tr><td><span class="cell-title"><span class="account-id">#' + item.id + '</span>' + esc(item.name) + '</span><span class="cell-sub" title="' + esc(item.last_error || "") + '">' + esc(item.last_error || "Krea 浏览器会话") + '</span></td>' +
      '<td><span class="badge ' + statusClass + '">' + status + '</span></td><td class="mono">' + num(item.balance) + '</td><td class="mono">' + esc(item.project_id || "—") + '</td><td class="proxy mono" title="' + esc(item.proxy_url || "") + '">' + esc(item.proxy_url || "直连") + '</td><td>' + esc(item.max_concurrency || 1) + '</td><td>' + stamp(item.updated_at) + '</td>' +
      '<td><div class="row-actions">' + (item.has_password ? '<button class="icon-button" data-action="login" data-id="' + item.id + '" title="重试登录"><i data-lucide="log-in"></i></button>' : '') + '<button class="icon-button" data-action="refresh" data-id="' + item.id + '" title="刷新余额"><i data-lucide="refresh-cw"></i></button><button class="icon-button" data-action="edit" data-id="' + item.id + '" title="设置账号"><i data-lucide="pencil"></i></button><button class="icon-button" data-action="toggle" data-id="' + item.id + '" title="' + (item.enabled ? "停用" : "启用") + '"><i data-lucide="' + (item.enabled ? "pause" : "play") + '"></i></button></div></td></tr>';
  }).join("");
  icons();
}
function renderTasks() {
  const tasks = state.tasks;
  $("#taskCount").textContent = tasks.length + " 个任务";
  $("#metricRunning").textContent = tasks.filter((item) => !["succeeded","failed","expired"].includes(item.status)).length;
  $("#metricComplete").textContent = tasks.filter((item) => item.status === "succeeded").length;
  $("#tasksEmpty").classList.toggle("show", !tasks.length);
  $("#tasksBody").innerHTML = tasks.map((item) => {
    const account = state.accounts.find((entry) => entry.id === item.account_id);
    const url = item.data?.[0]?.url;
    const result = url && /^https?:\/\//i.test(url) ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">查看视频 ↗</a>' : esc(item.error || "—");
    return '<tr><td><button class="link-button cell-title mono" data-task="' + esc(item.id) + '">' + esc(item.id) + '</button><span class="cell-sub">' + esc(account?.name || item.account_id) + '</span></td>' +
      '<td><span class="cell-title">' + esc(item.model) + '</span><span class="cell-sub">' + esc(item.duration) + 's · ' + esc(item.resolution) + ' · ' + esc(item.aspect_ratio) + '</span></td>' +
      '<td><span class="badge ' + esc(item.status) + '">' + esc(item.status) + '</span></td><td class="mono">' + num(item.estimated_cost) + ' / ' + num(item.actual_cost) + '</td><td>' + stamp(item.created_at) + '</td><td class="result-links">' + result + '</td></tr>';
  }).join("");
}
function modelOptions() {
  const options = state.models.map((item) => '<option value="' + esc(item.id) + '">' + esc(item.id) + '</option>').join("");
  for (const id of ["taskModel", "costModel"]) {
    $("#" + id).innerHTML = options;
    $("#" + id).value = "sd-2-0";
    updateModel(id === "taskModel" ? "task" : "cost");
  }
  $("#metricModels").textContent = state.models.length;
}
function updateModel(prefix) {
  const model = state.models.find((item) => item.id === $("#" + prefix + "Model").value);
  if (!model) return;
  const preferred = model.id.match(/(480p|1080p|4k|768p)$/)?.[1] || (model.id === "minimax-h3" ? "2k" : "720p");
  $("#" + prefix + "Resolution").innerHTML = model.capabilities.resolutions.map((value) =>
    '<option value="' + esc(value) + '"' + (value === preferred ? " selected" : "") + '>' + esc(value) + '</option>').join("");
  $("#" + prefix + "Ratio").innerHTML = model.capabilities.aspect_ratios.map((value) =>
    '<option value="' + esc(value) + '">' + esc(value) + '</option>').join("");
  const duration = $("#" + prefix + "Form").elements.duration;
  const supported = model.capabilities.durations;
  duration.min = Math.min(...supported); duration.max = Math.max(...supported);
  if (!supported.includes(Number(duration.value))) duration.value = supported[0];
}
function accountOptions() {
  const taskValue = $("#taskAccount").value;
  const costValue = $("#costAccount").value;
  const options = '<option value="">自动选择</option>' + state.accounts.filter((item) => item.enabled && item.cookies_ready && item.project_id && item.login_status === "ready").map((item) =>
    '<option value="' + item.id + '">' + esc(item.name) + ' · ' + num(item.balance) + '</option>').join("");
  $("#taskAccount").innerHTML = options;
  $("#costAccount").innerHTML = options;
  $("#taskAccount").value = taskValue;
  $("#costAccount").value = costValue;
}
async function refresh() {
  if (!state.loggedIn) return;
  try {
    const [accounts, tasks, models] = await Promise.all([
      api("/api/admin/accounts"), api("/api/admin/tasks"),
      state.models.length ? Promise.resolve(state.models) : api("/api/admin/models")]);
    const firstModelLoad = !state.models.length;
    state.accounts = accounts; state.tasks = tasks; state.models = models;
    renderAccounts(); renderTasks(); if (firstModelLoad) modelOptions(); accountOptions();
    $("#refreshStatus").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", {hour12: false});
  } catch (error) { if (state.loggedIn) toast(error.message, true); }
}
async function loadCosts() {
  try {
    const samples = await api("/api/admin/model-costs?limit=100");
    $("#costEmpty").classList.toggle("show", !samples.length);
    $("#costSamples").innerHTML = samples.map((item) => '<tr><td class="mono">' + esc(item.model) + '</td><td>' + esc(item.duration) + 's · ' + esc(item.resolution) + '</td><td>' + esc(item.video_count) + ' 个 / ' + num(item.video_reference_seconds) + 's</td><td class="mono">' + num(item.estimated_cost) + '</td><td class="mono cost-value">' + num(item.actual_cost) + '</td><td>' + stamp(item.observed_at) + '</td></tr>').join("");
  } catch (error) { toast(error.message, true); }
}
function payload(form) {
  const fields = Object.fromEntries(new FormData(form));
  const result = {model: fields.model, duration: Number(fields.duration), resolution: fields.resolution, aspect_ratio: fields.aspect_ratio};
  if (fields.account_id) result.account_id = Number(fields.account_id);
  return result;
}
function docs() {
  const base = location.origin;
  return "KRE2API 视频接口\n\nPOST " + base + "/v1/videos\nAuthorization: Bearer <KR_API_KEY>\nContent-Type: application/json\n\n" +
    JSON.stringify({model:"sd-2-0", prompt:"视频描述", duration:5, resolution:"720p", aspect_ratio:"16:9", generate_audio:true, image_urls:[]}, null, 2) +
    "\n\n查询结果：GET " + base + "/v1/videos/{task_id}\n任务状态：queued / preparing / submitting / running / succeeded / failed / expired\n成功后从 data[0].url 获取视频。\n\n模型能力：GET " + base + "/v1/models\n实时积分预估：POST " + base + "/api/quote\n详细 OpenAPI：" + base + "/docs";
}
function detailTab(key) {
  const item = state.detail;
  const data = item[key];
  $("#auditCode").textContent = JSON.stringify(data ?? {}, null, 2);
  document.querySelectorAll("#auditTabs button").forEach((button) => button.classList.toggle("active", button.dataset.key === key));
}
async function openDetail(id) {
  try {
    const item = await api("/api/admin/tasks/" + encodeURIComponent(id));
    state.detail = item; $("#detailId").textContent = item.id;
    const facts = [["状态", item.status],["模型", item.model],["账号", state.accounts.find((entry) => entry.id === item.account_id)?.name || item.account_id],["时长 / 格式", item.duration + "s · " + item.resolution + " · " + item.aspect_ratio],["预估 / 实际", num(item.estimated_cost) + " / " + num(item.actual_cost)],["创建时间", stamp(item.created_at)]];
    $("#detailFacts").innerHTML = facts.map(([label, value]) => '<div><span>' + esc(label) + '</span><b title="' + esc(value) + '">' + esc(value) + '</b></div>').join("");
    $("#detailPrompt").textContent = item.request?.prompt || "—";
    $("#detailMeta").textContent = item.error || (item.upstream_job_id ? "Krea Job " + item.upstream_job_id : "");
    const media = ["image_urls","video_urls","audio_urls"].flatMap((key) => {
      const source = item.request?.[key];
      const entries = Array.isArray(source) ? source : source ? [source] : [];
      return entries.map((entry, index) => ({key, index, url: typeof entry === "string" ? entry : entry?.url}));
    });
    $("#detailMedia").innerHTML = media.filter((entry) => /^https?:\/\//i.test(entry.url || "")).map((entry) => '<a href="' + esc(entry.url) + '" target="_blank" rel="noopener noreferrer">' + esc(entry.key + " " + (entry.index + 1)) + ' ↗</a>').join("");
    detailTab("request"); show($("#detailDialog"));
  } catch (error) { toast(error.message, true); }
}
function openAccount(account) {
  const form = $("#accountForm"); form.reset();
  form.elements.name.value = account?.name || "";
  form.elements.name.readOnly = !!account;
  form.elements.project_id.value = account?.project_id || "";
  form.elements.max_concurrency.value = account?.max_concurrency || 1;
  form.elements.proxy_url.value = account?.proxy_url || "";
  form.elements.user_agent.value = account?.user_agent || "";
  form.elements.enabled.checked = account?.enabled ?? true;
  $("#accountDialogTitle").textContent = account ? "设置账号 " + account.name : "添加账号";
  show($("#accountDialog"));
}
document.querySelectorAll("dialog .close").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
document.querySelectorAll("#accountTabs button").forEach((button) => button.addEventListener("click", () => {
  state.accountFilter = button.dataset.filter;
  document.querySelectorAll("#accountTabs button").forEach((other) => other.classList.toggle("active", other === button));
  renderAccounts();
}));
$("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault(); $("#loginError").textContent = "";
  try { await api("/api/admin/login", {method:"POST", body:JSON.stringify({token:$("#adminToken").value})});
    $("#adminToken").value = ""; state.loggedIn = true; $("#loginDialog").close(); await refresh();
  } catch (error) { $("#loginError").textContent = error.message; }
});
$("#logoutButton").addEventListener("click", async () => {
  try { await api("/api/admin/logout", {method:"POST"}); } catch {}
  state.loggedIn = false; show($("#loginDialog"));
});
$("#refreshButton").addEventListener("click", refresh);
$("#tasksRefresh").addEventListener("click", refresh);
$("#accountsRefresh").addEventListener("click", async () => {
  const accounts = state.accounts.filter((item) => item.enabled && item.cookies_ready && item.login_status === "ready");
  const outcome = await Promise.allSettled(accounts.map((item) => api("/api/admin/accounts/" + item.id + "/refresh", {method:"POST"})));
  toast(outcome.filter((item) => item.status === "fulfilled").length + " / " + accounts.length + " 个账号已刷新");
  await refresh();
});
$("#accountsBody").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]"); if (!button) return;
  const account = state.accounts.find((item) => item.id === Number(button.dataset.id)); if (!account) return;
  if (button.dataset.action === "edit") return openAccount(account);
  try {
    if (button.dataset.action === "refresh") await api("/api/admin/accounts/" + account.id + "/refresh", {method:"POST"});
    if (button.dataset.action === "login") await api("/api/admin/accounts/" + account.id + "/login", {method:"POST"});
    if (button.dataset.action === "toggle") await api("/api/admin/accounts/" + account.id, {method:"PATCH", body:JSON.stringify({enabled:!account.enabled})});
    await refresh(); toast("账号已更新");
  } catch (error) { toast(error.message, true); }
});
$("#accountForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget, values = Object.fromEntries(new FormData(form));
  let cookies = [];
  try {
    if (values.cookies?.trim()) {
      cookies = JSON.parse(values.cookies);
      if (!Array.isArray(cookies) || !cookies.length) throw new Error("Cookie JSON 必须是非空数组");
    }
    const body = {name:values.name.trim(), project_id:values.project_id.trim(), max_concurrency:Number(values.max_concurrency), proxy_url:values.proxy_url.trim(), user_agent:values.user_agent.trim(), enabled:form.elements.enabled.checked};
    if (cookies.length) body.cookies = cookies;
    await api("/api/admin/accounts", {method:"POST", body:JSON.stringify(body)});
    $("#accountDialog").close(); await refresh(); toast("账号已保存");
  } catch (error) { toast(error.message, true); }
});
$("#addAccountButton").addEventListener("click", () => openAccount(null));
$("#batchButton").addEventListener("click", () => { $("#batchResult").hidden = true; $("#batchResult").textContent = ""; show($("#batchDialog")); });
$("#batchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget, panel = $("#batchResult");
  try {
    const result = await api("/api/accounts/batch-import", {method:"POST", body:JSON.stringify({text:form.elements.text.value, start_login:form.elements.start_login.checked, max_concurrency:Number(form.elements.max_concurrency.value)})});
    const errors = (result.errors || []).map((item) => `第 ${item.line} 行：${item.message}`).join("\n");
    panel.hidden = false; panel.classList.toggle("error", !!errors);
    panel.textContent = `输入 ${result.input_count} · 保存 ${result.count} · 合并 ${result.duplicate_count} · 启动登录 ${result.login_started_count}${errors ? "\n" + errors : ""}`;
    await refresh(); toast(`已导入 ${result.count} 个账号`);
  } catch (error) { panel.hidden = false; panel.classList.add("error"); panel.textContent = error.message; toast(error.message, true); }
});
$("#newTaskButton").addEventListener("click", () => show($("#taskDialog")));
$("#taskModel").addEventListener("change", () => updateModel("task"));
$("#costModel").addEventListener("change", () => updateModel("cost"));
$("#taskForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget, values = Object.fromEntries(new FormData(form));
  const body = {...payload(form), prompt:values.prompt, generate_audio:form.elements.generate_audio.checked};
  for (const key of ["image_urls","video_urls","audio_urls"]) body[key] = (values[key] || "").split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  $("#taskFormMessage").textContent = "正在询价、选择账号并创建任务…";
  try {
    const item = await api("/api/admin/videos", {method:"POST", body:JSON.stringify(body)});
    $("#taskFormMessage").textContent = "已创建 " + item.id;
    await refresh(); toast("任务已提交");
  } catch (error) { $("#taskFormMessage").textContent = error.message; toast(error.message, true); }
});
$("#tasksBody").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-task]");
  if (button) openDetail(button.dataset.task);
});
$("#auditTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-key]");
  if (button && state.detail) detailTab(button.dataset.key);
});
$("#clearTasks").addEventListener("click", async () => {
  if (!confirm("清理所有已结束任务？积分消耗样本会保留。")) return;
  try { const result = await api("/api/admin/tasks/finished", {method:"DELETE"}); await refresh(); toast("已清理 " + result.deleted + " 个任务"); }
  catch (error) { toast(error.message, true); }
});
$("#settingsButton").addEventListener("click", async () => {
  try { const settings = await api("/api/admin/settings"); for (const [key, value] of Object.entries(settings)) $("#settingsForm").elements[key].value = value; show($("#settingsDialog")); }
  catch (error) { toast(error.message, true); }
});
$("#settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget;
  try { await api("/api/admin/settings", {method:"PATCH", body:JSON.stringify({poll_interval_seconds:Number(form.elements.poll_interval_seconds.value), task_timeout_seconds:Number(form.elements.task_timeout_seconds.value)})}); $("#settingsDialog").close(); toast("运行设置已保存"); }
  catch (error) { toast(error.message, true); }
});
$("#integrationDocsButton").addEventListener("click", () => { $("#docsCode").textContent = docs(); show($("#docsDialog")); });
$("#costReferenceButton").addEventListener("click", () => { show($("#costDialog")); loadCosts(); });
$("#costForm").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget, values = Object.fromEntries(new FormData(form));
  const count = Number(values.video_count || 0), seconds = Number(values.video_seconds || 0);
  const body = {...payload(form), prompt:"询价", video_urls:Array.from({length:count}, (_, index) => ({url:"https://example.invalid/reference-" + (index + 1) + ".mp4", duration:count ? seconds / count : 0}))};
  const box = $("#quoteResult"); box.hidden = false; box.textContent = "正在向 Krea 询价…";
  try { const result = await api("/api/admin/quote", {method:"POST", body:JSON.stringify(body)});
    box.textContent = result.accounts.length ? result.accounts.map((item) => item.name + "：预估 " + num(item.estimated_cost) + "，余额 " + num(item.balance) + (item.error ? "，" + item.error : item.eligible ? "，可提交" : "，余额不足")).join("\n") : "暂无可询价账号";
  } catch (error) { box.textContent = error.message; toast(error.message, true); }
});
(async () => {
  icons();
  try { await api("/api/admin/accounts"); state.loggedIn = true; await refresh(); }
  catch { show($("#loginDialog")); }
})();
setInterval(refresh, 15000);
