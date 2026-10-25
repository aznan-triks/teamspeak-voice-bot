"use strict";

// Everything shown comes from TeamSpeak or user input: always escaped before going into HTML.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const $ = (sel, root = document) => root.querySelector(sel);
const main = $("#main");
let me = null;
let BOT = "Bot"; // the bot's name, from its TeamSpeak nickname
let timer = null; // the current page's refresh loop

// ---- Internationalization (i18n) ----
let currentLang = localStorage.getItem("lang") || (navigator.language && navigator.language.startsWith("en") ? "en" : "fr");
let dict = {};
let fallbackDict = {};

async function loadDictionary(lang) {
  try {
    const res = await fetch(`/i18n/${lang}.json?v=${Date.now()}`);
    if (res.ok) return await res.json();
  } catch (e) {
    console.warn("Could not load dictionary for", lang, e);
  }
  return {};
}

async function initI18n() {
  fallbackDict = await loadDictionary("fr");
  dict = currentLang === "fr" ? fallbackDict : await loadDictionary(currentLang);
  document.documentElement.lang = currentLang;
  updateLangButton();
}

function updateLangButton() {
  const btn = $("#lang-toggle");
  if (btn) {
    btn.textContent = currentLang.toUpperCase();
    btn.setAttribute("aria-label", t("nav.lang"));
    btn.title = currentLang === "fr" ? "English" : "Français";
  }
}

function t(key, params = {}) {
  let text = dict[key] ?? fallbackDict[key] ?? key;
  for (const [k, v] of Object.entries(params)) {
    text = text.replaceAll(`{${k}}`, String(v));
  }
  return text;
}

async function setLanguage(newLang) {
  if (newLang === currentLang && Object.keys(dict).length > 0) return;
  currentLang = newLang;
  localStorage.setItem("lang", currentLang);
  document.documentElement.lang = currentLang;
  dict = currentLang === "fr" ? fallbackDict : await loadDictionary(currentLang);
  updateLangButton();
  if (me) {
    drawRail();
    refreshBanner();
  }
  show();
}

// ---- API client ----
async function api(path, body) {
  const separator = path.includes("?") ? "&" : "?";
  const url = `${path}${separator}lang=${encodeURIComponent(currentLang)}`;
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "Accept-Language": currentLang }, body: JSON.stringify(body) };
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const errorMsg = data.error || `${t("error.internal", { error: r.status })}`;
    throw Object.assign(new Error(errorMsg), { status: r.status });
  }
  return data;
}

// ---- UI feedback: Toast, Confirm Box, Tooltips ----
function toast(text, error = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = "toast" + (error ? " error" : "");
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), error ? 6000 : 3500);
}

function confirmBox(text, okLabel = null) {
  const d = $("#confirm");
  $("#confirm-text").textContent = text;
  $("#confirm-cancel").textContent = t("dialog.cancel");
  $("#confirm-ok").textContent = okLabel || t("dialog.confirm");
  d.showModal();
  return new Promise((resolve) => d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true }));
}

// Tooltip for disabled reason explaining WHY a control cannot be activated
const tooltipEl = $("#tooltip");
function showTooltip(target, text) {
  if (!tooltipEl || !text) return;
  tooltipEl.textContent = text;
  tooltipEl.hidden = false;
  tooltipEl.setAttribute("aria-hidden", "false");
  const rect = target.getBoundingClientRect();
  const tipRect = tooltipEl.getBoundingClientRect();
  let top = rect.top - tipRect.height - 8;
  if (top < 10) top = rect.bottom + 8;
  let left = rect.left + (rect.width / 2) - (tipRect.width / 2);
  left = Math.max(10, Math.min(left, window.innerWidth - tipRect.width - 10));
  tooltipEl.style.top = `${top}px`;
  tooltipEl.style.left = `${left}px`;
}
function hideTooltip() {
  if (!tooltipEl) return;
  tooltipEl.hidden = true;
  tooltipEl.setAttribute("aria-hidden", "true");
}

document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-disabled-reason]");
  if (el && (el.disabled || el.getAttribute("aria-disabled") === "true")) {
    showTooltip(el, el.dataset.disabledReason);
  } else {
    hideTooltip();
  }
});
document.addEventListener("mouseout", (e) => {
  if (e.target.closest("[data-disabled-reason]")) hideTooltip();
});
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-disabled-reason]");
  if (el && (el.disabled || el.getAttribute("aria-disabled") === "true")) {
    e.preventDefault();
    e.stopPropagation();
    toast(el.dataset.disabledReason, false);
  }
}, true);

// ---- Date & Time Formatters ----
const when = (ms) => new Date(ms).toLocaleString(currentLang === "en" ? "en-US" : "fr-FR", { dateStyle: "short", timeStyle: "short" });
const secs = (ms) => (ms == null ? "—" : `${(ms / 1000).toLocaleString(currentLang === "en" ? "en-US" : "fr-FR", { maximumFractionDigits: 1 })} s`);
const ago = (ms) => {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 60) return t("time.just_now");
  if (s < 3600) return t("time.mins_ago", { n: Math.round(s / 60) });
  if (s < 86400) return t("time.hours_ago", { n: Math.round(s / 3600) });
  return t("time.days_ago", { n: Math.round(s / 86400) });
};

// Round avatar with consistent color from user name
function avatar(name, talking = false) {
  const n = String(name || "?");
  let h = 0;
  for (const c of n) h = (h * 31 + c.codePointAt(0)) % 360;
  const initials = n.replace(/\(.*?\)/g, "").trim().split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase() || "?";
  return `<span class="avatar${talking ? " talking" : ""}" style="--avatar:hsl(${h} 42% 40%)" aria-hidden="true">${esc(initials)}</span>`;
}

function getStatusLabel(status) {
  return t(`status.${status}`) || status;
}

// ---- Frame: Navigation Rail, Lamp, Theme ----
const PAGES = {
  conversation: { key: "page.conversation", render: pageConversation },
  phrases: { key: "page.phrases", render: pagePhrases },
  speak: { key: "page.speak", render: pageSpeak, admin: true },
  stats: { key: "page.stats", render: pageStats },
  actions: { key: "page.actions", render: pageActions },
  profile: { key: "page.profile", render: pageProfile },
  live: { key: "page.live", render: pageLive, admin: true },
  services: { key: "page.services", render: pageServices, admin: true },
  settings: { key: "page.settings", render: pageSettings, admin: true },
  people: { key: "page.people", render: pagePeople, admin: true },
  plugins: { key: "page.plugins", render: pagePlugins, admin: true },
  logs: { key: "page.logs", render: pageLogs, admin: true },
};

