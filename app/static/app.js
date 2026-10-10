/* DealPilot frontend: vanilla JS, hash routing (#/ and #/deal/<id>). */
const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
const QUICK_QS = [
  "What are the biggest objections?",
  "Who are the key stakeholders?",
  "What happened in our previous meetings?",
  "Prepare me for the next meeting.",
  "What should I focus on next?",
];
let pendingPrep = false;
let activeRecognition = null;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
function fmt(text) {
  let html = "", inList = false;
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    const m = line.match(/^[-*•]\s+(.*)/) || line.match(/^\d+[.)]\s+(.*)/);
    if (m) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(m[1])}</li>`; }
    else { if (inList) { html += "</ul>"; inList = false; } if (line) html += `<p>${inline(line)}</p>`; }
  }
  return html + (inList ? "</ul>" : "");
}
const fmtDate = (iso) => iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  } catch { throw new Error("Can't reach the DealPilot server. Is it still running?"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (HTTP ${res.status}).`);
  return data;
}
const post = (path, body = {}) => api(path, { method: "POST", body: JSON.stringify(body) });

let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (isErr ? " err" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), isErr ? 7000 : 3500);
}
async function withLoading(btn, label, fn) {
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = label;
  try { return await fn(); } finally { btn.disabled = false; btn.textContent = original; }
}

/* ---------- shared renderers ---------- */
function memoryCards(list) {
  if (!list.length) return `<div class="empty">No customer-specific context recalled yet. Save an interaction or try a question.</div>`;
  return list.map((m) => `
    <div class="mem-card">
      <p>${esc(m.text)}</p>
      <div class="mem-meta">
        ${m.source ? `<span class="badge">${esc(memorySourceLabel(m.source))}</span>` : ""}
        ${m.type ? `<span class="badge">${esc(m.type)}</span>` : ""}
        ${m.context ? `<span>${esc(m.context)}</span>` : ""}
        ${m.date ? `<span>${esc(fmtDate(m.date))}</span>` : ""}
      </div>
    </div>`).join("");
}
function briefList(items) {
  if (!items.length) return `<p class="none">Nothing in memory yet.</p>`;
  return "<ul>" + items.map((i) => {
    if (i && typeof i === "object") {
      const role = i.role || i.name || i.name_or_role || "";
      return `<li><strong>${esc(role)}</strong>${i.stance ? `: ${esc(i.stance)}` : ""}</li>`;
    }
    return `<li>${esc(i)}</li>`;
  }).join("") + "</ul>";
}
function memorySourceLabel(source) {
  if (source === "mixed") return "Hindsight and saved customer history";
  if (source === "customer_history") return "saved customer history";
  if (source === "offline") return "offline demo history";
  return "Hindsight memories";
}
function briefCard(b, count, source) {
  const sec = (title, body, cls = "") => `<div class="brief-sec ${cls}"><h3>${title}</h3>${body}</div>`;
  return `
    <div class="brief-head"><h2>Meeting brief</h2><span class="badge mem">Built from ${count} ${esc(memorySourceLabel(source))}</span></div>
    <div class="brief-grid">
      ${sec("Deal status", `<p>${esc(b.deal_status) || '<span class="none">Not in memory yet.</span>'}</p>`, "wide")}
      ${sec("Key stakeholders", briefList(b.key_stakeholders))}
      ${sec("Top objections", briefList(b.top_objections))}
      ${sec("Requirements", briefList(b.requirements))}
      ${sec("Competitor", briefList(b.competitor))}
      ${sec("Open issues", briefList(b.open_issues), "wide")}
      ${sec("Recommended next action", `<p>${esc(b.recommended_next_action) || "Not in memory yet."}</p>`, "wide action")}
    </div>`;
}
/* ---------- routing ---------- */
let pendingCapture = false;

