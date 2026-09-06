/**
 * Injected into LinkedIn profile pages (https://www.linkedin.com/in/*).
 *
 * What this does (and, importantly, does NOT do):
 *  - Reads the visible profile name from the page, the same way you'd read
 *    it with your own eyes, to save you typing it in.
 *  - Renders one of your outreach templates with that name filled in, and
 *    copies it to your clipboard so you can paste it into LinkedIn's own
 *    connection-note or message box.
 *  - Logs the outreach action to your Twikey Sales Platform dashboard
 *    AFTER you tell it you've sent something.
 *
 * It never fills in or clicks anything inside LinkedIn's own UI, and never
 * sends a connection request or message itself. That's a deliberate choice,
 * not a technical limitation - see README.md in this folder for why.
 */

(function () {
  "use strict";

  function apiRequest(path, options) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "API_REQUEST", path, options }, resolve);
    });
  }

  function detectName() {
    const h1 = document.querySelector("h1");
    return h1 ? h1.textContent.trim() : "";
  }

  function detectHeadline() {
    // Best-effort: LinkedIn's profile headline usually sits in the first
    // text-only element right after the <h1>. This is a heuristic, not a
    // guaranteed selector - LinkedIn's markup changes over time, so the
    // "Bedrijf" field is always editable if this guesses wrong.
    const h1 = document.querySelector("h1");
    if (!h1) return "";
    let el = h1.parentElement && h1.parentElement.nextElementSibling;
    for (let i = 0; i < 3 && el; i++) {
      const text = el.textContent.trim();
      if (text && text.length < 120) return text;
      el = el.nextElementSibling;
    }
    return "";
  }

  function renderTemplate(body, firstName, company) {
    // Templates use {{company}} in ways that assume a real company name is
    // there ("at {{company}}'s size", "help {{company}} accelerate ...").
    // Leaving it blank when the "Bedrijf" field is empty produces broken
    // grammar like "at 's size" - so fall back to "your company", which
    // reads naturally in every default template's phrasing.
    const companyText = (company || "").trim() || "your company";
    return body
      .replaceAll("{{firstName}}", firstName || "")
      .replaceAll("{{company}}", companyText);
  }

  const ACTION_LABELS = {
    connection_sent: "Connectieverzoek verstuurd",
    connection_accepted: "Connectieverzoek geaccepteerd",
    message_sent: "Bericht verstuurd",
    reply_received: "Reactie ontvangen",
  };

  function buildPanel() {
    const panel = document.createElement("div");
    panel.id = "twikey-outreach-panel";
    panel.innerHTML = `
      <div id="twikey-panel-header">
        <span>📇 Twikey Outreach</span>
        <button id="twikey-toggle" title="In-/uitklappen">–</button>
      </div>
      <div id="twikey-panel-body">
        <div class="twikey-row">
          <label>Contactnaam</label>
          <input type="text" id="twikey-contact-name">
        </div>
        <div class="twikey-row">
          <label>Bedrijf</label>
          <input type="text" id="twikey-contact-company" placeholder="(optioneel, voor {{company}})">
        </div>
        <div class="twikey-row">
          <label>Template</label>
          <select id="twikey-template-select">
            <option value="">Templates laden...</option>
          </select>
        </div>
        <textarea id="twikey-preview" readonly rows="4"></textarea>
        <button id="twikey-copy-button" class="twikey-btn twikey-btn-primary">📋 Kopieer naar klembord</button>
        <p class="twikey-hint">Plak de tekst zelf in LinkedIn's eigen venster en verstuur het daar.</p>
        <div class="twikey-log-buttons">
          <button class="twikey-btn twikey-log-btn" data-action="connection_sent">Log: verzoek verstuurd</button>
          <button class="twikey-btn twikey-log-btn" data-action="message_sent">Log: bericht verstuurd</button>
          <button class="twikey-btn twikey-log-btn" data-action="connection_accepted">Log: geaccepteerd</button>
          <button class="twikey-btn twikey-log-btn" data-action="reply_received">Log: reactie ontvangen</button>
        </div>
        <div id="twikey-status"></div>
      </div>
    `;
    document.body.appendChild(panel);
    return panel;
  }

  function setStatus(panel, text, isError) {
    const el = panel.querySelector("#twikey-status");
    el.textContent = text;
    el.className = isError ? "twikey-status-error" : "twikey-status-ok";
  }

  async function init() {
    if (document.getElementById("twikey-outreach-panel")) return;
    const panel = buildPanel();

    const nameInput = panel.querySelector("#twikey-contact-name");
    const companyInput = panel.querySelector("#twikey-contact-company");
    const templateSelect = panel.querySelector("#twikey-template-select");
    const preview = panel.querySelector("#twikey-preview");

    nameInput.value = detectName();
    companyInput.value = detectHeadline();

    let templates = [];

    function updatePreview() {
      const template = templates.find((t) => t.label === templateSelect.value);
      if (!template) {
        preview.value = "";
        return;
      }
      const firstName = (nameInput.value || "").split(" ")[0];
      preview.value = renderTemplate(template.body, firstName, companyInput.value);
    }

    const templatesResp = await apiRequest("/api/linkedin/templates", { method: "GET" });
    if (templatesResp.ok) {
      templates = templatesResp.data.templates;
      templateSelect.innerHTML = templates
        .map((t) => `<option value="${t.label}">Template ${t.label}: ${t.title}</option>`)
        .join("");
      updatePreview();
    } else if (templatesResp.status === 401) {
      templateSelect.innerHTML = `<option value="">Log eerst in</option>`;
      setStatus(panel, "Je bent niet ingelogd. Klik het extensie-icoon rechtsboven in Chrome en log in met je Twikey-account.", true);
    } else {
      templateSelect.innerHTML = `<option value="">Kon templates niet laden</option>`;
      setStatus(panel, "Kon geen verbinding maken met de backend. Check de API-URL in de extensie-instellingen (klik het extensie-icoon).", true);
    }

    nameInput.addEventListener("input", updatePreview);
    companyInput.addEventListener("input", updatePreview);
    templateSelect.addEventListener("change", updatePreview);

    panel.querySelector("#twikey-toggle").addEventListener("click", () => {
      const body = panel.querySelector("#twikey-panel-body");
      const collapsed = body.style.display === "none";
      body.style.display = collapsed ? "block" : "none";
      panel.querySelector("#twikey-toggle").textContent = collapsed ? "–" : "+";
    });

    panel.querySelector("#twikey-copy-button").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(preview.value);
        setStatus(panel, "✓ Gekopieerd naar klembord.", false);
      } catch (err) {
        setStatus(panel, "Kopiëren mislukt: " + err.message, true);
      }
    });

    panel.querySelectorAll(".twikey-log-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const action = btn.getAttribute("data-action");
        const contactName = nameInput.value.trim();
        if (!contactName) {
          setStatus(panel, "Vul een contactnaam in.", true);
          return;
        }
        btn.disabled = true;
        const resp = await apiRequest("/api/linkedin/log", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contact_name: contactName,
            action,
            template_label: templateSelect.value || "",
            note: `Gelogd via LinkedIn-profiel: ${location.href}`,
          }),
        });
        btn.disabled = false;
        if (resp.ok) {
          setStatus(panel, `✓ Gelogd: ${ACTION_LABELS[action]} voor ${contactName}.`, false);
        } else if (resp.status === 401) {
          setStatus(panel, "Je bent niet (meer) ingelogd. Klik het extensie-icoon en log opnieuw in.", true);
        } else {
          setStatus(panel, `Loggen mislukt: ${(resp.data && resp.data.detail) || resp.error || resp.status}`, true);
        }
      });
    });
  }

  init();
})();