function drawRail() {
  const link = (id) => `<a href="#/${id}" data-page="${id}">${esc(t(PAGES[id].key))}</a>`;
  const playerPages = ["conversation", "phrases", "stats", "actions", "profile"];
  const adminPages = ["live", "speak", "services", "settings", "people", "plugins", "logs"];
  $("#rail").innerHTML = `
    <div class="brand">${avatar(BOT)}<div><strong>${esc(BOT)}</strong><small>${esc(t("meta.title"))}</small></div></div>
    <div class="group">${esc(t("nav.group.player"))}</div>
    ${playerPages.map(link).join("")}
    ${me.admin ? `
      <div class="group">${esc(t("nav.group.admin"))}</div>
      ${adminPages.map(link).join("")}` : ""}
    <div class="who">${avatar(me.identity || me.nickname)}<div><strong>${esc(me.identity || me.nickname)}</strong>${me.admin ? esc(t("nav.admin")) : esc(t("nav.member"))}<br><a href="#/profile">${esc(t("nav.profile"))}</a> · <a href="#" id="logout">${esc(t("nav.logout"))}</a></div></div>`;
  $("#logout").onclick = async (e) => {
    e.preventDefault();
    await api("/api/logout", {});
    location.hash = "";
    location.reload();
  };
}

function setLamp(state, text) {
  const l = $("#lamp");
  l.dataset.state = state;
  $(".lamp-text", l).textContent = text;
}

async function refreshLamp() {
  if (!me?.admin) {
    try {
      const p = await api("/api/presence");
      if (!p.bot_online) setLamp("down", t("lamp.down"));
      else if (p.talking) setLamp("live", t("lamp.live_talking"));
      else setLamp("listening", t("lamp.listening"));
    } catch (e) {
      setLamp("down", e.status === 503 ? t("lamp.not_responding") : t("lamp.down"));
    }
    return;
  }
  try {
    const s = await api("/api/state");
    const talking = (s.speaking || []).some((x) => Date.now() - x.ms < 3000);
    if (!s.me) setLamp("down", t("lamp.down"));
    else if (s.llm?.degraded) setLamp("down", t("lamp.degraded"));
    else if (talking || s.llm?.pending) setLamp("live", talking ? t("lamp.live_talking") : t("lamp.live_waiting"));
    else setLamp("listening", t("lamp.listening"));
    return s;
  } catch (e) {
    setLamp("down", e.status === 503 ? t("lamp.not_responding") : t("lamp.down"));
  }
}

async function refreshBanner() {
  if (!me?.admin) return;
  const b = $("#banner");
  try {
    const s = await api("/api/settings");
    b.hidden = !s.pending_restart;
    b.innerHTML = `<span>${esc(t("banner.pending_restart"))}</span>
      <button class="btn primary" id="apply">${esc(t("banner.apply_now"))}</button>`;
    $("#apply").onclick = applyNow;
  } catch {
    b.hidden = true;
  }
}

async function applyNow() {
  if (!(await confirmBox(t("banner.restart_confirm"), t("services.btn_restart")))) return;
  await api("/api/restart", {});
  toast(t("banner.restarting"));
  $("#banner").hidden = true;
}