function setActiveNav(section) {
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const active = link.dataset.nav === section;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function openDeal(dealId, action = "") {
  pendingCapture = action === "capture";
  pendingPrep = action === "prepare";
  const nextHash = `#/deal/${dealId}`;
  if (location.hash === nextHash) route();
  else location.hash = nextHash;
}

async function route() {
  if (activeRecognition) {
    activeRecognition.stop();
    activeRecognition = null;
  }
  const m = location.hash.match(/^#\/deal\/([\w-]+)/);
  const page = location.hash.replace(/\/$/, "") || "#/";
  const navSection = m ? "meeting" : page === "#/customers" ? "customers" : page === "#/activity" ? "activity" : "dashboard";
  setActiveNav(navSection);
  app.innerHTML = `<p class="muted"><span class="spinner"></span>Loading…</p>`;
  try {
    if (m) await renderDeal(m[1]);
    else if (page === "#/customers") await renderCustomers();
    else if (page === "#/activity") await renderActivity();
    else await renderDashboard();
  } catch (e) {
    app.innerHTML = `<div class="page-heading"><p class="eyebrow">Something went wrong</p><h1>We couldn't load this page</h1></div><div class="banner">${esc(e.message)}</div><button class="primary" data-retry>Try again</button>`;
    $("[data-retry]")?.addEventListener("click", route);
  }
}
window.addEventListener("hashchange", route);
document.addEventListener("click", (event) => {
  const capture = event.target.closest("[data-capture]");
  if (capture) {
    const currentDeal = location.hash.match(/^#\/deal\/([\w-]+)/);
    const dealId = capture.closest(".sidebar") && currentDeal ? currentDeal[1] : capture.dataset.capture;
    openDeal(dealId, "capture");
  }
});

function statusBanner(s) {
  const missing = [];
  if (!s.hindsight_configured) missing.push("<code>HINDSIGHT_API_KEY</code>");
  if (!s.groq_configured) missing.push("<code>GROQ_API_KEY</code>");
  return missing.length
    ? `<div class="banner"><strong>Setup needed.</strong> Add ${missing.join(" and ")} to your <code>.env</code> file and restart the server.</div>` : "";
}

/* ---------- dashboard ---------- */
function noteSentences(notes) {
  return notes.flatMap((note) => String(note.content || "").split(/\n+/).flatMap((line) => line.match(/[^.!?]+[.!?]?/g) || []))
    .map((sentence) => sentence.trim()).filter(Boolean);
}

function noteHighlights(notes, pattern, emptyText) {
  const matches = [...new Set(noteSentences(notes).filter((sentence) => pattern.test(sentence)))].slice(0, 2);
  return matches.length
    ? `<ul class="evidence-list">${matches.map((sentence) => `<li>${esc(sentence)}</li>`).join("")}</ul>`
    : `<p class="empty-copy">${esc(emptyText)}</p>`;
}

function customerCard(deal) {
  const notes = Array.isArray(deal.recent_interactions) ? deal.recent_interactions : [];
  const latest = notes[notes.length - 1];
  const total = Number(deal.interaction_count) || 0;
  const recentLabel = latest?.created_at ? `Last interaction ${esc(fmtDate(latest.created_at))}` : "No interactions saved yet";
  return `
    <article class="customer-card">
      <div class="customer-card-head">
        <div class="customer-mark" aria-hidden="true">${esc(String(deal.name || "?").slice(0, 1).toUpperCase())}</div>
        <div class="customer-title"><p class="eyebrow">CUSTOMER</p><h2>${esc(deal.name)}</h2><p class="muted small">${total} ${total === 1 ? "interaction" : "interactions"} · ${recentLabel}</p></div>
        <a class="text-link" href="#/deal/${encodeURIComponent(deal.id)}">Open customer <span aria-hidden="true">→</span></a>
      </div>
      <div class="customer-note">
        <span class="mini-label">MOST RECENT NOTE</span>
        ${latest ? `<p>${esc(latest.content)}</p>` : `<p class="muted">Capture meeting notes to start building this customer’s history.</p>`}
      </div>
      <div class="insight-grid">
        <section class="insight-cell"><h3>Requirements mentioned</h3>${noteHighlights(notes, /\b(require(?:s|d|ment)?|need(?:s|ed)?|must|asked for|wants?)\b/i, "No specific requirement found in recent notes.")}</section>
        <section class="insight-cell"><h3>Risks or concerns mentioned</h3>${noteHighlights(notes, /\b(concern(?:ed)?|risk|block(?:er|ed)?|pending|too high|compar(?:e|ing|ison)|issue)\b/i, "No specific concern found in recent notes.")}</section>
        <section class="insight-cell"><h3>Next action</h3><p class="empty-copy">No saved next action. Prepare a meeting brief to review suggested next steps.</p></section>
      </div>
      <p class="data-note">Requirements and concerns above are matching excerpts from saved notes, not AI-generated findings.</p>
      <div class="customer-actions">
        <button class="primary" type="button" data-capture="${esc(deal.id)}">＋ Capture meeting</button>
        <button class="secondary" type="button" data-prepare="${esc(deal.id)}">Prepare for meeting</button>
        ${deal.id === "acme-corp" && !deal.demo_loaded ? `<button class="quiet-button" type="button" data-demo="${esc(deal.id)}">Load sample notes</button>` : ""}
      </div>
    </article>`;
}

function dealEmptyState() {
  return `<section class="empty-state"><div class="empty-icon" aria-hidden="true">✦</div><h2>No customer records yet</h2><p>When a customer is available, you can capture meeting notes and prepare for your next conversation here.</p></section>`;
}

function bindCustomerActions() {
  app.querySelectorAll("[data-prepare]").forEach((button) => button.addEventListener("click", () => openDeal(button.dataset.prepare, "prepare")));
  app.querySelectorAll("[data-demo]").forEach((button) => button.addEventListener("click", () => withLoading(button, "Loading sample…", async () => {
    try { await post(`/api/deals/${button.dataset.demo}/load-demo`); toast("Sample notes added to the customer history."); await route(); }
    catch (error) { toast(error.message, true); }
  })));
}

async function renderDashboard() {
  const [{ deals }, status] = await Promise.all([api("/api/deals"), api("/api/status")]);
  const total = deals.reduce((n, d) => n + (Number(d.interaction_count) || 0), 0);
  app.innerHTML = `
    <div class="page-heading">
      <div><p class="eyebrow">YOUR SALES WORKSPACE</p><h1>Your deals, at a glance</h1><p class="page-subtitle">Pick a customer, capture what you discussed, and get ready for what comes next.</p></div>
      <div class="hero-actions"><button class="primary" type="button" data-capture="${esc(deals[0]?.id || "acme-corp")}">＋ Capture meeting</button><button class="secondary" type="button" data-prepare="${esc(deals[0]?.id || "acme-corp")}">Prepare for meeting</button></div>
    </div>
    ${statusBanner(status)}
    <section class="overview-stats" aria-label="Workspace summary">
      <div class="overview-stat"><span class="stat-icon" aria-hidden="true">◉</span><div><strong>${deals.length}</strong><span>${deals.length === 1 ? "customer" : "customers"}</span></div></div>
      <div class="overview-stat"><span class="stat-icon amber" aria-hidden="true">▤</span><div><strong>${total}</strong><span>${total === 1 ? "saved interaction" : "saved interactions"}</span></div></div>
    </section>
    <div class="section-heading"><div><p class="eyebrow">CUSTOMER OVERVIEW</p><h2>Your customers</h2></div><a href="#/customers" class="text-link">View customers <span aria-hidden="true">→</span></a></div>
    ${deals.length ? `<div class="customer-list">${deals.map(customerCard).join("")}</div>` : dealEmptyState()}`;
  bindCustomerActions();
}

async function renderCustomers() {
  const { deals } = await api("/api/deals");
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">CUSTOMER RECORDS</p><h1>Customers</h1><p class="page-subtitle">Each customer has a separate interaction history and meeting workspace.</p></div><button class="primary" type="button" data-capture="${esc(deals[0]?.id || "acme-corp")}">＋ Capture meeting</button></div>
    <form class="card add-customer-form" id="add-customer-form">
      <div><label class="field-label" for="new-customer-name">Add a customer</label><p class="sub">Create a separate workspace and history. Existing customer notes are not changed.</p></div>
      <div class="add-customer-controls"><input type="text" id="new-customer-name" maxlength="120" placeholder="Customer or company name" aria-describedby="create-customer-status"><button class="primary" type="submit">Add customer</button></div>
      <p id="create-customer-status" class="recording-status" role="status" aria-live="polite"></p>
    </form>
    ${deals.length ? `<div class="customer-list">${deals.map(customerCard).join("")}</div>` : dealEmptyState()}`;
  bindCustomerActions();
  $("#add-customer-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#new-customer-name");
    const status = $("#create-customer-status");
    const name = input.value.trim();
    if (!name) {
      status.textContent = "Enter a customer name.";
      input.focus();
      return;
    }
    const submit = event.submitter || event.target.querySelector('[type="submit"]');
    await withLoading(submit, "Adding…", async () => {
      let deal;
      try {
        ({ deal } = await post("/api/deals", { name }));
      } catch (error) {
        status.textContent = error.message;
        return;
      }
      if (!deal?.id) {
        status.textContent = "The server returned no customer ID. Refresh the page to check whether the customer was added.";
        return;
      }
      try {
        await renderCustomers();
        toast(`${deal.name} added. Open the customer card to capture a meeting or prepare a brief.`);
      } catch {
        status.textContent = `${deal.name} was added, but the list could not refresh. Reload the Customers page to see it.`;
      }
    });
  });
}

async function renderActivity() {
  const { deals } = await api("/api/deals");
  const events = deals.flatMap((deal) => (deal.recent_interactions || []).map((item) => ({ ...item, dealName: deal.name, dealId: deal.id })))
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  app.innerHTML = `
    <div class="page-heading"><div><p class="eyebrow">RECENT HISTORY</p><h1>Activity</h1><p class="page-subtitle">A record of notes saved to your customer timeline.</p></div></div>
    ${events.length ? `<section class="activity-list">${events.map((item) => `
      <article class="activity-item"><div class="activity-dot" aria-hidden="true"></div><div class="activity-body"><div class="activity-meta"><a href="#/deal/${encodeURIComponent(item.dealId)}">${esc(item.dealName)}</a><time>${esc(fmtDate(item.created_at))}${item.source === "demo" ? " · sample note" : ""}</time></div><p>${esc(item.content)}</p></div></article>`).join("")}</section>
      <p class="muted small activity-limit">Showing the most recent ${events.length} saved interactions.</p>`
      : `<section class="empty-state"><div class="empty-icon" aria-hidden="true">↺</div><h2>No activity yet</h2><p>Saved meeting notes will appear here.</p><button class="primary" type="button" data-capture="${esc(deals[0]?.id || "acme-corp")}">＋ Capture meeting</button></section>`}`;
}

/* ---------- deal page ---------- */
async function renderDeal(id) {
  const [deal, status, { deals }] = await Promise.all([
    api(`/api/deals/${id}`), api("/api/status"), api("/api/deals"),
  ]);
  if (location.hash !== `#/deal/${id}`) return;
  const meetingNav = document.querySelector('[data-nav="meeting"]');
  if (meetingNav) meetingNav.href = `#/deal/${encodeURIComponent(deal.id)}`;
  const prepQuestion = `Tell me about ${deal.name} and prepare me for the next meeting.`;
  app.innerHTML = `
    <a href="#/customers" class="back-link">← Back to customers</a>
    <div class="page-heading deal-page-heading">
      <div><p class="eyebrow">MEETING ASSISTANT</p><h1>${esc(deal.name)}</h1><p class="page-subtitle">Keep the conversation organized and walk into your next meeting prepared.</p>
        <label class="customer-selector"><span>Current customer</span><select id="customer-select" aria-label="Select customer">${deals.map((item) => `<option value="${esc(item.id)}" ${item.id === deal.id ? "selected" : ""}>${esc(item.name)}</option>`).join("")}</select></label>
      </div>
      <div class="deal-heading-actions">
        <div class="stats">
          <div class="overview-stat compact"><div><strong id="stat-int">${deal.interaction_count}</strong><span>saved interactions</span></div></div>
        </div>
        <div class="hero-actions"><button class="secondary" type="button" id="jump-capture">＋ Capture meeting</button></div>
      </div>
    </div>
    ${statusBanner(status)}
    <section class="workflow-strip" aria-label="Meeting preparation steps">
      <div class="workflow-step complete"><span>1</span><strong>Choose customer</strong></div><i aria-hidden="true"></i>
      <div class="workflow-step current"><span>2</span><strong>Capture notes</strong></div><i aria-hidden="true"></i>
      <div class="workflow-step"><span>3</span><strong>Review and save</strong></div><i aria-hidden="true"></i>
      <div class="workflow-step"><span>4</span><strong>Prepare brief</strong></div>
    </section>
    <div class="grid">
      <div>
        <section class="card capture-card" id="capture-card">
          <div class="card-heading-row"><div><p class="eyebrow">STEP 2 · CAPTURE</p><h2>Capture a meeting</h2></div><span class="step-badge">Nothing is saved until you confirm</span></div>
          <p class="sub">Record a transcript with your browser or use text. DealPilot does not start recording until you choose Start Recording.</p>
          <div class="consent-notice"><strong>Before recording</strong><p>Ask everyone in the meeting for permission first. Browser speech recognition may process audio through your browser’s speech service. You can stop at any time or use text instead.</p></div>
          <label class="consent-check"><input type="checkbox" id="record-consent"><span>I have permission to capture speech from everyone who may be heard.</span></label>
          <div class="recording-toolbar">
            <button class="primary record-button" type="button" id="start-recording">● Start Recording</button>
            <button class="secondary" type="button" id="stop-recording" disabled>Stop Recording</button>
            <button class="secondary" type="button" id="use-text">Use Text Instead</button>
            <button class="quiet-button" type="button" id="paste-transcript">Paste Transcript</button>
          </div>
          <p id="recording-status" class="recording-status" role="status" aria-live="polite"></p>
          <p id="interim-transcript" class="interim-transcript" aria-live="polite" hidden></p>
          <label class="field-label" for="note">Transcript (editable)</label>
          <textarea id="note" placeholder="Your transcript will appear here. You can also type or paste meeting notes." aria-describedby="recording-status"></textarea>
          <div class="summary-review">
            <div class="card-heading-row"><div><p class="eyebrow">STEP 3 · REVIEW</p><h3>Review meeting summary</h3></div><span id="summary-source" class="step-badge">Not summarized yet</span></div>
            <p class="sub">Ask DealPilot for a structured draft, then check and edit every item. Suggestions use only this transcript.</p>
            <div id="summary-fields" class="summary-fields"></div>
            <button class="secondary" type="button" id="review-summary">Review Meeting Summary</button>
            <p id="summary-status" class="summary-status" role="status" aria-live="polite">Add or paste a transcript to request a summary, or fill in these fields yourself.</p>
          </div>
          <label class="summary-confirm"><input type="checkbox" id="confirm-summary"><span>I reviewed the transcript and summary and want to save them to this customer.</span></label>
          <div class="actions capture-actions">
            <button class="primary" type="button" id="remember" disabled>Save reviewed meeting</button>
            ${deal.demo_loaded ? "" : `<button id="demo">Load sample notes</button>`}
          </div>
          <p class="save-note">Saving sends the transcript and reviewed summary to the configured memory service and adds them to this customer’s activity.</p>
        </section>

        <section class="card">
          <p class="eyebrow">MEETING ASSISTANT</p><h2>Ask about this customer</h2>
          <p class="sub">Ask a question about the customer history available to DealPilot.</p>
          <div class="chips">${QUICK_QS.map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
          <div class="chat-log" id="chat"></div>
          <div class="ask-row">
            <input type="text" id="q" placeholder="Ask about ${esc(deal.name)}…" aria-label="Question">
            <button class="primary" id="ask">Ask DealPilot</button>
          </div>
        </section>

        <div id="brief-slot"></div>
        <section class="card">
          <h2>Why memory matters</h2>
          <p class="sub">Same question, same model. The memory answer uses this customer’s saved history and any Hindsight memories tagged to this customer.</p>
          <p><strong>“${esc(prepQuestion)}”</strong></p>
          <div class="actions">
            <button id="no-mem">Ask Without Memory</button>
            <button class="memory" id="with-mem">Ask With Customer Memory</button>
          </div>
          <div class="compare">
            <div class="panel"><h3>Without memory <span class="badge">No context</span></h3><div id="out-no"><p class="muted small">Click “Ask Without Memory”.</p></div></div>
            <div class="panel with"><h3>With Hindsight <span class="badge mem">Recalled memories</span></h3><div id="out-yes"><p class="muted small">Click “Ask With Hindsight”.</p></div></div>
          </div>
        </section>
      </div>

      <aside>
        <section class="card" id="summary-card">
          <h2>Current deal summary</h2>
          <div id="summary"><p class="muted small">${deal.interaction_count ? "Generate a meeting brief to see the current summary." : "No interactions yet. Load the Acme demo to get started."}</p></div>
          <div class="actions"><button class="primary" id="brief-btn">Prepare for meeting</button></div>
        </section>
        <section class="card">
          <div class="mem-head"><h2>Customer-specific context</h2><span class="badge mem" id="mem-count">0</span></div>
          <div id="mem-list">${memoryCards([])}</div>
        </section>
        <section class="card">
          <h2>Interaction timeline</h2>
          <div id="timeline"></div>
        </section>
      </aside>
    </div>`;

  const base = `/api/deals/${id}`;
  $("#customer-select").addEventListener("change", (event) => {
    location.hash = `#/deal/${encodeURIComponent(event.target.value)}`;
  });
  const renderTimeline = (items) => {
    $("#timeline").innerHTML = items.length
      ? `<ul class="timeline">${[...items].reverse().map((i) => `<li><time>${esc(fmtDate(i.created_at))}${i.source === "demo" ? " · demo" : ""}</time>${esc(i.content.length > 150 ? i.content.slice(0, 150) + "…" : i.content)}</li>`).join("")}</ul>`
      : `<div class="empty">No interactions yet. Add notes or load the Acme demo.</div>`;
  };
  renderTimeline(deal.interactions);
  const showMemories = (list) => { $("#mem-list").innerHTML = memoryCards(list); $("#mem-count").textContent = `${list.length} found`; };
  const refreshCounts = async () => {
    const d = await api(base);
    $("#stat-int").textContent = d.interaction_count;
    renderTimeline(d.interactions);
  };
  const showBrief = (r) => {
    $("#brief-slot").innerHTML = `<section class="brief">${briefCard(r.brief, r.memory_count, r.memory_source)}</section>`;
    $("#summary").innerHTML = fmt(r.brief.deal_status);
  };
  const noteInput = $("#note");
  const saveNoteButton = $("#remember");
  const recordConsent = $("#record-consent");
  const startRecording = $("#start-recording");
  const stopRecording = $("#stop-recording");
  const recordingStatus = $("#recording-status");
  const interimTranscript = $("#interim-transcript");
  const summaryStatus = $("#summary-status");
  const summarySource = $("#summary-source");
  const confirmSummary = $("#confirm-summary");
  const summaryLabels = {
    requirements: "Customer requirements",
    objections: "Objections or concerns",
    stakeholders: "Stakeholders and roles",
    commitments: "Commitments made",
    deadlines: "Deadlines and dates",
    unresolved_questions: "Unresolved questions",
  };
  const summaryFields = $("#summary-fields");
  summaryFields.innerHTML = Object.entries(summaryLabels).map(([key, label]) => `
    <div class="summary-field"><label class="field-label" for="summary-${key}">${esc(label)}</label><textarea id="summary-${key}" rows="2" maxlength="1000" placeholder="Add only what was said, or leave blank if unknown."></textarea></div>
  `).join("");
  const summaryTextareas = Object.keys(summaryLabels).map((key) => $(`#summary-${key}`));
  const getSummary = () => Object.fromEntries(Object.keys(summaryLabels).map((key) => [
    key,
    $(`#summary-${key}`).value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean),
  ]));
  const buildMeetingRecord = () => {
    const summary = getSummary();
    const sections = Object.entries(summaryLabels)
      .filter(([key]) => summary[key].length)
      .map(([key, label]) => `${label}:\n${summary[key].map((item) => `- ${item}`).join("\n")}`);
    return `Meeting transcript:\n${noteInput.value.trim()}\n\nReviewed meeting summary:\n${sections.length ? sections.join("\n\n") : "No details entered by reviewer."}`;
  };
  const updateSaveState = () => {
    saveNoteButton.disabled = !confirmSummary.checked || !noteInput.value.trim();
  };
  const invalidateReview = () => {
    confirmSummary.checked = false;
    updateSaveState();
    if (summarySource.textContent !== "Not summarized yet") {
      summarySource.textContent = "Transcript changed — review again";
      summaryStatus.textContent = "The transcript changed. Request a fresh summary or update the summary fields yourself, then confirm your review.";
    }
  };
  let recognition = null;
  let recording = false;
  let recognitionFinalText = "";
  let lastRecognitionError = "";
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const recognitionSupported = Boolean(window.isSecureContext && Recognition);

  if (recognitionSupported) {
    recordingStatus.textContent = "Speech recognition is available in this browser. Choose the consent checkbox before starting.";
  } else {
    startRecording.disabled = true;
    recordingStatus.textContent = "Speech recognition is unavailable in this browser or connection. Type or paste your transcript instead.";
  }

  const setRecordingState = (isRecording) => {
    recording = isRecording;
    startRecording.disabled = !recognitionSupported || isRecording;
    startRecording.setAttribute("aria-pressed", String(isRecording));
    stopRecording.disabled = !isRecording;
    $("#use-text").disabled = false;
    noteInput.readOnly = isRecording;
  };
  const recognitionError = (code) => ({
    "not-allowed": "Microphone permission was denied. Allow microphone access or use text instead.",
    "service-not-allowed": "The browser speech service is unavailable. Use text or paste a transcript instead.",
    "audio-capture": "No microphone was available. Check your microphone or use text instead.",
    network: "The browser speech service could not be reached. Use text or paste a transcript instead.",
    "no-speech": "No speech was detected. Try again or use text instead.",
    aborted: "Recording stopped.",
  }[code] || "Speech recognition could not continue. Use text or paste a transcript instead.");

  startRecording.addEventListener("click", () => {
    if (!recognitionSupported) {
      recordingStatus.textContent = "Speech recognition is unavailable here. Type or paste a transcript instead.";
      return;
    }
    if (!recordConsent.checked) {
      recordingStatus.textContent = "Please confirm everyone has given permission before starting the microphone.";
      recordConsent.focus();
      return;
    }
    if (recording) return;
    try {
      recognition = new Recognition();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = "en-US";
      recognitionFinalText = noteInput.value.trim();
      lastRecognitionError = "";
      recognition.onstart = () => {
        activeRecognition = recognition;
        invalidateReview();
        interimTranscript.hidden = true;
        interimTranscript.textContent = "";
        setRecordingState(true);
        recordingStatus.textContent = "Listening… Speak clearly. Stop recording when the meeting capture is complete.";
      };
      recognition.onresult = (event) => {
        let interim = "";
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const text = event.results[i][0]?.transcript?.trim();
          if (!text) continue;
          if (event.results[i].isFinal) recognitionFinalText = [recognitionFinalText, text].filter(Boolean).join(" ");
          else interim = [interim, text].filter(Boolean).join(" ");
        }
        noteInput.value = [recognitionFinalText, interim].filter(Boolean).join(" ");
        invalidateReview();
        interimTranscript.textContent = interim ? `Hearing: ${interim}` : "";
        interimTranscript.hidden = !interim;
      };
      recognition.onerror = (event) => {
        lastRecognitionError = event.error;
        const message = recognitionError(event.error);
        recordingStatus.textContent = message;
        if (event.error !== "aborted") toast(message, true);
      };
      recognition.onend = () => {
        if (activeRecognition === recognition) activeRecognition = null;
        setRecordingState(false);
        interimTranscript.hidden = true;
        interimTranscript.textContent = "";
        if (!lastRecognitionError || lastRecognitionError === "aborted") {
          recordingStatus.textContent = noteInput.value.trim()
            ? "Recording stopped. Review and edit the transcript below."
            : "Recording stopped. No transcript was captured; try again or use text.";
        }
      };
      recordingStatus.textContent = "Requesting microphone access…";
      recognition.start();
    } catch {
      setRecordingState(false);
      recordingStatus.textContent = "Could not start speech recognition. Check microphone permission or use text instead.";
    }
  });
  stopRecording.addEventListener("click", () => {
    if (recognition && recording) {
      recordingStatus.textContent = "Stopping recording…";
      recognition.stop();
    }
  });
  $("#use-text").addEventListener("click", () => {
    if (recognition && recording) recognition.stop();
    noteInput.focus();
    recordingStatus.textContent = "Text entry selected. Type or paste your transcript below.";
  });
  $("#paste-transcript").addEventListener("click", () => {
    noteInput.focus();
    noteInput.setSelectionRange(noteInput.value.length, noteInput.value.length);
    recordingStatus.textContent = "Paste your transcript into the text box. Nothing is saved yet.";
  });
  $("#review-summary").addEventListener("click", async (event) => {
    const transcript = noteInput.value.trim();
    if (!transcript) {
      summaryStatus.textContent = "Add, record, or paste a transcript before requesting a summary.";
      noteInput.focus();
      return;
    }
    confirmSummary.checked = false;
    updateSaveState();
    summaryStatus.textContent = "Asking Groq to organize only the information in this transcript…";
    summarySource.textContent = "Requesting summary…";
    await withLoading(event.target, "Reviewing…", async () => {
      try {
        const response = await post(`${base}/meeting-summary`, { transcript });
        for (const key of Object.keys(summaryLabels)) {
          $(`#summary-${key}`).value = response.summary[key].join("\n");
        }
        summarySource.textContent = "Groq draft — please check";
        summaryStatus.textContent = "Draft received. Check every field and edit anything that is missing or incorrect. This has not been saved.";
      }
      catch (error) {
        summarySource.textContent = "Manual review available";
        summaryStatus.textContent = `${error.message} You can still complete the summary by hand. Nothing has been saved.`;
      }
    });
  });
  noteInput.addEventListener("input", () => {
    invalidateReview();
  });
  summaryTextareas.forEach((textarea) => textarea.addEventListener("input", () => {
    confirmSummary.checked = false;
    updateSaveState();
    if (summarySource.textContent.startsWith("Groq draft")) summarySource.textContent = "Edited draft — check before saving";
  }));
  confirmSummary.addEventListener("change", updateSaveState);

  $("#remember").addEventListener("click", async (event) => {
    if (!confirmSummary.checked || !noteInput.value.trim()) return;
    const content = buildMeetingRecord();
    if (content.length > 10_000) {
      toast("Transcript and summary are over the 10,000 character limit. Shorten them before saving.", true);
      return;
    }
    await withLoading(event.target, "Saving…", async () => {
      try {
        await post(`${base}/interactions`, { content });
        noteInput.value = "";
        summaryTextareas.forEach((textarea) => { textarea.value = ""; });
        confirmSummary.checked = false;
        updateSaveState();
        summarySource.textContent = "Not summarized yet";
        summaryStatus.textContent = "Add or paste a transcript to request a summary, or fill in these fields yourself.";
        toast("Reviewed meeting saved to the customer timeline.");
        await refreshCounts();
      } catch (error) {
        toast(error.message, true);
        summaryStatus.textContent = `${error.message} Your transcript and summary are still here and have not been confirmed as saved.`;
      }
    });
  });
  $("#jump-capture").addEventListener("click", () => {
    noteInput.focus();
    $("#capture-card").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  const demoBtn = $("#demo");
  if (demoBtn) demoBtn.addEventListener("click", () => withLoading(demoBtn, "Loading demo…", async () => {
    try { await post(`${base}/load-demo`); toast("Acme demo loaded into Hindsight."); await route(); }
    catch (err) { toast(err.message, true); }
  }));

  const chat = $("#chat");
  const addMsg = (cls, html) => { const d = document.createElement("div"); d.className = `msg ${cls}`; d.innerHTML = html; chat.appendChild(d); chat.scrollTop = chat.scrollHeight; return d; };
  async function ask(question, btn) {
    if (!question.trim()) return toast("Type a question for DealPilot first.", true);
    addMsg("user", esc(question)); $("#q").value = "";
    const pending = addMsg("bot", `<span class="spinner"></span>Finding this customer’s saved context…`);
    const run = async () => {
      try {
        const r = await post(`${base}/ask`, { question });
        showMemories(r.memories);
        const badge = r.empty ? "" : `<span class="badge mem">Answered from ${r.memory_count} ${esc(memorySourceLabel(r.memory_source))}</span>`;
        if (r.brief) { showBrief(r); pending.innerHTML = `${badge}<p>Your meeting brief is ready below.</p>${fmt(r.answer)}`; }
        else pending.innerHTML = `${badge}${fmt(r.answer)}`;
      } catch (err) { pending.innerHTML = `<span style="color:var(--err)">${esc(err.message)}</span>`; toast(err.message, true); }
    };
    btn ? await withLoading(btn, "Asking…", run) : await run();
  }
  $("#ask").addEventListener("click", (e) => ask($("#q").value, e.target));
  $("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") ask($("#q").value, $("#ask")); });
  app.querySelectorAll("[data-q]").forEach((b) => b.addEventListener("click", () => ask(b.dataset.q)));

  $("#brief-btn").addEventListener("click", (e) => withLoading(e.target, "Generating…", async () => {
    try {
      const r = await post(`${base}/ask`, { question: "Prepare me for my next Acme meeting.", brief: true });
      showMemories(r.memories);
      if (r.brief) showBrief(r); else toast(r.answer, r.empty);
    } catch (err) { toast(err.message, true); }
  }));

  $("#no-mem").addEventListener("click", (e) => withLoading(e.target, "Asking…", async () => {
    $("#out-no").innerHTML = `<span class="spinner"></span>Thinking without memory…`;
    try {
      const r = await post(`${base}/ask-without-memory`, { question: prepQuestion });
      $("#out-no").innerHTML = fmt(r.answer);
    }
    catch (err) { $("#out-no").innerHTML = `<span style="color:var(--err)">${esc(err.message)}</span>`; }
  }));
  $("#with-mem").addEventListener("click", (e) => withLoading(e.target, "Recalling…", async () => {
    $("#out-yes").innerHTML = `<span class="spinner"></span>Recalling from Hindsight…`;
    try {
      const r = await post(`${base}/ask`, { question: prepQuestion, brief: false });
      showMemories(r.memories);
      $("#out-yes").innerHTML = (r.empty ? "" : `<p><span class="badge mem">Based on ${r.memory_count} ${esc(memorySourceLabel(r.memory_source))}</span></p>`) + fmt(r.answer);
    } catch (err) { $("#out-yes").innerHTML = `<span style="color:var(--err)">${esc(err.message)}</span>`; }
  }));

  if (pendingCapture) {
    pendingCapture = false;
    noteInput.focus();
    $("#capture-card").scrollIntoView({ behavior: "smooth", block: "center" });
  }
  if (pendingPrep) { pendingPrep = false; $("#brief-btn").click(); }
}

route();
