"use strict";

const assistDialog = $("#browserAssistDialog");
const assistImage = $("#browserAssistImage");
let assistSession = null;

function assistBusy(session, busy) {
  if (assistSession !== session) return;
  session.busy = busy;
  for (const name of ["Login", "Up", "Down", "Refresh", "Type", "Enter", "Complete"]) {
    $("#browserAssist" + name).disabled = busy;
  }
  $("#browserAssistViewport").classList.toggle("busy", busy);
}

function showAssistFrame(session, frame) {
  if (assistSession !== session) return;
  session.frame = frame;
  assistImage.src = frame.image;
  assistImage.hidden = false;
  $("#browserAssistPlaceholder").hidden = true;
  $("#browserAssistStatus").textContent = frame.signed_in
    ? "检测到登录会话 · 请点击「保存登录会话」"
    : frame.challenge_required ? "等待网页验证 · 请点击画面中的验证框"
    : frame.has_login_form ? "登录页面 · 可点击「填写并登录」"
    : "浏览器已连接 · 等待页面加载";
}

async function refreshAssistFrame(session) {
  if (assistSession !== session || session.busy || session.refreshing) return;
  session.refreshing = true;
  try {
    showAssistFrame(session, await api(`/api/admin/accounts/${session.id}/browser`));
  } catch (error) {
    if (assistSession === session) $("#browserAssistStatus").textContent = error.message;
  } finally {
    session.refreshing = false;
  }
}

window.openKreaBrowserAssist = async (id) => {
  if (assistSession) return;
  const session = {id, busy: false, refreshing: false, frame: null, timer: null};
  assistSession = session;
  $("#browserAssistAccount").textContent = `#${id}`;
  $("#browserAssistStatus").textContent = "正在连接账号浏览器…";
  $("#browserAssistPlaceholder").hidden = false;
  assistImage.hidden = true;
  assistDialog.showModal();
  assistBusy(session, true);
  try {
    showAssistFrame(session, await api(`/api/admin/accounts/${id}/browser/open`, {method: "POST"}));
    if (assistSession === session) session.timer = setInterval(() => refreshAssistFrame(session), 2000);
    else await api(`/api/admin/accounts/${id}/browser/close`, {method: "POST"});
  } catch (error) {
    if (assistSession === session) {
      toast(error.message, true);
      assistDialog.close();
    }
  } finally {
    assistBusy(session, false);
  }
};

async function assistAction(action) {
  const session = assistSession;
  if (!session || session.busy || !session.frame) return;
  assistBusy(session, true);
  try {
    const result = await api(`/api/admin/accounts/${session.id}/browser/action`, {
      method: "POST", body: JSON.stringify(action),
    });
    if (result.action_result?.phase === "challenge_required") {
      toast("自动登录等待网页验证，请点击画面中的验证框");
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    assistBusy(session, false);
    setTimeout(() => refreshAssistFrame(session), 500);
  }
}

assistImage.addEventListener("click", event => {
  const rect = assistImage.getBoundingClientRect();
  if (rect.width && rect.height) assistAction({
    action: "click", x: (event.clientX - rect.left) / rect.width,
    y: (event.clientY - rect.top) / rect.height,
  });
});
$("#browserAssistLogin").addEventListener("click", () => assistAction({action: "login"}));
$("#browserAssistUp").addEventListener("click", () => assistAction({action: "scroll", delta_y: -500}));
$("#browserAssistDown").addEventListener("click", () => assistAction({action: "scroll", delta_y: 500}));
$("#browserAssistRefresh").addEventListener("click", () => refreshAssistFrame(assistSession));
$("#browserAssistType").addEventListener("click", () => {
  const input = $("#browserAssistText");
  if (!input.value) return;
  assistAction({action: "type", text: input.value});
  input.value = "";
});
$("#browserAssistEnter").addEventListener("click", () => assistAction({action: "enter"}));
$("#browserAssistComplete").addEventListener("click", async () => {
  const session = assistSession;
  if (!session || session.busy) return;
  assistBusy(session, true);
  try {
    const account = await api(`/api/admin/accounts/${session.id}/browser/complete`, {method: "POST"});
    if (assistSession === session) assistDialog.close();
    toast(account.login_status === "ready"
      ? `账号 #${session.id} 登录会话已保存`
      : `账号 #${session.id} 已登录，请先在 Krea 创建视频项目`);
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    assistBusy(session, false);
  }
});

function closeAssist() {
  const session = assistSession;
  assistSession = null;
  if (!session) return;
  clearInterval(session.timer);
  assistImage.removeAttribute("src");
  assistImage.hidden = true;
  $("#browserAssistText").value = "";
  fetch(`/api/admin/accounts/${session.id}/browser/close`, {
    method: "POST", credentials: "same-origin", keepalive: true,
  }).catch(() => {});
}

assistDialog.addEventListener("close", closeAssist);
window.addEventListener("pagehide", closeAssist);