async function show() {
  clearInterval(timer);
  const home = me?.admin ? "live" : "conversation";
  const id = location.hash.replace("#/", "") || home;
  const page = PAGES[id] && (!PAGES[id].admin || me?.admin) ? id : home;
  document.querySelectorAll("#rail a[data-page]").forEach((a) => (a.dataset.page === page ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  document.title = `${t(PAGES[page].key)} — ${BOT}`;
  $("#rail").classList.remove("open");
  $("#menu-toggle").setAttribute("aria-expanded", "false");
  main.innerHTML = `<div class="loading-state" role="status"><span class="spinner" aria-hidden="true"></span><p class="muted">${esc(t("lamp.connecting"))}</p></div>`;
  window.scrollTo(0, 0);
  try {
    await PAGES[page].render();
  } catch (e) {
    main.innerHTML = `<div class="panel stack" role="alert"><p class="bad">${esc(e.message)}</p><button class="btn" onclick="location.reload()">${esc(t("dialog.confirm"))}</button></div>`;
  }
  main.focus({ preventScroll: true });
}

// ---- Login Page ----
async function pageLogin() {
  $("#rail").hidden = true;
  setLamp("listening", BOT);
  main.innerHTML = `<section class="login">
    <div class="row" style="margin-bottom:16px">${avatar(BOT)}<h1 style="margin:0">${esc(t("login.title"))}</h1></div>
    <p class="lede">${esc(t("login.lede", { bot: BOT }))}</p>
    <div class="panel stack" id="login-box"><div class="loading-state"><span class="spinner" aria-hidden="true"></span><p class="muted">${esc(t("login.searching"))}</p></div></div>
  </section>`;
  const box = $("#login-box");
  const choose = async () => {
    let people;
    try {
      people = await api("/api/login/clients");
    } catch (e) {
      box.innerHTML = `<p class="bad">${esc(e.message)}</p><p class="muted">${esc(t("login.retry_auto"))}</p>`;
      return;
    }
    if (!people.length) {
      box.innerHTML = `<div class="empty-state"><p>${esc(t("login.empty"))}</p></div>`;
      return;
    }
    const picked = $("#who")?.value;
    box.innerHTML = `
      <div class="field">
        <label for="who">${esc(t("login.label_who"))}</label>
        <select id="who" aria-describedby="who-help">${people.map((p) => `<option value="${p.client_id}">${esc(p.nickname)}</option>`).join("")}</select>
        <p id="who-help" class="help">${esc(t("login.help_who"))}</p>
      </div>
      <button class="btn primary" id="send">${esc(t("login.btn_send"))}</button>`;
    if (picked && people.some((p) => String(p.client_id) === picked)) $("#who").value = picked;
    $("#send").onclick = () => send(Number($("#who").value), $("#who").selectedOptions[0].textContent);
  };
  const send = async (id, name) => {
    clearInterval(timer);
    try {
      const r = await api("/api/login/start", { client_id: id });
      box.innerHTML = `
        <p><strong>${esc(t("login.code_sent", { name: name, minutes: Math.round(r.ttl_s / 60), bot: BOT }))}</strong></p>
        <div class="field">
          <label for="code">${esc(t("login.label_code"))}</label>
          <input id="code" class="code-input" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="000000" aria-label="${esc(t("login.label_code"))}">
        </div>
        <div class="row">
          <button class="btn primary" id="go">${esc(t("login.btn_submit"))}</button>
          <button class="btn ghost" id="again">${esc(t("login.btn_resend"))}</button>
          <button class="btn ghost" id="other">${esc(t("login.btn_change"))}</button>
        </div>`;
      $("#code").focus();
      const go = async () => {
        try {
          await api("/api/login/finish", { client_id: id, code: $("#code").value });
          location.reload();
        } catch (e) {
          toast(e.message, true);
          $("#code").select();
        }
      };
      $("#go").onclick = go;
      $("#code").oninput = (e) => {
        const found = e.target.value.match(/\d{6}/);
        e.target.value = found ? found[0] : e.target.value.replace(/\D/g, "").slice(0, 6);
        if (found) go();
      };
      $("#again").onclick = () => send(id, name);
      $("#other").onclick = () => { choose(); timer = setInterval(choose, 5000); };
    } catch (e) {
      toast(e.message, true);
    }
  };
  await choose();
  timer = setInterval(() => { if (!$("#code")) choose(); }, 5000);
}

// ---- Member Pages ----

async function pagePhrases() {
  main.innerHTML = `
    <h1>${esc(t("phrases.title"))}</h1>
    <p class="lede">${esc(t("phrases.lede"))}</p>
    <div class="row" style="margin-bottom:20px">
      <input type="search" id="q" placeholder="${esc(t("phrases.search_placeholder"))}" aria-label="${esc(t("phrases.search_label"))}">
    </div>
    <div id="list" class="subtitles" style="max-height:none" role="feed" aria-busy="true">
      <div class="loading-state"><span class="spinner" aria-hidden="true"></span></div>
    </div>
    <div class="row" style="margin-top:16px">
      <button class="btn" id="more" hidden>${esc(t("phrases.more"))}</button>
    </div>`;
  let last = 0;
  const load = async (reset) => {
    if (reset) { last = 0; $("#list").innerHTML = ""; }
    $("#list").setAttribute("aria-busy", "true");
    const query = $("#q").value.trim();
    const rows = await api(`/api/my/phrases?q=${encodeURIComponent(query)}&before_id=${last}`);
    $("#list").setAttribute("aria-busy", "false");
    if (reset && !rows.length) {
      const msg = query ? t("phrases.empty_query") : t("phrases.empty", { bot: BOT });
      $("#list").innerHTML = `<div class="empty-state"><p>${esc(msg)}</p></div>`;
    }
    $("#list").insertAdjacentHTML("beforeend", rows.map((r) => `
      <article class="line">
        ${avatar(me.identity || me.nickname)}
        <div class="who">${esc(me.identity || me.nickname)}<time datetime="${new Date(r.started_ms).toISOString()}">${when(r.started_ms)}, ${t("phrases.duration", { duration: secs(r.duration_ms) })}</time></div>
        <p class="said">${esc(r.text)}</p>
      </article>`).join(""));
    if (rows.length) last = rows[rows.length - 1].id;
    $("#more").hidden = rows.length < 50;
  };
  let wait;
  $("#q").oninput = () => { clearTimeout(wait); wait = setTimeout(() => load(true), 300); };
  $("#more").onclick = () => load(false);
  await load(true);
}

function formatActionRequest(a) {
  const isPoints = a.name === "scores.add" || a.name.endsWith(".add_points");
  const label = isPoints ? t("actions.add_points") : a.name;
  let details = "";
  if (isPoints && a.args) {
    const p = a.args.player || "?";
    const pts = a.args.points != null ? a.args.points : 1;
    details = `<span class="chip">${esc(p)} : ${pts > 0 ? "+" : ""}${pts} pt${Math.abs(pts) > 1 ? "s" : ""}</span>`;
  } else {
    details = esc(Object.entries(a.args || {}).map(([k, v]) => `${k} : ${v}`).join(", "));
  }
  return `<strong>${esc(label)}</strong> ${details}${a.sentence ? `<div class="quote">« ${esc(a.sentence)} »</div>` : ""}`;
}

async function pageActions() {
  const ledeExtra = me.admin ? "" : t("actions.lede_member");
  main.innerHTML = `
    <h1>${esc(t("actions.title"))}</h1>
    <p class="lede">${esc(t("actions.lede"))}${esc(ledeExtra)}</p>
    <div id="list"><div class="loading-state"><span class="spinner" aria-hidden="true"></span></div></div>`;
  const rows = await api("/api/actions");
  if (!rows.length) {
    $("#list").innerHTML = `<div class="panel empty-state"><p>${esc(t("actions.empty", { bot: BOT }))}</p></div>`;
    return;
  }
  $("#list").innerHTML = `
    <div class="table-wrap">
      <table class="table responsive">
        <thead>
          <tr>
            <th scope="col">${esc(t("actions.th_when"))}</th>
            <th scope="col">${esc(t("actions.th_who"))}</th>
            <th scope="col">${esc(t("actions.th_request"))}</th>
            <th scope="col">${esc(t("actions.th_result"))}</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((a) => `
            <tr>
              <td><time datetime="${new Date(a.created_ms).toISOString()}">${when(a.created_ms)}</time></td>
              <td><strong>${esc(a.identity || "?")}</strong></td>
              <td>
                ${formatActionRequest(a)}
              </td>
              <td>
                <span class="chip ${esc(a.status)}">${esc(getStatusLabel(a.status))}</span>
                ${a.result ? `<div class="muted">${esc(a.result)}</div>` : ""}
              </td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

// Conversation feed shared between Conversation page and Live page
function conversationFeed(list, statusEl) {
  let last = 0;
  let first = true;
  return async () => {
    const [rows, pres] = await Promise.all([api(`/api/conversation?after_id=${last}`), api("/api/presence")]);
    statusEl.className = "hearing " + (pres.hearing ? "on" : "off");
    statusEl.innerHTML = pres.hearing
      ? esc(t("presence.hearing"))
      : `<span>${esc(t("presence.not_hearing", { reason: pres.reason }))}</span><div class="help-tip" style="margin-top:6px;font-size:13px;opacity:0.9">💡 ${esc(t("conversation.help_presence_tip", { bot: BOT }))}</div>`;
    if (first && !rows.length) {
      list.innerHTML = `<div class="empty-state"><p>${esc(t("conversation.empty", { bot: BOT }))}</p></div>`;
    }
    if (!rows.length) return;
    if (first || last === 0) list.innerHTML = "";
    first = false;
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
    list.insertAdjacentHTML("beforeend", rows.map((r) => `
      <div class="line${r.name && r.name.startsWith(BOT) ? " bot" : ""}">
        ${avatar(r.name)}
        <div class="who">${esc(r.name)}<time datetime="${new Date(r.started_ms).toISOString()}">${when(r.started_ms)}</time></div>
        <p class="said">${esc(r.text)}</p>
      </div>`).join(""));
    last = rows[rows.length - 1].id;
    if (atBottom) list.scrollTop = list.scrollHeight;
  };
}

async function pageConversation() {
  main.innerHTML = `
    <h1>${esc(t("conversation.title"))}</h1>
    <p class="lede">${esc(t("conversation.lede", { bot: BOT }))}</p>
    <div class="panel guide-card" style="margin-bottom:var(--space-4)">
      <div class="split" style="grid-template-columns:1fr auto;gap:var(--space-3);align-items:center">
        <div>
          <h2 style="margin:0 0 4px">${esc(t("conversation.guide_title", { bot: BOT }))}</h2>
          <p class="muted" style="margin:0;font-size:13.5px;line-height:1.4">${esc(t("conversation.guide_text", { bot: BOT }))}</p>
        </div>
        <div class="row" style="gap:8px">
          ${me.admin ? `<a href="#/speak" class="btn primary">${esc(t("conversation.btn_speak"))}</a>` : ""}
          <a href="#/stats" class="btn">${esc(t("conversation.btn_stats"))}</a>
          <a href="#/profile" class="btn ghost">${esc(t("conversation.btn_profile"))}</a>
        </div>
      </div>
    </div>
    <p id="hearing" class="hearing"></p>
    <section class="panel" aria-label="${esc(t("conversation.title"))}">
      <div id="lines" class="subtitles" role="log" aria-live="polite"></div>
    </section>`;
  const draw = conversationFeed($("#lines"), $("#hearing"));
  await draw();
  timer = setInterval(draw, 2000);
}

async function pageProfile() {
  main.innerHTML = `
    <h1>${esc(t("profile.title"))}</h1>
    <p class="lede">${esc(t("profile.lede", { bot: BOT }))}</p>
    <div class="panel stack"><div class="loading-state"><span class="spinner" aria-hidden="true"></span></div></div>`;

  const prof = await api("/api/my/profile").catch(() => ({ uid: me.uid, identity: me.identity || "", opt_out: false }));

  main.innerHTML = `
    <h1>${esc(t("profile.title"))}</h1>
    <p class="lede">${esc(t("profile.lede", { bot: BOT }))}</p>
    <form id="profile-form" class="stack">
      <fieldset class="panel stack">
        <legend>${esc(t("profile.sec_identity"))}</legend>
        <div class="field">
          <label for="prof-id">${esc(t("profile.identity_label"))}</label>
          <input id="prof-id" type="text" value="${esc(prof.identity || "")}" readonly aria-describedby="prof-id-help">
          <p id="prof-id-help" class="help">${esc(t("profile.identity_help"))}</p>
        </div>
        <div class="field">
          <label for="prof-nick">${esc(t("profile.ts_nickname_label"))}</label>
          <input id="prof-nick" type="text" value="${esc(me.nickname || "")}" readonly disabled style="opacity:0.8">
        </div>
        <div class="field">
          <label for="prof-uid">${esc(t("profile.uid_label"))}</label>
          <input id="prof-uid" type="text" value="${esc(prof.uid || me.uid)}" readonly disabled style="font-family:var(--mono);font-size:12px;opacity:0.8" aria-describedby="prof-uid-help">
          <p id="prof-uid-help" class="help">${esc(t("profile.uid_help"))}</p>
        </div>
      </fieldset>

      <fieldset class="panel stack">
        <legend>${esc(t("profile.sec_privacy"))}</legend>
        <div class="field">
          <label class="switch">
            <input type="checkbox" id="prof-optout" ${prof.opt_out ? "checked" : ""}>
            <span>${esc(t("profile.opt_out_label"))}</span>
          </label>
          <p class="help">${esc(t("profile.opt_out_help"))}</p>
        </div>
      </fieldset>

      <div class="row-end">
        <button type="submit" class="btn primary" id="save-profile">${esc(t("profile.btn_save"))}</button>
      </div>
    </form>`;

  $("#profile-form").onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#save-profile");
    btn.disabled = true;
    try {
      const saved = await api("/api/my/profile", { opt_out: $("#prof-optout").checked });
      me.opt_out = saved.opt_out;
      drawRail();
      toast(t("profile.saved_toast"));
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  };
}

// ---- Admin Pages ----

async function pageServices() {
  main.innerHTML = `
    <h1>${esc(t("services.title"))}</h1>
    <p class="lede">${esc(t("services.lede", { bot: BOT }))}</p>
    <div id="services" class="stack"><div class="loading-state"><span class="spinner" aria-hidden="true"></span></div></div>`;
  const openLogs = new Set();
  const draw = async () => {
    let list;
    try {
      list = await api("/api/services");
    } catch (e) {
      $("#services").innerHTML = `<div class="panel error-state"><p class="bad">${esc(e.message)}</p></div>`;
      return;
    }
    if (!list.length) {
      $("#services").innerHTML = `<div class="panel empty-state"><p>${esc(t("services.empty"))}</p></div>`;
      return;
    }
    const logs = {};
    await Promise.all([...openLogs].map(async (n) => {
      logs[n] = await api(`/api/services/logs?name=${encodeURIComponent(n)}&lines=150`).catch(() => []);
    }));
    $("#services").innerHTML = list.map((s) => `
      <section class="panel stack" data-service="${esc(s.name)}">
        <div class="row">
          <span class="lamp-dot ${s.active === "active" ? "on" : s.active === "failed" ? "down" : ""}" aria-hidden="true"></span>
          <div>
            <strong>${esc(t(`services.label.${s.name}`) || s.name)}</strong>
            <div class="muted">
              ${esc(t(`services.state.${s.active}`) || s.active)}
              ${s.active === "active" && s.since ? esc(t("services.since", { time: when(Number(s.since.replace("@", "")) * 1000) })) : ""}
              ${Number(s.restarts) ? esc(t("services.restarts", { count: s.restarts })) : ""}
            </div>
          </div>
          <div class="row" style="margin-left:auto">
            ${s.active === "active" ? "" : `<button class="btn primary" data-act="start">${esc(t("services.btn_start"))}</button>`}
            <button class="btn" data-act="restart">${esc(t("services.btn_restart"))}</button>
            ${s.active === "active" ? (s.name === "web" ? `<button class="btn danger" data-act="stop" disabled data-disabled-reason="${esc(t("reasons.site_self_stop"))}">${esc(t("services.btn_stop"))}</button>` : `<button class="btn danger" data-act="stop">${esc(t("services.btn_stop"))}</button>`) : ""}
            <button class="btn ghost" data-logs>${openLogs.has(s.name) ? esc(t("services.btn_hide_logs")) : esc(t("services.btn_show_logs"))}</button>
          </div>
        </div>
        ${openLogs.has(s.name) ? `<div class="logs" style="height:40vh" tabindex="0" role="region" aria-label="${esc(s.name)} log">${(logs[s.name] || []).map((l) => `<div class="${/error|failed|traceback/i.test(l) ? "ERROR" : /warn/i.test(l) ? "WARNING" : ""}">${esc(l)}</div>`).join("") || esc(t("services.empty_logs"))}</div>` : ""}
      </section>`).join("");
    document.querySelectorAll(".logs").forEach((b) => (b.scrollTop = b.scrollHeight));
    document.querySelectorAll("[data-service]").forEach((sec) => {
      const name = sec.dataset.service;
      sec.querySelector("[data-logs]").onclick = () => {
        openLogs.has(name) ? openLogs.delete(name) : openLogs.add(name);
        draw();
      };
      sec.querySelectorAll("[data-act]:not(:disabled)").forEach((b) => (b.onclick = async () => {
        const act = b.dataset.act;
        const actionLabel = act === "start" ? t("services.btn_start") : act === "stop" ? t("services.btn_stop") : t("services.btn_restart");
        const warn = name === "web" ? t("services.web_restart_warn") : name === "tsbot" ? t("services.tsbot_restart_warn") : "";
        const serviceName = t(`services.label.${name}`) || name;
        if (act !== "start" && !(await confirmBox(t("services.confirm_prompt", { action: actionLabel, service: serviceName, warning: warn }), actionLabel))) return;
        try {
          await api("/api/services", { name, action: act });
          toast(t("services.action_requested", { action: actionLabel }));
        } catch (e) {
          toast(e.message, true);
        }
        setTimeout(draw, name === "web" ? 4000 : 1500);
      }));
    });
  };
  await draw();
  timer = setInterval(draw, 5000);
}

async function pageLive() {
  main.innerHTML = `
    <h1>${esc(t("live.title"))}</h1>
    <p class="lede">${esc(t("live.lede"))}</p>
    <dl class="facts" id="facts"><div class="loading-state"><span class="spinner" aria-hidden="true"></span></div></dl>
    <div class="split">
      <section class="panel">
        <h2>${esc(t("page.conversation"))}</h2>
        <p id="hearing" class="hearing"></p>
        <div id="lines" class="subtitles" role="log" aria-live="polite"></div>
      </section>
      <section class="panel">
        <h2>${esc(t("live.in_channel"))}</h2>
        <ul class="people" id="people" aria-label="${esc(t("live.in_channel"))}"></ul>
      </section>
    </div>`;
  const feed = conversationFeed($("#lines"), $("#hearing"));
  const draw = async () => {
    feed().catch(() => {});
    const s = await refreshLamp();
    if (!s) {
      $("#facts").innerHTML = `<div><dt>Bot</dt><dd class="bad">${esc(t("live.bot_unresponsive"))}</dd></div>`;
      return;
    }
    const fact = (k, v, cls = "") => `<div><dt>${esc(k)}</dt><dd class="${cls}">${esc(v)}</dd></div>`;
    $("#facts").innerHTML =
      fact(t("live.fact_teamspeak"), s.me ? t("live.ts_connected") : t("live.ts_disconnected"), s.me ? "good" : "bad") +
      fact(t("live.fact_llm"), s.llm == null ? t("live.llm_disabled") : s.llm.degraded ? t("live.llm_degraded") : t("live.llm_ready"), s.llm?.degraded ? "bad" : "good") +
      fact(t("live.fact_voice"), s.voice || t("live.voice_unused")) +
      fact(t("live.fact_plugins"), s.plugins.length ? s.plugins.join(", ") : t("live.none")) +
      fact(t("live.fact_started"), ago(s.started_ms));
    const channel = s.me?.channel_id;
    const here = s.clients.filter((c) => c.channel_id === channel && c.client_id !== s.me?.client_id);
    const talking = new Set((s.speaking || []).filter((x) => Date.now() - x.ms < 3000).map((x) => x.client_id));
    $("#people").innerHTML = here.length
      ? here.map((c) => `
        <li class="${talking.has(c.client_id) ? "talking" : ""}">
          ${avatar(c.nickname, talking.has(c.client_id))}
          ${esc(c.nickname)}
          <span class="id">${esc(c.identity || t("live.unknown_id"))}</span>
        </li>`).join("")
      : `<li class="muted">${esc(t("live.channel_empty"))}</li>`;
  };
  await draw();
  timer = setInterval(draw, 2000);
}

async function pageSpeak() {
  main.innerHTML = `
    <h1>${esc(t("speak.title"))}</h1>
    <p class="lede">${esc(t("speak.lede"))}</p>
    <div class="panel stack">
      <div class="field">
        <label for="text">${esc(t("speak.label_phrase"))}</label>
        <textarea id="text" maxlength="400" placeholder="${esc(t("speak.placeholder"))}"></textarea>
      </div>
      <div class="row">
        <button class="btn" id="preview" disabled data-disabled-reason="${esc(t("reasons.phrase_required"))}">${esc(t("speak.btn_preview"))}</button>
        <button class="btn primary" id="say" disabled data-disabled-reason="${esc(t("reasons.phrase_required"))}">${esc(t("speak.btn_say"))}</button>
      </div>
      <audio id="player" controls hidden style="width:100%;margin-top:12px"></audio>
    </div>`;
  const textInput = $("#text");
  const updateButtons = () => {
    const val = textInput.value.trim();
    const hasText = Boolean(val);
    $("#preview").disabled = !hasText;
    $("#say").disabled = !hasText;
  };
  textInput.oninput = updateButtons;

  $("#preview").onclick = async () => {
    const val = textInput.value.trim();
    if (!val) return toast(t("speak.toast_empty"), true);
    $("#preview").disabled = true;
    try {
      const r = await fetch(`/api/voice?text=${encodeURIComponent(val)}&lang=${encodeURIComponent(currentLang)}`);
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        return toast(err.error || t("speak.toast_voice_error"), true);
      }
      const player = $("#player");
      player.src = URL.createObjectURL(await r.blob());
      player.hidden = false;
      player.play();
    } finally {
      updateButtons();
    }
  };

  $("#say").onclick = async () => {
    const val = textInput.value.trim();
    if (!val) return toast(t("speak.toast_empty"), true);
    $("#say").disabled = true;
    try {
      const r = await api("/api/say", { text: val });
      toast(t("speak.toast_spoken", { duration: secs(r.seconds * 1000) }));
    } catch (e) {
      toast(e.message, true);
    } finally {
      updateButtons();
    }
  };
}

async function pageSettings() {
  const s = await api("/api/settings");
  const groups = {};
  s.fields.forEach((f) => (groups[f.section] ||= []).push(f));
  const getSectionTitle = (sec) => {
    const key = `settings.sec.${sec.replace(/\./g, "_")}`;
    return t(key) || sec;
  };

  const inputControl = (f) => {
    const id = `f-${f.section}-${f.key}`.replace(/\./g, "-");
    const v = f.value;
    let ctrl;
    if (f.type === "bool") {
      return `
        <div class="field" data-key="${f.section}.${f.key}" data-type="bool">
          <label class="switch">
            <input type="checkbox" id="${id}" ${v ? "checked" : ""}>
            <span>${esc(f.label)}</span>
          </label>
          ${f.help ? `<p class="help">${esc(f.help)}</p>` : ""}
        </div>`;
    }
    if (f.type === "list") ctrl = `<textarea id="${id}" aria-describedby="${id}-help">${esc((v || []).join("\n"))}</textarea>`;
    else if (f.type === "text") ctrl = `<textarea id="${id}" rows="5" aria-describedby="${id}-help">${esc(v ?? "")}</textarea>`;
    else if (f.type.startsWith("choice:")) ctrl = `<select id="${id}" aria-describedby="${id}-help">${f.type.slice(7).split(",").map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    else if (f.type === "secret") ctrl = `<input id="${id}" type="password" autocomplete="new-password" value="${esc(v ?? "")}" placeholder="${esc(t("settings.none_placeholder"))}" aria-describedby="${id}-help">`;
    else ctrl = `<input id="${id}" type="${f.type === "int" ? "number" : "text"}" ${f.type === "int" ? 'min="0"' : ""} value="${esc(v ?? "")}" placeholder="${esc(f.default ?? "")}" aria-describedby="${id}-help">`;
    return `
      <div class="field" data-key="${f.section}.${f.key}" data-type="${f.type}">
        <label for="${id}">${esc(f.label)}</label>
        ${ctrl}
        ${f.help ? `<p id="${id}-help" class="help">${esc(f.help)}</p>` : ""}
      </div>`;
  };

  main.innerHTML = `
    <h1>${esc(t("settings.title"))}</h1>
    <p class="lede">${esc(t("settings.lede"))}</p>
    <form id="form" class="stack">
      ${Object.entries(groups).map(([sec, fields]) => `
        <fieldset class="panel">
          <legend>${esc(getSectionTitle(sec))}</legend>
          ${fields.map(inputControl).join("")}
        </fieldset>`).join("")}
      <div class="row-end" style="position:sticky;bottom:12px;z-index:50">
        <button class="btn primary" id="save" type="submit" disabled data-disabled-reason="${esc(t("reasons.no_changes"))}">${esc(t("settings.btn_save"))}</button>
      </div>
    </form>`;

  const read = (el) => {
    const c = el.querySelector("input,textarea,select");
    const tp = el.dataset.type;
    if (tp === "bool") return c.checked;
    if (tp === "int") return c.value === "" ? null : Number(c.value);
    if (tp === "list") return c.value.split("\n").map((x) => x.trim()).filter(Boolean);
    return c.value;
  };
  const initial = new Map([...main.querySelectorAll(".field")].map((el) => [el.dataset.key, JSON.stringify(read(el))]));
  const changed = () => [...main.querySelectorAll(".field")].filter((el) => JSON.stringify(read(el)) !== initial.get(el.dataset.key));

  $("#form").oninput = () => {
    const count = changed().length;
    main.querySelectorAll(".field").forEach((el) => el.classList.toggle("changed", changed().includes(el)));
    const saveBtn = $("#save");
    saveBtn.disabled = count === 0;
    saveBtn.textContent = count ? t("settings.btn_save_changes", { count }) : t("settings.btn_save");
  };

  $("#form").onsubmit = async (e) => {
    e.preventDefault();
    const fields = Object.fromEntries(changed().map((el) => [el.dataset.key, read(el)]));
    try {
      await api("/api/settings", { fields });
      toast(t("settings.saved_toast"));
      await refreshBanner();
      await pageSettings();
    } catch (err) {
      toast(err.message, true);
    }
  };
}

async function pagePeople() {
  const [s, state] = await Promise.all([api("/api/settings"), api("/api/state").catch(() => ({ clients: [] }))]);
  const online = (state.clients || []).filter((c) => c.uid && c.client_id !== state.me?.client_id);
  const nameOf = (uid) => online.find((c) => c.uid === uid)?.nickname;
  const fieldVal = (key) => s.fields.find((f) => `${f.section}.${f.key}` === key)?.value ?? (key === "web.admin_groups" ? ["Server Admin"] : []);
  const admins = { uid: new Set(fieldVal("web.admin_uids")), group: new Set(fieldVal("web.admin_groups")) };
  let adminsChanged = false;
  const groups = [...new Set(online.flatMap((c) => c.server_groups || []))].sort();

  let rows = [...Object.entries(s.identities || {}), ...[...admins.uid].map((u) => `uid:${u}`), ...[...admins.group].map((g) => `group:${g}`)]
    .map((r) => (Array.isArray(r) ? r : [r, ""])).filter(([k], i, all) => all.findIndex(([x]) => x === k) === i);
  let excluded = [...(s.opt_out_uids || [])];
  const kind = (k) => (k.startsWith("group:") ? "group" : "uid");
  const isAdmin = (k) => admins[kind(k)].has(k.slice(k.indexOf(":") + 1));
  const whoDisplay = (k) => {
    if (kind(k) === "group") {
      const gname = k.slice(6);
      return `${esc(t("people.group_desc", { name: gname }))}<div class="muted">${esc(t("people.group_sub"))}</div>`;
    }
    const targetUid = k.slice(4);
    const nick = nameOf(targetUid);
    return nick
      ? `${esc(nick)}<div class="muted">${esc(t("people.online_desc"))}</div>`
      : `${esc(t("people.offline_desc"))}<div class="muted">${esc(t("people.offline_sub", { uid: targetUid.slice(0, 8) }))}</div>`;
  };
  const isMine = (k) => k === `uid:${me.uid}`;

  const draw = () => {
    const used = new Set(rows.map(([k]) => k));
    main.innerHTML = `
      <h1>${esc(t("people.title"))}</h1>
      <p class="lede">${esc(t("people.lede"))}</p>
      <section class="panel stack">
        <h2>${esc(t("people.sec_identities"))}</h2>
        <div class="table-wrap">
          <table class="table responsive">
            <thead>
              <tr>
                <th scope="col">${esc(t("people.th_target"))}</th>
                <th scope="col">${esc(t("people.th_player"))}</th>
                <th scope="col">${esc(t("people.th_admin"))}</th>
                <th scope="col"><span class="skip">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              ${rows.map(([k, v], i) => `
                <tr>
                  <td>${whoDisplay(k)}</td>
                  <td><input type="text" data-i="${i}" value="${esc(v)}" aria-label="${esc(t("people.th_player"))}" placeholder="${esc(t("people.input_player_placeholder"))}"></td>
                  <td>
                    <label class="switch">
                      <input type="checkbox" data-admin="${i}" ${isAdmin(k) ? "checked" : ""} ${isMine(k) ? `disabled data-disabled-reason="${esc(t("people.cannot_remove_own_admin"))}"` : ""}>
                      <span>${esc(t("people.th_admin"))}</span>
                    </label>
                  </td>
                  <td>
                    ${isMine(k) ? `<button class="btn ghost danger" disabled data-disabled-reason="${esc(t("people.toast_self_remove"))}">${esc(t("people.btn_remove"))}</button>` : `<button class="btn ghost danger" data-del="${i}">${esc(t("people.btn_remove"))}</button>`}
                  </td>
                </tr>`).join("") || `<tr><td colspan="4" class="muted">${esc(t("people.empty_identities"))}</td></tr>`}
            </tbody>
          </table>
        </div>
        <div class="row">
          <select id="add-who" aria-label="${esc(t("people.add_person_placeholder"))}">
            <option value="">${esc(t("people.add_person_placeholder"))}</option>
            ${online.filter((c) => !used.has(`uid:${c.uid}`)).map((c) => `<option value="${esc(c.uid)}">${esc(c.nickname)}</option>`).join("")}
          </select>
          <select id="add-group" aria-label="${esc(t("people.add_group_placeholder"))}">
            <option value="">${esc(t("people.add_group_placeholder"))}</option>
            ${groups.filter((g) => !used.has(`group:${g}`)).map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("")}
          </select>
        </div>
        <p class="muted" style="margin-top:6px">${esc(t("people.help_footer"))}</p>
      </section>

      <section class="panel stack">
        <h2>${esc(t("people.sec_exclusions"))}</h2>
        <p class="muted">${esc(t("people.exclusions_help"))}</p>
        <ul class="people">
          ${excluded.map((uid, i) => `
            <li>
              <span>${esc(nameOf(uid) || uid)}</span>
              <button class="btn ghost danger" style="margin-left:auto" data-unex="${i}">${esc(t("people.btn_remove"))}</button>
            </li>`).join("") || `<li class="muted">${esc(t("people.empty_exclusions"))}</li>`}
        </ul>
        <select id="add-ex" aria-label="${esc(t("people.exclude_placeholder"))}">
          <option value="">${esc(t("people.exclude_placeholder"))}</option>
          ${online.filter((c) => !excluded.includes(c.uid)).map((c) => `<option value="${esc(c.uid)}">${esc(c.nickname)}</option>`).join("")}
        </select>
      </section>

      <div class="row-end"><button class="btn primary" id="save">${esc(t("settings.btn_save"))}</button></div>`;

    main.querySelectorAll("input[data-i]").forEach((el) => (el.oninput = () => { rows[el.dataset.i][1] = el.value; }));
    main.querySelectorAll("[data-admin]").forEach((el) => (el.onchange = () => {
      const k = rows[el.dataset.admin][0];
      admins[kind(k)][el.checked ? "add" : "delete"](k.slice(k.indexOf(":") + 1));
      adminsChanged = true;
    }));
    main.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => {
      const [k] = rows[b.dataset.del];
      if (isMine(k)) return toast(t("people.toast_self_remove"), true);
      if (isAdmin(k)) { admins[kind(k)].delete(k.slice(k.indexOf(":") + 1)); adminsChanged = true; }
      rows.splice(b.dataset.del, 1);
      draw();
    }));
    main.querySelectorAll("[data-unex]").forEach((b) => (b.onclick = () => { excluded.splice(b.dataset.unex, 1); draw(); }));
    $("#add-who").onchange = (e) => {
      const c = online.find((x) => x.uid === e.target.value);
      if (c) { rows.push([`uid:${c.uid}`, c.identity || c.nickname]); draw(); }
    };
    $("#add-group").onchange = (e) => {
      if (e.target.value) { rows.push([`group:${e.target.value}`, ""]); draw(); main.querySelector(`input[data-i="${rows.length - 1}"]`).focus(); }
    };
    $("#add-ex").onchange = (e) => { if (e.target.value) { excluded.push(e.target.value); draw(); } };
    $("#save").onclick = async () => {
      if (rows.some(([k, v]) => !v.trim() && !isAdmin(k))) return toast(t("people.toast_validation"), true);
      try {
        await api("/api/settings", {
          identities: Object.fromEntries(rows.filter(([, v]) => v.trim())),
          opt_out_uids: excluded,
          ...(adminsChanged && { fields: { "web.admin_uids": [...admins.uid], "web.admin_groups": [...admins.group] } }),
        });
        toast(t("settings.saved_toast"));
        refreshBanner();
      } catch (e) {
        toast(e.message, true);
      }
    };
  };
  draw();
}

async function pagePlugins() {
  const s = await api("/api/settings");
  const enabled = new Set(s.plugins.enabled);
  const config = s.plugins.config;
  const labelPlugin = (n) => ({
    topdestops: t("plugins.label.topdestops"),
    "tsbot.plugins_examples.scoreboard": t("plugins.label.scoreboard"),
  }[n] || n);

  const draw = () => {
    main.innerHTML = `
      <h1>${esc(t("plugins.title"))}</h1>
      <p class="lede">${esc(t("plugins.lede"))}</p>
      <div class="stack">
        ${s.plugins.available.map((n) => `
          <section class="panel stack" data-plugin="${esc(n)}">
            <div class="row">
              <label class="switch">
                <input type="checkbox" data-on ${enabled.has(n) ? "checked" : ""}>
                <span><strong>${esc(labelPlugin(n))}</strong></span>
              </label>
              <button class="btn" data-test style="margin-left:auto">${esc(t("plugins.btn_test"))}</button>
            </div>
            <div data-result></div>
            <div class="kvs">
              ${Object.entries(config[n] || {}).map(([k, v]) => `
                <div class="kv">
                  <input type="text" data-k value="${esc(k)}" aria-label="${esc(t("plugins.setting_name_label"))}">
                  <input type="${/password|token|api_key/.test(k) ? "password" : "text"}" data-v value="${esc(Array.isArray(v) ? v.join(", ") : v)}" aria-label="${esc(t("plugins.setting_value_label"))}">
                </div>`).join("")}
            </div>
            <button class="btn ghost" data-add style="align-self:flex-start">${esc(t("plugins.btn_add_setting"))}</button>
          </section>`).join("")}
      </div>
      <div class="row-end" style="margin-top:20px">
        <button class="btn primary" id="save">${esc(t("plugins.btn_save"))}</button>
      </div>`;

    main.querySelectorAll("[data-plugin]").forEach((sec) => {
      const n = sec.dataset.plugin;
      $("[data-on]", sec).onchange = (e) => (e.target.checked ? enabled.add(n) : enabled.delete(n));
      $("[data-add]", sec).onclick = () => { collect(); (config[n] ||= {})[`setting_${Object.keys(config[n]).length + 1}`] = ""; draw(); };
      $("[data-test]", sec).onclick = async (e) => {
        e.target.disabled = true;
        const r = await api("/api/plugins/test", { name: n }).catch((err) => ({ ok: false, error: err.message }));
        e.target.disabled = false;
        $("[data-result]", sec).innerHTML = r.ok
          ? `<p class="good">${esc(t("plugins.success", { actions: r.actions.join(", ") || t("plugins.none_actions"), context: Object.entries(r.context).map(([k, v]) => `${k} (${v})`).join(", ") }))}</p>`
          : `<p class="bad">${esc(r.error)}</p>`;
      };
    });
    $("#save").onclick = async () => {
      collect();
      try {
        await api("/api/settings", { plugins: { enabled: [...enabled], config } });
        toast(t("plugins.saved_toast"));
        refreshBanner();
      } catch (e) {
        toast(e.message, true);
      }
    };
  };

  const collect = () => main.querySelectorAll("[data-plugin]").forEach((sec) => {
    const n = sec.dataset.plugin;
    const old = config[n] || {};
    config[n] = Object.fromEntries([...sec.querySelectorAll(".kv")].map((row) => {
      const k = $("[data-k]", row).value.trim();
      let v = $("[data-v]", row).value;
      if (Array.isArray(old[k])) v = v.split(",").map((x) => x.trim()).filter(Boolean);
      else if (typeof old[k] === "number" && v.trim() !== "" && !isNaN(v)) v = Number(v);
      return [k, v];
    }).filter(([k]) => k));
  });
  draw();
}

async function pageStats() {
  main.innerHTML = `
    <h1>${esc(t("stats.title"))}</h1>
    <p class="lede">${esc(t("stats.lede"))}</p>
    <div class="row" style="margin-bottom:20px">
      <label for="days">${esc(t("stats.label_period"))}</label>
      <select id="days" style="width:auto">
        <option value="7">${esc(t("stats.period_7"))}</option>
        <option value="30" selected>${esc(t("stats.period_30"))}</option>
        <option value="90">${esc(t("stats.period_90"))}</option>
        <option value="3650">${esc(t("stats.period_all"))}</option>
      </select>
    </div>
    <div id="out" class="stack"><div class="loading-state"><span class="spinner" aria-hidden="true"></span></div></div>`;

  const draw = async () => {
    const s = await api(`/api/stats?days=${$("#days").value}`);
    const speed = (title, d, note) => `
      <section class="panel">
        <h2>${esc(title)}</h2>
        ${d.count ? `<div class="big">${secs(d.median)}</div><p class="muted">${esc(t("stats.speed_desc", { p90: secs(d.p90), count: d.count, failures: note || "" }))}</p>` : `<p class="empty">${esc(t("stats.empty_measure"))}</p>`}
      </section>`;
    const bars = (rows, value, labelOf, fmt) => {
      const max = Math.max(1, ...rows.map(value));
      return `<div class="bars">${rows.map((r) => `
        <div class="bar">
          <span>${esc(labelOf(r))}</span>
          <span class="track"><span class="fill" style="width:${Math.max(2, (100 * value(r)) / max)}%"></span></span>
          <span class="num">${esc(fmt(r))}</span>
        </div>`).join("")}</div>`;
    };
    $("#out").innerHTML = `
      <div class="split" style="grid-template-columns:1fr 1fr">
        ${speed(t("stats.sec_stt"), s.stt_ms)}
        ${speed(t("stats.sec_llm"), s.llm_ms, s.llm_failures ? t("stats.failures_count", { count: s.llm_failures }) : "")}
      </div>
      <section class="panel">
        <h2>${esc(t("stats.sec_speakers"))}</h2>
        ${s.speakers.length ? bars(s.speakers, (r) => r.ms, (r) => r.name, (r) => t("stats.minutes_short", { min: Math.round(r.ms / 60000) })) : `<p class="empty">${esc(t("stats.empty_speakers"))}</p>`}
      </section>
      <section class="panel">
        <h2>${esc(t("stats.sec_daily"))}</h2>
        ${s.per_day.length ? bars(s.per_day.slice(-31), (r) => r.phrases, (r) => new Date(r.day).toLocaleDateString(currentLang === "en" ? "en-US" : "fr-FR", { weekday: "short", day: "numeric", month: "short" }), (r) => String(r.phrases)) : `<p class="empty">${esc(t("stats.empty_daily"))}</p>`}
      </section>
      <section class="panel">
        <h2>${esc(t("stats.sec_actions"))}</h2>
        ${s.actions.length ? bars(s.actions, (r) => r.n, (r) => getStatusLabel(r.status), (r) => String(r.n)) : `<p class="empty">${esc(t("stats.empty_actions"))}</p>`}
      </section>`;
  };
  $("#days").onchange = draw;
  await draw();
}

async function pageLogs() {
  main.innerHTML = `
    <h1>${esc(t("logs.title"))}</h1>
    <p class="lede">${esc(t("logs.lede"))}</p>
    <div class="row" style="margin-bottom:12px">
      <label class="switch">
        <input type="checkbox" id="only">
        <span>${esc(t("logs.switch_errors_only"))}</span>
      </label>
      <button class="btn danger" id="restart" style="margin-left:auto">${esc(t("logs.btn_restart"))}</button>
    </div>
    <div class="logs" id="logs" tabindex="0" role="region" aria-label="${esc(t("logs.aria_label"))}"></div>`;
  let last = 0;
  let all = [];
  const draw = () => {
    const box = $("#logs");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const rows = $("#only").checked ? all.filter((l) => l.level !== "INFO" && l.level !== "DEBUG") : all;
    box.innerHTML = rows.map((l) => `<div class="${esc(l.level)}">${esc(l.text)}</div>`).join("") || `<span class="muted">${esc(t("logs.empty"))}</span>`;
    if (atBottom) box.scrollTop = box.scrollHeight;
  };
  const poll = async () => {
    try {
      const fresh = await api(`/api/logs?after=${last}`);
      if (fresh.length && fresh[0].id < last) all = []; // bot restarted
      all = all.concat(fresh).slice(-500);
      if (fresh.length) last = fresh[fresh.length - 1].id;
      draw();
    } catch {
      last = 0;
      all = [];
    }
  };
  $("#only").onchange = draw;
  $("#restart").onclick = async () => {
    if (!(await confirmBox(t("logs.confirm_restart"), t("services.btn_restart")))) return;
    await api("/api/restart", {});
    toast(t("logs.restart_requested"));
  };
  await poll();
  timer = setInterval(poll, 3000);
}

// ---- Event Listeners & Startup ----

$("#theme-toggle").onclick = () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) {}
};

$("#lang-toggle").onclick = () => {
  const nextLang = currentLang === "fr" ? "en" : "fr";
  setLanguage(nextLang);
};

$("#menu-toggle").onclick = (e) => {
  e.stopPropagation();
  const open = $("#rail").classList.toggle("open");
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
};

// Phone: click outside drawer closes it
$(".stage").addEventListener("click", () => {
  if (!$("#rail").classList.contains("open")) return;
  $("#rail").classList.remove("open");
  $("#menu-toggle").setAttribute("aria-expanded", "false");
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#rail").classList.contains("open")) {
    $("#rail").classList.remove("open");
    $("#menu-toggle").setAttribute("aria-expanded", "false");
    $("#menu-toggle").focus();
  }
});

(async () => {
  await initI18n();
  try {
    const info = await api("/api/info");
    BOT = info.name;
  } catch {}

  // Login token via query link sent in TeamSpeak: ?login=<client>-<code>
  const link = new URLSearchParams(location.search).get("login");
  if (link) {
    const [client, code] = link.split("-");
    history.replaceState(null, "", "/");
    try {
      await api("/api/login/finish", { client_id: Number(client), code });
    } catch (e) {
      toast(`${e.message} ${t("error.code_expired")}`, true);
    }
  }

  try {
    me = await api("/api/me");
  } catch {
    return pageLogin();
  }

  drawRail();
  window.addEventListener("hashchange", show);
  await show();
  refreshBanner();
  setInterval(refreshLamp, 3000);
  refreshLamp();
})();
