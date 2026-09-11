// UI Logic for Browser Bridge popup.

const statusBadge = document.getElementById("statusBadge");
const gatewayUrlInput = document.getElementById("gatewayUrl");
const pairingTokenInput = document.getElementById("pairingToken");
const btnAutoPair = document.getElementById("btnAutoPair");
const btnConnect = document.getElementById("btnConnect");
const btnDisconnect = document.getElementById("btnDisconnect");
const jobStatusText = document.getElementById("jobStatusText");
const jobProgress = document.getElementById("jobProgress");
const jobProgressLabel = document.getElementById("jobProgressLabel");
const jobProgressPercent = document.getElementById("jobProgressPercent");
const jobProgressBar = document.getElementById("jobProgressBar");
const jobProgressMeta = document.getElementById("jobProgressMeta");
const btnExportSnapshot = document.getElementById("btnExportSnapshot");
const jobExportMessage = document.getElementById("jobExportMessage");
const verificationBox = document.getElementById("verificationBox");
const verificationReason = document.getElementById("verificationReason");
const verificationMsg = document.getElementById("verificationMsg");
const verificationTargetUrl = document.getElementById("verificationTargetUrl");
const btnOpenVerificationTab = document.getElementById("btnOpenVerificationTab");
const btnResumeVerification = document.getElementById("btnResumeVerification");
const btnRecheckApi = document.getElementById("btnRecheckApi");
const recheckStatusMsg = document.getElementById("recheckStatusMsg");
const tabList = document.getElementById("tabList");
const resultSection = document.getElementById("resultSection");
const resultSummary = document.getElementById("resultSummary");
const downloadCsv = document.getElementById("downloadCsv");
const downloadZip = document.getElementById("downloadZip");
const openManifest = document.getElementById("openManifest");
const openReport = document.getElementById("openReport");
const zipParts = document.getElementById("zipParts");
const btnDismissResult = document.getElementById("btnDismissResult");

let currentVerification = null;
let currentJobId = null;
let dismissedResultId = "";

function storageGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}

function extractVerificationUrl(input) {
  if (!input) return "";
  if (typeof input === "object") {
    if (typeof input.verification_url === "string" && input.verification_url.includes("/verify/traffic")) return input.verification_url.replace(/[;,.)]+$/, "");
    if (typeof input.target_url === "string" && input.target_url.includes("/verify/traffic")) return input.target_url.replace(/[;,.)]+$/, "");
    if (typeof input.pageUrl === "string" && input.pageUrl.includes("/verify/traffic")) return input.pageUrl.replace(/[;,.)]+$/, "");
    if (typeof input.url === "string" && input.url.includes("/verify/traffic")) return input.url.replace(/[;,.)]+$/, "");
    const str = `${input.reason || ""} ${input.error || ""} ${input.message || ""}`;
    const match = str.match(/https?:\/\/[^\s"'`<>]+verify\/traffic[^\s"'`<>;,)]*/);
    if (match) return match[0].replace(/[;,.)]+$/, "");
  } else if (typeof input === "string") {
    const match = input.match(/https?:\/\/[^\s"'`<>]+verify\/traffic[^\s"'`<>;,)]*/);
    if (match) return match[0].replace(/[;,.)]+$/, "");
  }
  return "";
}

function runtimeStatus() {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ action: "getStatus" }, (response) => {
      // An older service worker may not know this diagnostic action yet.
      if (chrome.runtime.lastError || !response) {
        resolve(null);
        return;
      }
      resolve(response);
    });
  });
}

function gatewayStatusUrl(wsUrl) {
  const target = new URL(wsUrl || "ws://127.0.0.1:8766/browser/v1/ws");
  target.protocol = target.protocol === "wss:" ? "https:" : "http:";
  if (target.port === "8767") target.port = "8766";
  target.pathname = "/browser/status";
  target.search = "";
  target.hash = "";
  return target.toString();
}

function outputUrl(path, wsUrl) {
  if (!path) return "";
  const value = String(path).trim();
  if (!value) return "";
  const base = new URL(gatewayStatusUrl(wsUrl));
  if (!["127.0.0.1", "localhost", "[::1]"].includes(base.hostname)) return "";
  if (/^https?:\/\//i.test(value)) {
    const absolute = new URL(value);
    return ["127.0.0.1", "localhost", "[::1]"].includes(absolute.hostname) ? absolute.toString() : "";
  }
  return new URL(value.startsWith("/") ? value : `/${value}`, base.origin).toString();
}

