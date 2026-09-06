const DEFAULT_API_BASE = "https://twikey-platform-backend.onrender.com";

const apiBaseInput = document.getElementById("api-base");
const status = document.getElementById("status");

const loginView = document.getElementById("login-view");
const loggedInView = document.getElementById("logged-in-view");
const accountNameEl = document.getElementById("account-name");
const authStatus = document.getElementById("auth-status");
const loginButton = document.getElementById("login-button");
const logoutButton = document.getElementById("logout-button");

chrome.storage.sync.get("apiBase").then((stored) => {
  apiBaseInput.value = stored.apiBase || DEFAULT_API_BASE;
});

document.getElementById("save-button").addEventListener("click", async () => {
  const value = apiBaseInput.value.trim().replace(/\/$/, "");
  if (!value) return;
  await chrome.storage.sync.set({ apiBase: value });
  status.textContent = "✓ Opgeslagen.";
  setTimeout(() => (status.textContent = ""), 2000);
});

function setAuthStatus(text, isError) {
  authStatus.textContent = text || "";
  authStatus.className = isError ? "error" : "ok";
}

function showAuthState(loggedIn, account) {
  loginView.style.display = loggedIn ? "none" : "block";
  loggedInView.style.display = loggedIn ? "block" : "none";
  if (loggedIn && account) {
    accountNameEl.textContent = account.company_name || account.login_email || "-";
  }
}

async function refreshAuthState() {
  const resp = await chrome.runtime.sendMessage({ type: "GET_AUTH_STATE" });
  showAuthState(resp.loggedIn, resp.account);
}

loginButton.addEventListener("click", async () => {
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-password").value;
  if (!email || !password) {
    setAuthStatus("Vul e-mailadres en wachtwoord in.", true);
    return;
  }
  loginButton.disabled = true;
  setAuthStatus("Bezig met inloggen...", false);
  const resp = await chrome.runtime.sendMessage({ type: "LOGIN", email, password });
  loginButton.disabled = false;
  if (resp.ok) {
    setAuthStatus("✓ Ingelogd.", false);
    showAuthState(true, resp.account);
  } else {
    setAuthStatus(resp.error || "Inloggen mislukt.", true);
  }
});

logoutButton.addEventListener("click", async () => {
  logoutButton.disabled = true;
  await chrome.runtime.sendMessage({ type: "LOGOUT" });
  logoutButton.disabled = false;
  setAuthStatus("", false);
  showAuthState(false, null);
});

refreshAuthState();
