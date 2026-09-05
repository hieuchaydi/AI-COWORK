// UI Logic for Browser Bridge popup.

const statusBadge = document.getElementById("statusBadge");
const gatewayUrlInput = document.getElementById("gatewayUrl");
const pairingTokenInput = document.getElementById("pairingToken");
const btnAutoPair = document.getElementById("btnAutoPair");
const btnConnect = document.getElementById("btnConnect");
const btnDisconnect = document.getElementById("btnDisconnect");
const jobStatusText = document.getElementById("jobStatusText");
const verificationBox = document.getElementById("verificationBox");
const verificationMsg = document.getElementById("verificationMsg");
const btnResumeVerification = document.getElementById("btnResumeVerification");
const tabList = document.getElementById("tabList");

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
    verificationBox.style.display = "block";
    verificationMsg.textContent = `Yêu cầu xác minh tại: ${details.verification.url || details.verification.hostname || "tab hiện tại"}`;
  } else {
    verificationBox.style.display = "none";
  }

  if (details && details.currentJob) {
    jobStatusText.textContent = `Job: ${details.currentJob.id} (${details.currentJob.stage || "running"})`;
  } else {
    jobStatusText.textContent = "No job currently running";
  }
}

async function loadConfig() {
  chrome.storage.local.get(["gatewayUrl", "pairingToken", "extensionState", "verificationInfo", "currentJob"], (res) => {
    gatewayUrlInput.value = res.gatewayUrl || "ws://127.0.0.1:8766/browser/v1/ws";
    pairingTokenInput.value = res.pairingToken || "";
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

btnAutoPair.addEventListener("click", async () => {
  try {
    const r = await fetch("http://127.0.0.1:8766/browser/pair", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    if (data.token) {
      pairingTokenInput.value = data.token;
      gatewayUrlInput.value = data.wsUrl || "ws://127.0.0.1:8766/browser/v1/ws";
      chrome.storage.local.set({
        gatewayUrl: gatewayUrlInput.value,
        pairingToken: data.token,
      });
      chrome.runtime.sendMessage({ action: "connect", url: gatewayUrlInput.value, token: data.token });
    }
  } catch (e) {
    alert("Auto-pair failed: " + e.message);
  }
});

btnConnect.addEventListener("click", () => {
  const url = gatewayUrlInput.value.trim();
  const token = pairingTokenInput.value.trim();
  chrome.storage.local.set({ gatewayUrl: url, pairingToken: token });
  chrome.runtime.sendMessage({ action: "connect", url, token });
});

btnDisconnect.addEventListener("click", () => {
  chrome.runtime.sendMessage({ action: "disconnect" });
});

btnResumeVerification.addEventListener("click", () => {
  chrome.runtime.sendMessage({ action: "resumeVerification" });
  verificationBox.style.display = "none";
});

// Periodic refresh
loadConfig();
setInterval(loadConfig, 2000);