function setResultLink(element, path, wsUrl) {
  const url = outputUrl(path, wsUrl);
  element.classList.toggle("is-hidden", !url);
  if (url) element.href = url;
  else element.removeAttribute("href");
  return Boolean(url);
}

function completedResultId(result) {
  if (!result) return "";
  return String(result.job || result.job_id || result.csv || result.completedAt || "");
}

function renderCompletedResult(result, wsUrl) {
  if (!result || !result.count || completedResultId(result) === dismissedResultId) {
    resultSection.style.display = "none";
    return;
  }

  resultSection.style.display = "block";
  const completed = result.completedAt ? new Date(result.completedAt).toLocaleString("vi-VN") : "";
  const count = Number(result.count).toLocaleString("vi-VN");
  const expected = Number(result.crawl_summary?.expected) || 0;
  const partial = result.partial === true;
  resultSummary.textContent = partial && expected
    ? `Đã lưu tối đa ${count}/${expected.toLocaleString("vi-VN")} đánh giá (kết quả một phần)${completed ? ` · ${completed}` : ""}`
    : `Đã lưu ${count} đánh giá${completed ? ` · ${completed}` : ""}`;

  setResultLink(downloadCsv, result.csv, wsUrl);
  const allZipUrls = Array.isArray(result.zip_urls) && result.zip_urls.length
    ? result.zip_urls
    : [result.zip_url || result.zip].filter(Boolean);
  setResultLink(downloadZip, allZipUrls[0], wsUrl);
  downloadZip.textContent = allZipUrls.length > 1 ? "Tải ZIP part 01" : "Tải ZIP ảnh/video";
  setResultLink(openManifest, result.manifest, wsUrl);
  setResultLink(openReport, result.report_url || result.report, wsUrl);

  zipParts.innerHTML = "";
  if (allZipUrls.length > 1) {
    allZipUrls.forEach((path, index) => {
      const url = outputUrl(path, wsUrl);
      if (!url) return;
      const link = document.createElement("a");
      link.className = "zip-part-link";
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = `ZIP part ${String(index + 1).padStart(2, "0")}`;
      zipParts.appendChild(link);
    });
  }
}

async function readGatewayStatus(wsUrl) {
  try {
    const response = await fetch(gatewayStatusUrl(wsUrl), { cache: "no-store" });
    if (!response.ok) return { connected: false, error: `HTTP ${response.status}` };
    return await response.json();
  } catch (error) {
    return { connected: false, error: error.message };
  }
}

