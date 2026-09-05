// AI Cowork — Content Script
// Automatically wakes up background service worker on Shopee or helper pages.
(function() {
  try {
    chrome.runtime.sendMessage({ action: "autoPair" }, () => {
      if (chrome.runtime.lastError) {
        // Ignored — worker waking up
      }
    });
  } catch (e) {
    // Ignore context invalidation
  }
})();
