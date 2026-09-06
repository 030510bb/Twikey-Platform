/**
 * Background service worker.
 *
 * All calls to the Twikey Sales Platform backend go through here instead of
 * directly from the content script. Extension background contexts with
 * declared host_permissions can make cross-origin requests without being
 * blocked by the page's CORS policy, so this keeps things working
 * regardless of how CORS_ORIGINS is configured on the backend later.
 *
 * The backend now requires a logged-in session for /api/linkedin/* (part of
 * the platform's multi-tenant login), so this worker also owns the login
 * session: it stores the session token in chrome.storage.local (not .sync -
 * a session token shouldn't be copied to every device signed into the same
 * Chrome profile) and attaches it to every API_REQUEST automatically.
 */

const DEFAULT_API_BASE = "https://twikey-platform-backend.onrender.com";

async function getApiBase() {
  const stored = await chrome.storage.sync.get("apiBase");
  return stored.apiBase || DEFAULT_API_BASE;
}

async function getAuthState() {
  const stored = await chrome.storage.local.get(["token", "account"]);
  return { token: stored.token || null, account: stored.account || null };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "LOGIN") {
    (async () => {
      try {
        const apiBase = await getApiBase();
        const res = await fetch(`${apiBase}/api/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: message.email, password: message.password }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          sendResponse({ ok: false, error: data.detail || `HTTP ${res.status}` });
          return;
        }
        await chrome.storage.local.set({ token: data.token, account: data.account });
        sendResponse({ ok: true, account: data.account });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true;
  }

  if (message.type === "LOGOUT") {
    (async () => {
      const { token } = await getAuthState();
      if (token) {
        try {
          const apiBase = await getApiBase();
          await fetch(`${apiBase}/api/auth/logout`, {
            method: "POST",
            headers: { Authorization: `Bearer ${token}` },
          });
        } catch (_e) {
          // Ignore network errors - we clear the local session either way.
        }
      }
      await chrome.storage.local.remove(["token", "account"]);
      sendResponse({ ok: true });
    })();
    return true;
  }

  if (message.type === "GET_AUTH_STATE") {
    (async () => {
      const { token, account } = await getAuthState();
      sendResponse({ loggedIn: !!token, account });
    })();
    return true;
  }

  if (message.type !== "API_REQUEST") return false;

  (async () => {
    try {
      const apiBase = await getApiBase();
      const { token } = await getAuthState();
      const options = message.options || {};
      const headers = Object.assign({}, options.headers || {});
      if (token) headers.Authorization = `Bearer ${token}`;
      const res = await fetch(`${apiBase}${message.path}`, Object.assign({}, options, { headers }));
      let data = null;
      try {
        data = await res.json();
      } catch (_e) {
        // no JSON body, fine
      }
      sendResponse({ ok: res.ok, status: res.status, data });
    } catch (err) {
      sendResponse({ ok: false, status: 0, error: err.message });
    }
  })();

  return true; // keep the message channel open for the async sendResponse
});