function renderStatus(state, details) {
  const connectionError = document.getElementById("connectionError");
  if (connectionError && (state === "connected" || state === "busy")) {
    // A transient socket close may leave the previous error in storage after reconnect.
    connectionError.textContent = "";
  }
  statusBadge.className = "badge " + (state || "disconnected");
  if (state === "connected") {
    statusBadge.textContent = "Connected";
  } else if (state === "busy") {
    statusBadge.textContent = "Busy";
  } else if (state === "resuming") {
    statusBadge.textContent = "Resuming";
    statusBadge.className = "badge warning";
  } else if (state === "awaiting_user_verification") {
    statusBadge.textContent = "Verification";
    statusBadge.className = "badge verification";
  } else if (state === "login_required") {
    statusBadge.textContent = "Login Req";
    statusBadge.className = "badge warning";
  } else if (state === "api_blocked") {
    statusBadge.textContent = "Blocked";
    statusBadge.className = "badge error";
  } else if (state === "client_conflict") {
    statusBadge.textContent = "Client khác";
    statusBadge.className = "badge warning";
  } else {
    statusBadge.textContent = "Disconnected";
  }

  const verifTitle = document.getElementById("verificationTitle");
  if (details && details.verification) {
    currentVerification = details.verification;
    verificationBox.style.display = "block";
    const kind = details.verification.kind || "verification";
    const targetUrl = details.verification.verification_url || details.verification.target_url || extractVerificationUrl(details.verification) || details.verification.url || details.verification.hostname || "";
    verificationMsg.textContent = `Yêu cầu xác minh tại: ${targetUrl || "tab hiện tại"}`;
    if (verificationReason) {
      verificationReason.textContent = details.verification.reason || "Lý do: Shopee yêu cầu giải CAPTCHA hoặc xác minh danh tính tài khoản";
    }

    if (kind === "login") {
      verificationBox.className = "alert-box login-box";
      if (verifTitle) verifTitle.textContent = "🔑 Yêu cầu Đăng nhập Shopee";
      verificationMsg.textContent = "Shopee yêu cầu bạn đăng nhập tài khoản trên Chrome. Hãy mở tab Shopee và đăng nhập, sau đó tạo lại yêu cầu cào mới (không thể tự động bypass).";
      if (verificationReason) {
        verificationReason.textContent = details.verification.reason || "Lý do: Shopee trả mã 90309999 / is_login=false (Chưa đăng nhập)";
      }
      if (btnOpenVerificationTab) {
        btnOpenVerificationTab.style.display = "block";
        btnOpenVerificationTab.textContent = "Mở Shopee để đăng nhập";
      }
      if (btnResumeVerification) {
        btnResumeVerification.style.display = "none";
      }
      if (btnRecheckApi) btnRecheckApi.style.display = "none";
      if (recheckStatusMsg) recheckStatusMsg.style.display = "none";
    } else if (kind === "api_blocked") {
      verificationBox.className = "alert-box blocked-box";
      if (verifTitle) verifTitle.textContent = "🚫 Shopee chặn API (HTTP 403 / API Blocked)";
      verificationMsg.textContent = "Shopee đã tạm chặn API đánh giá (HTTP 403). Job đã dừng an toàn (không tự động loop retry). Hãy mở tab Shopee kiểm tra hoặc bấm 'Kiểm tra lại' để thử kết nối API.";
      if (verificationReason) {
        verificationReason.textContent = details.verification.reason || "Lý do: HTTP 403 Forbidden / Access Denied";
      }
      if (btnOpenVerificationTab) {
        btnOpenVerificationTab.style.display = (targetUrl || details.verification.tab_id) ? "block" : "none";
        btnOpenVerificationTab.textContent = "Mở tab Shopee";
      }
      if (btnResumeVerification) {
        btnResumeVerification.style.display = "none";
      }
      if (btnRecheckApi) {
        btnRecheckApi.style.display = "block";
        btnRecheckApi.disabled = false;
        btnRecheckApi.textContent = "Kiểm tra lại";
      }
      if (recheckStatusMsg) {
        recheckStatusMsg.style.display = "none";
        recheckStatusMsg.textContent = "";
      }
    } else {
      // verification (CAPTCHA / challenge)
      verificationBox.className = "alert-box";
      if (verifTitle) verifTitle.textContent = "⚠️ Yêu cầu giải CAPTCHA / Xác minh";
      verificationMsg.textContent = `Yêu cầu xác minh tại: ${targetUrl || "tab Shopee"}. Vui lòng mở tab để kéo thanh trượt / giải CAPTCHA, sau đó bấm Tiếp tục.`;
      if (verificationReason) {
        verificationReason.textContent = details.verification.reason || "Lý do: Shopee yêu cầu thử thách CAPTCHA / xác minh traffic";
      }
      if (btnOpenVerificationTab) {
        btnOpenVerificationTab.style.display = (targetUrl || details.verification.tab_id) ? "block" : "none";
        btnOpenVerificationTab.textContent = "Mở tab Shopee cần xác minh";
      }
      if (btnResumeVerification) {
        btnResumeVerification.style.display = "block";
        btnResumeVerification.textContent = "Tiếp tục sau khi đã giải CAPTCHA";
      }
      if (btnRecheckApi) btnRecheckApi.style.display = "none";
      if (recheckStatusMsg) recheckStatusMsg.style.display = "none";
    }

    if (verificationTargetUrl) {
      verificationTargetUrl.textContent = targetUrl ? `URL cần mở: ${targetUrl}` : "";
    }

    const verificationEvidence = document.getElementById("verificationEvidence");
    const evidenceTitle = document.getElementById("evidenceTitle");
    const evidenceImage = document.getElementById("evidenceImage");
    const evidenceLink = document.getElementById("evidenceLink");
    const screenshot = details.verification.evidence_screenshot || details.verification.evidence_url;
    if (verificationEvidence && evidenceImage && screenshot) {
      verificationEvidence.style.display = "block";
      evidenceImage.src = screenshot;
      if (evidenceTitle) {
        const label = details.verification.evidence_label || "Đã chụp";
        evidenceTitle.textContent = `📸 Bằng chứng CAPTCHA (${label}):`;
      }
      if (evidenceLink) {
        evidenceLink.href = screenshot;
      }
    } else if (verificationEvidence) {
      verificationEvidence.style.display = "none";
    }
  } else {
    const verificationEvidence = document.getElementById("verificationEvidence");
    if (verificationEvidence) verificationEvidence.style.display = "none";
    currentVerification = null;
    verificationBox.style.display = "none";
    if (btnResumeVerification) {
      btnResumeVerification.style.display = "block";
    }
    if (btnRecheckApi) btnRecheckApi.style.display = "none";
    if (recheckStatusMsg) recheckStatusMsg.style.display = "none";
  }

  if (details && details.currentJob) {
    const job = details.currentJob;
    currentJobId = job.id;
    const progress = job._progress || {};
    jobStatusText.textContent = `Job: ${job.id} (${progress.stage || job.stage || "running"})`;
    const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
    const rows = Number(progress.rows);
    const total = Number(progress.total || progress.totalTarget);
    const label = progress.message || "Đang xử lý...";
    jobProgress.style.display = "block";
    jobProgressLabel.textContent = label;
    jobProgressPercent.textContent = `${percent}%`;
    jobProgressBar.style.width = `${percent}%`;
    let metaText = Number.isFinite(rows) && rows > 0
      ? `Đã lấy ${rows.toLocaleString("vi-VN")}${Number.isFinite(total) && total > 0 ? `/${total.toLocaleString("vi-VN")} đánh giá unique` : " đánh giá unique"}`
      : "Đang kết nối và chuẩn bị dữ liệu";

    const scopes = progress.scopes;
    if (scopes && typeof scopes === "object") {
      const scopeSummary = [];
      if (scopes.all && (scopes.all.rows_count > 0 || progress.active_scope === "all")) {
        scopeSummary.push(`Tất cả: ${Number(scopes.all.rows_count || 0).toLocaleString("vi-VN")}`);
      }
      if (scopes.comment && (scopes.comment.rows_count > 0 || progress.active_scope === "comment")) {
        scopeSummary.push(`Bình luận: ${Number(scopes.comment.rows_count || 0).toLocaleString("vi-VN")}`);
      }
      if (scopes.media && (scopes.media.rows_count > 0 || progress.active_scope === "media")) {
        scopeSummary.push(`Ảnh/Video: ${Number(scopes.media.rows_count || 0).toLocaleString("vi-VN")}`);
      }
      if (scopeSummary.length > 0) {
        metaText += ` [${scopeSummary.join(" | ")}]`;
      }
    }

    const uiRef = progress.ui_reference;
    if (uiRef && typeof uiRef === "object") {
      const refParts = [];
      if (uiRef.rating_total) refParts.push(`Tổng: ${uiRef.rating_total.toLocaleString("vi-VN")}`);
      if (uiRef.rcount_with_context) refParts.push(`Bình luận: ${uiRef.rcount_with_context.toLocaleString("vi-VN")}`);
      if (uiRef.rcount_with_media) refParts.push(`Ảnh/Video: ${uiRef.rcount_with_media.toLocaleString("vi-VN")}`);
      if (refParts.length > 0) {
        metaText += ` • Tham chiếu Shopee UI: ${refParts.join(", ")}`;
      }
    }
    jobProgressMeta.textContent = metaText;

    const jobEvidencePreview = document.getElementById("jobEvidencePreview");
    const jobEvidenceTitle = document.getElementById("jobEvidenceTitle");
    const jobEvidenceImg = document.getElementById("jobEvidenceImg");
    const jobEvidenceLink = document.getElementById("jobEvidenceLink");
    const jobScreenshot = progress.evidence_screenshot || progress.evidence_url;
    if (jobEvidencePreview && jobEvidenceImg && jobScreenshot && (!details.verification || !details.verification.evidence_screenshot)) {
      jobEvidencePreview.style.display = "block";
      jobEvidenceImg.src = jobScreenshot;
      if (jobEvidenceTitle) {
        const label = progress.evidence_label || "Kiểm chứng gần nhất";
        jobEvidenceTitle.textContent = `📸 Ảnh kiểm chứng (${label}):`;
      }
      if (jobEvidenceLink) {
        jobEvidenceLink.href = jobScreenshot;
      }
    } else if (jobEvidencePreview) {
      jobEvidencePreview.style.display = "none";
    }
  } else {
    currentJobId = null;
    jobStatusText.textContent = "No job currently running";
    jobProgress.style.display = "none";
    const jobEvidencePreview = document.getElementById("jobEvidencePreview");
    if (jobEvidencePreview) jobEvidencePreview.style.display = "none";
  }
}

