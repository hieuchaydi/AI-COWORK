// UI Logic for Browser Bridge popup.

const statusBadge = document.getElementById("statusBadge");
const gatewayUrlInput = document.getElementById("gatewayUrl");
const pairingTokenInput = document.getElementById("pairingToken");
const btnAutoPair = document.getElementById("btnAutoPair");
const btnConnect = document.getElementById("btnConnect");
const btnDisconnect = document.getElementById("btnDisconnect");
const jobStatusText = document.getElementById("jobStatusText");
const verificationBox = document.getElementById("verificationBox");
const verificationReason = document.getElementById("verificationReason");
const verificationMsg = document.getElementById("verificationMsg");
const verificationTargetUrl = document.getElementById("verificationTargetUrl");
const btnOpenVerificationTab = document.getElementById("btnOpenVerificationTab");
const btnResumeVerification = document.getElementById("btnResumeVerification");
const tabList = document.getElementById("tabList");

let currentVerification = null;

function renderStatus(state, details) {
  statusBadge.className = "badge " + (state || "disconnected");
  if (state === "connected") {
    statusBadge.textContent = "Connected";
  } else if (state === "busy") {
    statusBadge.textContent = "Busy";
  } else if (state === "awaiting_user_verification") {
    statusBadge.textContent = "Verification";
    statusBadge.className = "badge verification";
  } else {
    statusBadge.textContent = "Disconnected";
  }

  if (details && details.verification) {
    currentVerification = details.verification;
    verificationBox.style.display = "block";
    verificationMsg.textContent = `Yêu cầu xác minh tại: ${details.verification.url || details.verification.hostname || "tab hiện tại"}`;
    if (verificationReason) {
      verificationReason.textContent = details.verification.reason || "Lý do: Shopee yêu cầu giải CAPTCHA hoặc xác minh danh tính tài khoản";
    }
    const targetUrl = details.verification.url || details.verification.hostname || "";
    if (verificationTargetUrl) {
      verificationTargetUrl.textContent = targetUrl ? `URL cần mở: ${targetUrl}` : "";
    }
    if (btnOpenVerificationTab) {
      btnOpenVerificationTab.style.display = (targetUrl || details.verification.tab_id) ? "block" : "none";
    }
  } else {
    currentVerification = null;
    verificationBox.style.display = "none";
  }

  if (details && details.currentJob) {
    jobStatusText.textContent = `Job: ${details.currentJob.id} (${details.currentJob.stage || "running"})`;
  } else {
    jobStatusText.textContent = "No job currently running";
  }
}

let configLoaded = false;

async function loadConfig() {
  chrome.storage.local.get(["gatewayUrl", "pairingToken", "extensionState", "verificationInfo", "currentJob", "lastConnectionError"], (res) => {
    if (!configLoaded) {
      gatewayUrlInput.value = res.gatewayUrl || "ws://127.0.0.1:8766/browser/v1/ws";
      pairingTokenInput.value = res.pairingToken || "";
      configLoaded = true;
    }
    document.getElementById("connectionError").textContent = res.lastConnectionError || "";
    renderStatus(res.extensionState, { verification: res.verificationInfo, currentJob: res.currentJob });
  });
  loadTabs();
}

async function loadTabs() {
  try {
    const tabs = await chrome.tabs.query({});
    tabList.innerHTML = "";
    if (tabs.length === 0) {
      tabList.innerHTML = '<li class="tab-item text-muted">No tabs open</li>';
      return;
    }
    tabs.slice(0, 8).forEach((tab) => {
      const li = document.createElement("li");
      li.className = "tab-item";
      const title = document.createElement("span");
      title.textContent = (tab.title || tab.url || `Tab ${tab.id}`).slice(0, 32);
      title.title = tab.url || "";
      const idSpan = document.createElement("span");
      idSpan.className = "text-muted";
      idSpan.textContent = `id:${tab.id}`;
      li.appendChild(title);
      li.appendChild(idSpan);
      tabList.appendChild(li);
    });
  } catch (e) {
    tabList.innerHTML = `<li class="tab-item text-muted">Error loading tabs: ${e.message}</li>`;
  }
}

async function changeConnection(action) {
  try {
    const response = await chrome.runtime.sendMessage({
      action, url: gatewayUrlInput.value.trim(), token: pairingTokenInput.value.trim(),
    });
    if (!response || !response.ok) throw new Error(response?.error || "Extension worker không phản hồi");
    await loadConfig();
  } catch (error) {
    document.getElementById("connectionError").textContent = error.message;
  }
}

btnAutoPair.addEventListener("click", () => changeConnection("connect"));
btnConnect.addEventListener("click", () => changeConnection("connect"));
btnDisconnect.addEventListener("click", () => changeConnection("disconnect"));

if (btnOpenVerificationTab) {
  btnOpenVerificationTab.addEventListener("click", async () => {
    if (!currentVerification) return;
    const tabId = currentVerification.tab_id;
    const url = currentVerification.url;
    if (tabId) {
      try {
        await chrome.tabs.update(tabId, { active: true });
        return;
      } catch {}
    }
    if (url) {
      chrome.tabs.create({ url, active: true });
    }
  });
}

btnResumeVerification.addEventListener("click", () => {
  const jobId = currentVerification?.job_id;
  chrome.runtime.sendMessage({ action: "resumeVerification", jobId });
  verificationBox.style.display = "none";
  if (jobStatusText) {
    jobStatusText.textContent = "Đã xác nhận xác minh, đang tiếp tục cào...";
  }
});

// Periodic refresh
loadConfig();
setInterval(loadConfig, 2000);

