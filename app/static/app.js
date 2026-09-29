/* DealPilot frontend: vanilla JS, hash routing (#/ and #/deal/<id>). */
const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
const PREP_Q = "Tell me about Acme Corp and prepare me for the next meeting.";
const QUICK_QS = [
  "What are Acme's biggest objections?",
  "Who are the key stakeholders?",
  "What happened in our previous meetings?",
  "Prepare me for my next Acme meeting.",
  "What should I focus on next?",
];
let pendingPrep = false;

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
  if (!list.length) return `<div class="empty">Nothing recalled yet. Ask a question or generate a meeting brief.</div>`;
  return list.map((m) => `
    <div class="mem-card">
      <p>${esc(m.text)}</p>
      <div class="mem-meta">
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
function briefCard(b, count) {
  const sec = (title, body, cls = "") => `<div class="brief-sec ${cls}"><h3>${title}</h3>${body}</div>`;
  return `
    <div class="brief-head"><h2>Meeting brief</h2><span class="badge mem">Built from ${count} memories recalled from Hindsight</span></div>
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
async function route() {
  const m = location.hash.match(/^#\/deal\/([\w-]+)/);
  app.innerHTML = `<p class="muted"><span class="spinner"></span>Loading…</p>`;
  try { m ? await renderDeal(m[1]) : await renderDashboard(); }
  catch (e) { app.innerHTML = `<div class="banner">${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);

function statusBanner(s) {
  const missing = [];
  if (!s.hindsight_configured) missing.push("<code>HINDSIGHT_API_KEY</code>");
  if (!s.groq_configured) missing.push("<code>GROQ_API_KEY</code>");
  return missing.length
    ? `<div class="banner"><strong>Setup needed.</strong> Add ${missing.join(" and ")} to your <code>.env</code> file and restart the server.</div>` : "";
}

/* ---------- dashboard ---------- */
async function renderDashboard() {
  const [{ deals }, status] = await Promise.all([api("/api/deals"), api("/api/status")]);
  const total = deals.reduce((n, d) => n + d.interaction_count, 0);
  app.innerHTML = `
    <div class="deal-head">
      <div><h1>Active deals</h1><p class="muted">DealPilot recalls what was said in every meeting, so you don't have to.</p></div>
      <div class="stats">
        <div class="stat"><b>${deals.length}</b>active deal${deals.length === 1 ? "" : "s"}</div>
        <div class="stat mem"><b>${total}</b>interactions stored</div>
      </div>
    </div>
    ${statusBanner(status)}
    ${deals.map((d) => `
      <section class="card deal-card">
        <div>
          <h2>${esc(d.name)}</h2>
          <p class="muted small">${d.interaction_count} interaction${d.interaction_count === 1 ? "" : "s"}${d.last_interaction_at ? ` · last on ${esc(fmtDate(d.last_interaction_at))}` : " · nothing stored yet"}</p>
        </div>
        <div class="actions" style="margin:0">
          <a class="btn primary" href="#/deal/${d.id}">Open deal</a>
          <button class="memory" data-prep="${d.id}">Prepare for Acme meeting</button>
          ${d.demo_loaded ? "" : `<button data-demo="${d.id}">Load Acme demo</button>`}
        </div>
      </section>`).join("")}`;
  app.querySelectorAll("[data-prep]").forEach((b) => b.addEventListener("click", () => { pendingPrep = true; location.hash = `#/deal/${b.dataset.prep}`; }));
  app.querySelectorAll("[data-demo]").forEach((b) => b.addEventListener("click", () => withLoading(b, "Loading demo…", async () => {
    try { await post(`/api/deals/${b.dataset.demo}/load-demo`); toast("Acme demo loaded into Hindsight."); await route(); }
    catch (e) { toast(e.message, true); }
  })));
}

/* ---------- deal page ---------- */
async function renderDeal(id) {
  const [deal, status] = await Promise.all([api(`/api/deals/${id}`), api("/api/status")]);
  app.innerHTML = `
    <a href="#/" class="small">← All deals</a>
    <div class="deal-head">
      <div><h1>${esc(deal.name)}</h1><p class="muted">Deal workspace</p></div>
      <div class="stats">
        <div class="stat"><b id="stat-int">${deal.interaction_count}</b>interactions</div>
        <div class="stat mem"><b id="stat-mem">${deal.memory_count ?? "–"}</b>memories in Hindsight</div>
      </div>
    </div>
    ${statusBanner(status)}
    <div class="grid">
      <div>
        <section class="card">
          <h2>Add interaction</h2>
          <p class="sub">Paste notes from a call or meeting. Hindsight extracts and stores what matters.</p>
          <textarea id="note" placeholder="Paste meeting notes or conversation here..."></textarea>
          <div class="actions">
            <button class="primary" id="remember">Remember Interaction</button>
            ${deal.demo_loaded ? "" : `<button id="demo">Load Acme Demo</button>`}
          </div>
        </section>

        <section class="card">
          <h2>Ask DealPilot</h2>
          <p class="sub">Answers are built from memories recalled from Hindsight.</p>
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
          <p class="sub">Same question, same model. The only difference is whether Hindsight memories are provided.</p>
          <p><strong>“${esc(PREP_Q)}”</strong></p>
          <div class="actions">
            <button id="no-mem">Ask Without Memory</button>
            <button class="memory" id="with-mem">Ask With Hindsight</button>
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
          <div class="actions"><button class="memory" id="brief-btn">Generate meeting brief</button></div>
        </section>
        <section class="card">
          <div class="mem-head"><h2>Recalled from Hindsight</h2><span class="badge mem" id="mem-count">0</span></div>
          <div id="mem-list">${memoryCards([])}</div>
        </section>
        <section class="card">
          <h2>Interaction timeline</h2>
          <div id="timeline"></div>
        </section>
      </aside>
    </div>`;

  const base = `/api/deals/${id}`;
  const renderTimeline = (items) => {
    $("#timeline").innerHTML = items.length
      ? `<ul class="timeline">${[...items].reverse().map((i) => `<li><time>${esc(fmtDate(i.created_at))}${i.source === "demo" ? " · demo" : ""}</time>${esc(i.content.length > 150 ? i.content.slice(0, 150) + "…" : i.content)}</li>`).join("")}</ul>`
      : `<div class="empty">No interactions yet. Add notes or load the Acme demo.</div>`;
  };
  renderTimeline(deal.interactions);
  const showMemories = (list) => { $("#mem-list").innerHTML = memoryCards(list); $("#mem-count").textContent = `${list.length} recalled`; };
  const refreshCounts = async () => {
    const d = await api(base);
    $("#stat-int").textContent = d.interaction_count; $("#stat-mem").textContent = d.memory_count ?? "–";
    renderTimeline(d.interactions);
  };
  const showBrief = (r) => {
    $("#brief-slot").innerHTML = `<section class="brief">${briefCard(r.brief, r.memory_count)}</section>`;
    $("#summary").innerHTML = fmt(r.brief.deal_status);
  };

  $("#remember").addEventListener("click", (e) => {
    const note = $("#note").value.trim();
    if (!note) return toast("Paste some meeting notes or conversation first.", true);
    withLoading(e.target, "Remembering…", async () => {
      try { await post(`${base}/interactions`, { content: note }); $("#note").value = ""; toast("Interaction stored in Hindsight."); await refreshCounts(); }
      catch (err) { toast(err.message, true); }
    });
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
    const pending = addMsg("bot", `<span class="spinner"></span>Recalling from Hindsight…`);
    const run = async () => {
      try {
        const r = await post(`${base}/ask`, { question });
        showMemories(r.memories);
        const badge = r.empty ? "" : `<span class="badge mem">Answered from ${r.memory_count} recalled memories</span>`;
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
    try { const r = await post(`${base}/ask-without-memory`, { question: PREP_Q }); $("#out-no").innerHTML = fmt(r.answer); }
    catch (err) { $("#out-no").innerHTML = `<span style="color:var(--err)">${esc(err.message)}</span>`; }
  }));
  $("#with-mem").addEventListener("click", (e) => withLoading(e.target, "Recalling…", async () => {
    $("#out-yes").innerHTML = `<span class="spinner"></span>Recalling from Hindsight…`;
    try {
      const r = await post(`${base}/ask`, { question: PREP_Q, brief: false });
      showMemories(r.memories);
      $("#out-yes").innerHTML = (r.empty ? "" : `<p><span class="badge mem">Based on ${r.memory_count} recalled memories</span></p>`) + fmt(r.answer);
    } catch (err) { $("#out-yes").innerHTML = `<span style="color:var(--err)">${esc(err.message)}</span>`; }
  }));

  if (pendingPrep) { pendingPrep = false; $("#brief-btn").click(); }
}

route();