async function exportSnapshot() {
  if (!currentJobId) return;
  btnExportSnapshot.disabled = true;
  jobExportMessage.textContent = "Đang tạo ZIP từ checkpoint hiện tại...";
  try {
    const endpoint = new URL("/ingest/export", gatewayStatusUrl(gatewayUrlInput.value));
    endpoint.searchParams.set("id", currentJobId);
    const response = await fetch(endpoint.toString());
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
    jobExportMessage.textContent = `Đã tạo ZIP với ${Number(result.count || 0).toLocaleString("vi-VN")} đánh giá.`;
    renderCompletedResult(result, gatewayUrlInput.value);
  } catch (error) {
    jobExportMessage.textContent = `Không thể xuất ZIP: ${error.message}`;
  } finally {
    btnExportSnapshot.disabled = false;
  }
}

let configLoaded = false;

async function loadConfig() {
  const res = await storageGet([
    "gatewayUrl",
    "pairingToken",
    "extensionState",
    "verificationInfo",
    "currentJob",
    "lastConnectionError",
    "lastCompletedResult",
    "dismissedResultId",
  ]);
  const runtime = await runtimeStatus();
  const gateway = await readGatewayStatus(res.gatewayUrl);
  if (!configLoaded) {
    gatewayUrlInput.value = res.gatewayUrl || "ws://127.0.0.1:8766/browser/v1/ws";
    pairingTokenInput.value = res.pairingToken || "";
    configLoaded = true;
  }

  const state = runtime?.extensionState || res.extensionState;
  const verification = runtime?.verificationInfo ?? res.verificationInfo;
  const currentJob = runtime?.currentJob ?? res.currentJob;
  const completedResult = runtime?.lastCompletedResult ?? res.lastCompletedResult;
  dismissedResultId = res.dismissedResultId || dismissedResultId;
  const connectionError = document.getElementById("connectionError");
  if (state === "client_conflict") {
    const ownerId = runtime?.connectionConflict?.owner?.clientId;
    connectionError.textContent = `Gateway đang do client khác giữ${ownerId ? ` (${ownerId})` : ""}; bấm Connect để takeover có chủ đích.`;
  } else if (runtime && !runtime.connected && gateway.connected) {
    connectionError.textContent = "Gateway đang có client khác kết nối; hãy tắt bản extension/profile trùng rồi bấm Connect.";
  } else if (runtime && runtime.connected && !gateway.connected) {
    connectionError.textContent = "Extension đã mở socket nhưng gateway chưa nhận trạng thái connected.";
  } else {
    connectionError.textContent = runtime?.lastConnectionError || res.lastConnectionError || "";
  }
  renderStatus(state, { verification, currentJob });
  renderCompletedResult(completedResult, gatewayUrlInput.value);
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
      li.style.cursor = "pointer";
      li.title = "Bấm để chuyển tới tab: " + (tab.title || tab.url || "");
      li.addEventListener("click", async () => {
        try {
          await chrome.tabs.update(tab.id, { active: true });
          if (tab.windowId) await chrome.windows.update(tab.windowId, { focused: true });
        } catch {}
      });
      const titleSpan = document.createElement("span");
      titleSpan.className = "tab-title";
      titleSpan.textContent = (tab.title || tab.url || `Tab ${tab.id}`).slice(0, 32);
      titleSpan.title = tab.url || "";
      const idSpan = document.createElement("span");
      idSpan.className = "text-muted";
      idSpan.textContent = `id:${tab.id}`;
      li.appendChild(titleSpan);
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
      action,
      url: gatewayUrlInput.value.trim(),
      token: pairingTokenInput.value.trim(),
      // A manual Connect is the explicit user consent to take over the owner lease.
      takeover: action === "connect",
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
btnExportSnapshot.addEventListener("click", exportSnapshot);
btnDismissResult.addEventListener("click", async () => {
  // Only dismisses the notification card. The generated CSV/ZIP files remain intact.
  const result = (await runtimeStatus())?.lastCompletedResult
    || (await storageGet(["lastCompletedResult"])).lastCompletedResult;
  dismissedResultId = completedResultId(result);
  resultSection.style.display = "none";
  await chrome.storage.local.set({ dismissedResultId });
  await chrome.runtime.sendMessage({ action: "clearCompletedResult" });
});

if (btnOpenVerificationTab) {
  btnOpenVerificationTab.addEventListener("click", async () => {
    if (!currentVerification) return;
    const tabId = currentVerification.tab_id;
    const targetUrl = currentVerification.verification_url || currentVerification.target_url || extractVerificationUrl(currentVerification) || currentVerification.url;
    if (tabId) {
      try {
        let tab = null;
        if (targetUrl && targetUrl.includes("/verify/traffic")) {
          tab = await chrome.tabs.update(tabId, { url: targetUrl, active: true });
        } else {
          tab = await chrome.tabs.update(tabId, { active: true });
        }
        if (tab?.windowId) {
          await chrome.windows.update(tab.windowId, { focused: true });
        }
        return;
      } catch {}
    }
    if (targetUrl) {
      chrome.tabs.create({ url: targetUrl, active: true }, (tab) => {
        if (tab?.windowId) chrome.windows.update(tab.windowId, { focused: true });
      });
    }
  });
}

btnResumeVerification.addEventListener("click", () => {
  if (currentVerification && (currentVerification.kind === "login" || currentVerification.kind === "api_blocked")) {
    return;
  }
  const jobId = currentVerification?.job_id;
  chrome.runtime.sendMessage({ action: "resumeVerification", jobId });
  verificationBox.style.display = "none";
  if (jobStatusText) {
    jobStatusText.textContent = "Đã xác nhận xác minh, đang tiếp tục cào...";
  }
});

if (btnRecheckApi) {
  btnRecheckApi.addEventListener("click", async () => {
    if (!currentVerification) return;
    const jobId = currentVerification.job_id;
    btnRecheckApi.disabled = true;
    btnRecheckApi.textContent = "Đang kiểm tra...";
    if (recheckStatusMsg) {
      recheckStatusMsg.style.display = "block";
      recheckStatusMsg.style.color = "#0066cc";
      recheckStatusMsg.textContent = "Đang gửi preflight request tới Shopee API...";
    }
    try {
      const resp = await chrome.runtime.sendMessage({ action: "recheckApi", jobId });
      if (resp && resp.ok) {
        btnRecheckApi.textContent = "Thành công!";
        if (recheckStatusMsg) {
          recheckStatusMsg.style.color = "#008800";
          recheckStatusMsg.textContent = "✔ " + (resp.message || "Kiểm tra thành công (HTTP 200)! Đang tiếp tục cào...");
        }
        setTimeout(() => {
          verificationBox.style.display = "none";
          loadConfig();
        }, 1200);
      } else {
        btnRecheckApi.disabled = false;
        btnRecheckApi.textContent = "Kiểm tra lại";
        if (recheckStatusMsg) {
          recheckStatusMsg.style.color = "#cc0000";
          recheckStatusMsg.textContent = "✘ " + (resp?.error || "Shopee vẫn đang chặn API (HTTP 403)");
        }
      }
    } catch (err) {
      btnRecheckApi.disabled = false;
      btnRecheckApi.textContent = "Kiểm tra lại";
      if (recheckStatusMsg) {
        recheckStatusMsg.style.color = "#cc0000";
        recheckStatusMsg.textContent = "✘ Lỗi: " + err.message;
      }
    }
  });
}

// Periodic refresh
loadConfig();
setInterval(loadConfig, 2000);
