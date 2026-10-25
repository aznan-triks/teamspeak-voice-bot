"use strict";

// Everything shown comes from TeamSpeak (names, sentences): always escaped before going into the page.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const $ = (sel, root = document) => root.querySelector(sel);
const main = $("#main");
let me = null;
let BOT = "Bot"; // the bot's name, from its TeamSpeak nickname
let timer = null; // the current page's refresh loop

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.error || `Erreur ${r.status}`), { status: r.status });
  return data;
}

function toast(text, error = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast" + (error ? " error" : "");
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.hidden = true), error ? 6000 : 3000);
}

function confirmBox(text, okLabel = "Confirmer") {
  const d = $("#confirm");
  $("#confirm-text").textContent = text;
  $("#confirm-ok").textContent = okLabel;
  d.showModal();
  return new Promise((resolve) => d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true }));
}

const when = (ms) => new Date(ms).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
const secs = (ms) => (ms == null ? "—" : `${(ms / 1000).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} s`);
const ago = (ms) => {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 60) return "à l'instant";
  if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.round(s / 3600)} h`;
  return `il y a ${Math.round(s / 86400)} j`;
};
// Initials on a colour picked from the name: the same person always gets the same avatar.
function avatar(name, talking = false) {
  const n = String(name || "?");
  let h = 0;
  for (const c of n) h = (h * 31 + c.codePointAt(0)) % 360;
  const initials = n.replace(/\(.*?\)/g, "").trim().split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase() || "?";
  return `<span class="avatar${talking ? " talking" : ""}" style="--avatar:hsl(${h} 42% 40%)" aria-hidden="true">${esc(initials)}</span>`;
}

const STATUS = { proposed: "en attente", replaced: "remplacée", cancelled: "annulée", expired: "sans réponse", failed: "échouée", done: "faite" };

// ---- Frame: menu, lamp, theme ----

const PAGES = {
  conversation: { title: "Conversation", render: pageConversation },
  phrases: { title: "Mes phrases", render: pagePhrases },
  services: { title: "Services", render: pageServices, admin: true },
  actions: { title: "Actions", render: pageActions },
  live: { title: "En direct", render: pageLive, admin: true },
  speak: { title: "Faire parler", render: pageSpeak, admin: true },
  settings: { title: "Réglages", render: pageSettings, admin: true },
  people: { title: "Identités", render: pagePeople, admin: true },
  plugins: { title: "Plugins", render: pagePlugins, admin: true },
  stats: { title: "Statistiques", render: pageStats, admin: true },
  logs: { title: "Journal", render: pageLogs, admin: true },
};

function drawRail() {
  const link = (id) => `<a href="#/${id}" data-page="${id}">${PAGES[id].title}</a>`;
  $("#rail").innerHTML = `
    <div class="brand">${avatar(BOT)}<div><strong>${esc(BOT)}</strong><small>Gestion du bot</small></div></div>
    ${me.admin ? `<div class="group">Le bot</div>${["live", "services", "speak", "logs", "stats"].map(link).join("")}
      <div class="group">Configuration</div>${["settings", "people", "plugins"].map(link).join("")}
      <div class="group">Pour tous</div>` : ""}
    ${["conversation", "phrases", "actions"].map(link).join("")}
    <div class="who">${avatar(me.identity || me.nickname)}<div><strong>${esc(me.identity || me.nickname)}</strong>${me.admin ? "Administrateur" : "Membre"}<br><a href="#" id="logout">Se déconnecter</a></div></div>`;
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
  if (!me?.admin) return setLamp("listening", BOT);
  try {
    const s = await api("/api/state");
    const talking = s.speaking.some((x) => Date.now() - x.ms < 3000);
    if (!s.me) setLamp("down", "Hors ligne");
    else if (s.llm?.degraded) setLamp("down", "Mode dégradé : il écoute mais n'agit plus");
    else if (talking || s.llm?.pending) setLamp("live", talking ? "Quelqu'un parle" : "Attend une confirmation");
    else setLamp("listening", "À l'écoute");
    return s;
  } catch (e) {
    setLamp("down", e.status === 503 ? "Le bot ne répond pas" : "Hors ligne");
  }
}

async function refreshBanner() {
  if (!me?.admin) return;
  const b = $("#banner");
  try {
    const s = await api("/api/settings");
    b.hidden = !s.pending_restart;
    b.innerHTML = `<span>Des réglages attendent d'être appliqués. Appliquer redémarre le bot : il quitte le salon et revient en une minute environ.</span>
      <button class="btn primary" id="apply">Appliquer maintenant</button>`;
    $("#apply").onclick = applyNow;
  } catch { b.hidden = true; }
}

async function applyNow() {
  if (!(await confirmBox("Redémarrer le bot pour appliquer les réglages ? Il quitte le salon une minute environ.", "Redémarrer"))) return;
  await api("/api/restart", {});
  toast("Redémarrage demandé. Le bot revient dans une minute environ.");
  $("#banner").hidden = true;
}

async function show() {
  clearInterval(timer);
  const home = me.admin ? "live" : "conversation";
  const id = location.hash.replace("#/", "") || home;
  const page = PAGES[id] && (!PAGES[id].admin || me.admin) ? id : home;
  document.querySelectorAll("#rail a[data-page]").forEach((a) => (a.dataset.page === page ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  document.title = `${PAGES[page].title} — ${BOT}`;
  $("#rail").classList.remove("open");
  $("#menu-toggle").setAttribute("aria-expanded", "false");
  main.innerHTML = "";
  window.scrollTo(0, 0);
  try { await PAGES[page].render(); } catch (e) { main.innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
  main.focus({ preventScroll: true });
}

// ---- Login ----

async function pageLogin() {
  $("#rail").hidden = true;
  setLamp("listening", BOT);
  main.innerHTML = `<section class="login">
    <div class="row" style="margin-bottom:12px">${avatar(BOT)}<h1 style="margin:0">Connexion</h1></div>
    <p class="lede">Sois connecté au serveur TeamSpeak, choisis ton pseudo : ${esc(BOT)} t'envoie un code. Il apparaît dans une fenêtre au milieu de TeamSpeak (un « poke ») et dans tes messages privés.</p>
    <div class="panel stack" id="login-box"><p class="muted">Recherche des personnes connectées…</p></div></section>`;
  const box = $("#login-box");
  const choose = async () => {
    let people;
    try { people = await api("/api/login/clients"); } catch (e) {
      box.innerHTML = `<p class="bad">${esc(e.message)}</p><p class="muted">Nouvel essai automatique dans quelques secondes.</p>`;
      return;
    }
    if (!people.length) {
      box.innerHTML = `<p>Personne n'est connecté au serveur TeamSpeak. Connecte-toi : ton pseudo apparaîtra ici tout seul.</p>`;
      return;
    }
    const picked = $("#who")?.value;
    box.innerHTML = `<label for="who">Ton pseudo sur TeamSpeak</label>
      <select id="who">${people.map((p) => `<option value="${p.client_id}">${esc(p.nickname)}</option>`).join("")}</select>
      <button class="btn primary" id="send">Recevoir mon code</button>
      <p class="muted">Tu ne te vois pas ? Vérifie que tu es connecté au bon serveur : la liste se met à jour toute seule.</p>`;
    if (picked && people.some((p) => String(p.client_id) === picked)) $("#who").value = picked;
    $("#send").onclick = () => send(Number($("#who").value), $("#who").selectedOptions[0].textContent);
  };
  const send = async (id, name) => {
    clearInterval(timer);
    try {
      const r = await api("/api/login/start", { client_id: id });
      box.innerHTML = `<p><strong>Code envoyé à ${esc(name)}.</strong> Regarde TeamSpeak : une fenêtre s'est ouverte avec le code (valable ${Math.round(r.ttl_s / 60)} minutes). Le plus simple : dans tes messages privés TeamSpeak, clique sur le lien envoyé par ${esc(BOT)}.</p>
        <label for="code">Code à 6 chiffres</label>
        <input id="code" class="code-input" type="text" inputmode="numeric" autocomplete="one-time-code">
        <div class="row"><button class="btn primary" id="go">Se connecter</button><button class="btn ghost" id="again">Renvoyer un code</button><button class="btn ghost" id="other">Changer de pseudo</button></div>`;
      $("#code").focus();
      const go = async () => {
        try { await api("/api/login/finish", { client_id: id, code: $("#code").value }); location.reload(); }
        catch (e) { toast(e.message, true); $("#code").select(); }
      };
      $("#go").onclick = go;
      // Typing or pasting the whole TeamSpeak message both work: the 6 digits are picked out of it.
      $("#code").oninput = (e) => {
        const found = e.target.value.match(/\d{6}/);
        e.target.value = found ? found[0] : e.target.value.replace(/\D/g, "").slice(0, 6);
        if (found) go();
      };
      $("#again").onclick = () => send(id, name);
      $("#other").onclick = () => { choose(); timer = setInterval(choose, 5000); };
    } catch (e) { toast(e.message, true); }
  };
  await choose();
  timer = setInterval(() => { if (!$("#code")) choose(); }, 5000);
}

// ---- Member pages ----

async function pagePhrases() {
  main.innerHTML = `<h1>Mes phrases</h1><p class="lede">Ce que le bot a transcrit de ta voix, du plus récent au plus ancien.</p>
    <div class="row" style="margin-bottom:20px"><input type="search" id="q" placeholder="Chercher un mot" aria-label="Chercher un mot"></div>
    <div id="list" class="subtitles" style="max-height:none"></div><div class="row" style="margin-top:16px"><button class="btn" id="more" hidden>Plus ancien</button></div>`;
  let last = 0;
  const load = async (reset) => {
    if (reset) { last = 0; $("#list").innerHTML = ""; }
    const rows = await api(`/api/my/phrases?q=${encodeURIComponent($("#q").value)}&before_id=${last}`);
    if (reset && !rows.length) $("#list").innerHTML = `<p class="empty">Aucune phrase${$("#q").value ? " avec ce mot" : " pour l'instant : parle dans le salon où se trouve ${esc(BOT)}"}.</p>`;
    $("#list").insertAdjacentHTML("beforeend", rows.map((r) => `<div class="line">${avatar(me.identity || me.nickname)}<div class="who">${esc(me.identity || me.nickname)}<time>${when(r.started_ms)}, durée ${secs(r.duration_ms)}</time></div><p class="said">${esc(r.text)}</p></div>`).join(""));
    if (rows.length) last = rows[rows.length - 1].id;
    $("#more").hidden = rows.length < 50;
  };
  let wait;
  $("#q").oninput = () => { clearTimeout(wait); wait = setTimeout(() => load(true), 300); };
  $("#more").onclick = () => load(false);
  await load(true);
}

async function pageActions() {
  main.innerHTML = `<h1>Actions</h1><p class="lede">Chaque demande faite à la voix : qui l'a demandée, ce qui a été proposé, et ce qui s'est passé.${me.admin ? "" : " La phrase d'origine n'est visible que pour tes propres demandes."}</p><div id="list"></div>`;
  const rows = await api("/api/actions");
  if (!rows.length) return ($("#list").innerHTML = `<p class="empty">Aucune action pour l'instant. Dis « ${esc(BOT)}, … » dans le salon pour en demander une.</p>`);
  $("#list").innerHTML = `<table class="table responsive"><thead><tr><th>Quand</th><th>Qui</th><th>Demande</th><th>Résultat</th></tr></thead><tbody>
    ${rows.map((a) => `<tr><td>${when(a.created_ms)}</td><td>${esc(a.identity || "?")}</td>
      <td><strong>${esc(a.name.endsWith(".add_points") ? "Ajout de points" : a.name)}</strong> ${esc(Object.entries(a.args).map(([k, v]) => `${k} : ${v}`).join(", "))}
        ${a.sentence ? `<div class="quote">« ${esc(a.sentence)} »</div>` : ""}</td>
      <td><span class="chip ${esc(a.status)}">${esc(STATUS[a.status] || a.status)}</span>${a.result ? `<div class="muted">${esc(a.result)}</div>` : ""}</td></tr>`).join("")}
    </tbody></table>`;
}

// ---- Admin pages ----

// The conversation as this person could hear it: only sentences said while they were in the bot's channel,
// headset on. Returns a function that draws the new sentences; call it on every refresh.
function conversationFeed(list, status) {
  let last = 0;
  let first = true;
  return async () => {
    const [rows, presence] = await Promise.all([api(`/api/conversation?after_id=${last}`), api("/api/presence")]);
    status.className = "hearing " + (presence.hearing ? "on" : "off");
    status.innerHTML = presence.hearing
      ? "Tu entends le salon : ce qui s'y dit s'affiche ici."
      : `${esc(presence.reason)} Tu ne vois que ce que tu pouvais entendre : rien de nouveau ne s'affiche tant que tu n'écoutes pas le salon.`;
    if (first && !rows.length) list.innerHTML = `<p class="empty">Rien pour l'instant. Rejoins le salon de ${esc(BOT)}, casque actif, et la conversation s'affichera ici.</p>`;
    if (!rows.length) return;
    if (first || last === 0) list.innerHTML = "";
    first = false;
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
    list.insertAdjacentHTML("beforeend", rows.map((r) => `<div class="line${r.name && r.name.startsWith(BOT) ? " bot" : ""}">${avatar(r.name)}
      <div class="who">${esc(r.name)}<time>${when(r.started_ms)}</time></div><p class="said">${esc(r.text)}</p></div>`).join(""));
    last = rows[rows.length - 1].id;
    if (atBottom) list.scrollTop = list.scrollHeight;
  };
}

async function pageConversation() {
  main.innerHTML = `<h1>Conversation</h1><p class="lede">Ce que ${esc(BOT)} a entendu dans son salon pendant que tu y étais, casque actif. Ce que tu ne pouvais pas entendre n'apparaît jamais.</p>
    <p id="hearing" class="hearing"></p><section class="panel"><div id="lines" class="subtitles"></div></section>`;
  const draw = conversationFeed($("#lines"), $("#hearing"));
  await draw();
  timer = setInterval(draw, 2000);
}

const SERVICE_LABEL = { llm: "Modèle de langage (réflexion des réponses)", tsbot: "Bot (écoute, transcription, voix)", web: "Ce site" };
const SERVICE_STATE = { active: "en marche", activating: "démarre", deactivating: "s'arrête", inactive: "arrêté", failed: "en panne" };

async function pageServices() {
  main.innerHTML = `<h1>Services</h1><p class="lede">Les trois programmes qui font tourner ${esc(BOT)} sur le NUC. Ils redémarrent tout seuls au démarrage de la machine.</p>
    <div id="services" class="stack"></div>`;
  const open = new Set();
  const draw = async () => {
    let list;
    try { list = await api("/api/services"); } catch (e) { $("#services").innerHTML = `<p class="bad">${esc(e.message)}</p>`; return; }
    if (!list.length) { $("#services").innerHTML = `<p class="empty">Aucun service déclaré ([web] services dans la configuration).</p>`; return; }
    const logs = {};
    await Promise.all([...open].map(async (n) => { logs[n] = await api(`/api/services/logs?name=${encodeURIComponent(n)}&lines=150`).catch(() => []); }));
    $("#services").innerHTML = list.map((s) => `<section class="panel stack" data-service="${esc(s.name)}">
      <div class="row"><span class="lamp-dot ${s.active === "active" ? "on" : s.active === "failed" ? "down" : ""}"></span>
        <div><strong>${esc(SERVICE_LABEL[s.name] || s.name)}</strong><div class="muted">${esc(SERVICE_STATE[s.active] || s.active)}${s.active === "active" && s.since ? `, depuis le ${when(Number(s.since.replace("@", "")) * 1000)}` : ""}${Number(s.restarts) ? `, ${esc(s.restarts)} redémarrage(s) automatique(s)` : ""}</div></div>
        <div class="row" style="margin-left:auto">
          ${s.active === "active" ? "" : `<button class="btn primary" data-act="start">Démarrer</button>`}
          <button class="btn" data-act="restart">Redémarrer</button>
          ${s.active === "active" && s.name !== "web" ? `<button class="btn danger" data-act="stop">Arrêter</button>` : ""}
          <button class="btn ghost" data-logs>${open.has(s.name) ? "Masquer le journal" : "Voir le journal"}</button></div></div>
      ${open.has(s.name) ? `<div class="logs" style="height:40vh">${(logs[s.name] || []).map((l) => `<div class="${/error|failed|traceback/i.test(l) ? "ERROR" : /warn/i.test(l) ? "WARNING" : ""}">${esc(l)}</div>`).join("") || "Journal vide."}</div>` : ""}
    </section>`).join("");
    document.querySelectorAll(".logs").forEach((b) => (b.scrollTop = b.scrollHeight));
    document.querySelectorAll("[data-service]").forEach((sec) => {
      const name = sec.dataset.service;
      sec.querySelector("[data-logs]").onclick = () => { open.has(name) ? open.delete(name) : open.add(name); draw(); };
      sec.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
        const act = b.dataset.act;
        const what = { start: "Démarrer", stop: "Arrêter", restart: "Redémarrer" }[act];
        const warn = name === "web" ? " Le site se recharge dans quelques secondes." : name === "tsbot" ? " Le bot quitte le salon pendant environ une minute." : "";
        if (act !== "start" && !(await confirmBox(`${what} « ${SERVICE_LABEL[name] || name} » ?${warn}`, what))) return;
        try { await api("/api/services", { name, action: act }); toast(`${what} : demandé.`); }
        catch (e) { toast(e.message, true); }
        setTimeout(draw, name === "web" ? 4000 : 1500);
      }));
    });
  };
  await draw();
  timer = setInterval(draw, 5000);
}

async function pageLive() {
  main.innerHTML = `<h1>En direct</h1><p class="lede">L'état du bot, qui est dans son salon et qui parle. La conversation n'apparaît que si tu peux l'entendre toi-même.</p>
    <dl class="facts" id="facts"></dl>
    <div class="split"><section class="panel"><h2>Conversation</h2><p id="hearing" class="hearing"></p><div id="lines" class="subtitles"></div></section>
    <section class="panel"><h2>Dans le salon</h2><ul class="people" id="people"></ul></section></div>`;
  const feed = conversationFeed($("#lines"), $("#hearing"));
  const draw = async () => {
    feed().catch(() => {});
    const s = await refreshLamp();
    if (!s) return ($("#facts").innerHTML = `<div><dt>Bot</dt><dd class="bad">ne répond pas</dd></div>`);
    const fact = (k, v, cls = "") => `<div><dt>${k}</dt><dd class="${cls}">${v}</dd></div>`;
    $("#facts").innerHTML = fact("TeamSpeak", s.me ? "connecté" : "déconnecté", s.me ? "good" : "bad")
      + fact("Réponses (LLM)", s.llm == null ? "désactivées" : s.llm.degraded ? "mode dégradé" : "prêtes", s.llm?.degraded ? "bad" : "good")
      + fact("Voix", esc(s.voice || "pas encore utilisée"))
      + fact("Plugins", s.plugins.length ? esc(s.plugins.join(", ")) : "aucun")
      + fact("Démarré", ago(s.started_ms));
    const channel = s.me?.channel_id;
    const here = s.clients.filter((c) => c.channel_id === channel && c.client_id !== s.me?.client_id);
    const talking = new Set(s.speaking.filter((x) => Date.now() - x.ms < 3000).map((x) => x.client_id));
    $("#people").innerHTML = here.length ? here.map((c) => `<li class="${talking.has(c.client_id) ? "talking" : ""}">${avatar(c.nickname)}${esc(c.nickname)}<span class="id">${c.identity ? esc(c.identity) : "inconnu"}</span></li>`).join("")
      : `<li class="muted">Personne d'autre dans le salon.</li>`;
  };
  await draw();
  timer = setInterval(draw, 2000);
}

async function pageSpeak() {
  main.innerHTML = `<h1>Faire parler le bot</h1><p class="lede">Écoute d'abord la phrase ici, puis fais-la dire dans le salon. Chaque phrase n'est générée qu'une fois, puis réutilisée.</p>
    <div class="panel stack"><label for="text">Phrase</label><textarea id="text" maxlength="400" placeholder="Bonsoir tout le monde !"></textarea>
    <div class="row"><button class="btn" id="preview">Écouter ici</button><button class="btn primary" id="say">Dire dans le salon</button></div>
    <audio id="player" controls hidden></audio></div>`;
  const text = () => $("#text").value.trim();
  $("#preview").onclick = async () => {
    if (!text()) return toast("Écris d'abord une phrase.", true);
    $("#preview").disabled = true;
    const r = await fetch(`/api/voice?text=${encodeURIComponent(text())}`);
    $("#preview").disabled = false;
    if (!r.ok) return toast((await r.json().catch(() => ({}))).error || "La voix n'a pas pu être générée.", true);
    const p = $("#player");
    p.src = URL.createObjectURL(await r.blob());
    p.hidden = false;
    p.play();
  };
  $("#say").onclick = async () => {
    if (!text()) return toast("Écris d'abord une phrase.", true);
    $("#say").disabled = true;
    try { const r = await api("/api/say", { text: text() }); toast(`Dit dans le salon (${secs(r.seconds * 1000)}).`); }
    catch (e) { toast(e.message, true); }
    $("#say").disabled = false;
  };
}

const SECTIONS = { teamspeak: "TeamSpeak", stt: "Transcription", llm: "Réponses et actions", voice: "Voix", "voice.phrases": "Phrases du bot", web: "Accès au site" };

async function pageSettings() {
  const s = await api("/api/settings");
  const groups = {};
  s.fields.forEach((f) => (groups[f.section] ||= []).push(f));
  const input = (f) => {
    const id = `f-${f.section}-${f.key}`.replace(/\./g, "-");
    const v = f.value;
    let control;
    if (f.type === "bool") return `<div class="field" data-key="${f.section}.${f.key}" data-type="bool"><label class="switch"><input type="checkbox" id="${id}" ${v ? "checked" : ""}>${esc(f.label)}</label>${f.help ? `<p class="help">${esc(f.help)}</p>` : ""}</div>`;
    if (f.type === "list") control = `<textarea id="${id}">${esc((v || []).join("\n"))}</textarea>`;
    else if (f.type === "text") control = `<textarea id="${id}" rows="5">${esc(v ?? "")}</textarea>`;
    else if (f.type.startsWith("choice:")) control = `<select id="${id}">${f.type.slice(7).split(",").map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    else if (f.type === "secret") control = `<input id="${id}" type="password" autocomplete="new-password" value="${esc(v ?? "")}" placeholder="Aucun">`;
    else control = `<input id="${id}" type="${f.type === "int" ? "number" : "text"}" ${f.type === "int" ? 'min="0"' : ""} value="${esc(v ?? "")}" placeholder="${esc(f.default ?? "")}">`;
    return `<div class="field" data-key="${f.section}.${f.key}" data-type="${f.type}"><label for="${id}">${esc(f.label)}</label>${f.help ? `<p class="help">${esc(f.help)}</p>` : ""}${control}</div>`;
  };
  main.innerHTML = `<h1>Réglages</h1><p class="lede">Les changements sont enregistrés tout de suite, puis appliqués quand tu redémarres le bot avec « Appliquer ».</p>
    <form id="form" class="stack">${Object.entries(groups).map(([sec, fields]) => `<fieldset class="panel"><legend>${esc(SECTIONS[sec] || sec)}</legend>${fields.map(input).join("")}</fieldset>`).join("")}
    <div class="row-end" style="position:sticky;bottom:12px"><button class="btn primary" id="save" type="submit" disabled>Enregistrer</button></div></form>`;
  const read = (el) => {
    const c = el.querySelector("input,textarea,select");
    const t = el.dataset.type;
    if (t === "bool") return c.checked;
    if (t === "int") return c.value === "" ? null : Number(c.value);
    if (t === "list") return c.value.split("\n").map((x) => x.trim()).filter(Boolean);
    return c.value;
  };
  const initial = new Map([...main.querySelectorAll(".field")].map((el) => [el.dataset.key, JSON.stringify(read(el))]));
  const changed = () => [...main.querySelectorAll(".field")].filter((el) => JSON.stringify(read(el)) !== initial.get(el.dataset.key));
  $("#form").oninput = () => {
    main.querySelectorAll(".field").forEach((el) => el.classList.toggle("changed", changed().includes(el)));
    $("#save").disabled = !changed().length;
    $("#save").textContent = changed().length ? `Enregistrer ${changed().length} changement${changed().length > 1 ? "s" : ""}` : "Enregistrer";
  };
  $("#form").onsubmit = async (e) => {
    e.preventDefault();
    const fields = Object.fromEntries(changed().map((el) => [el.dataset.key, read(el)]));
    try { await api("/api/settings", { fields }); toast("Enregistré. Clique sur « Appliquer » pour que le bot les prenne en compte."); await refreshBanner(); await pageSettings(); }
    catch (err) { toast(err.message, true); }
  };
}

async function pagePeople() {
  const [s, state] = await Promise.all([api("/api/settings"), api("/api/state").catch(() => ({ clients: [] }))]);
  const online = state.clients.filter((c) => c.uid && c.client_id !== state.me?.client_id);
  const nameOf = (uid) => online.find((c) => c.uid === uid)?.nickname;
  let rows = Object.entries(s.identities);
  let excluded = [...s.opt_out_uids];
  const draw = () => {
    main.innerHTML = `<h1>Identités et exclusions</h1>
      <p class="lede">Une identité relie une personne TeamSpeak (par son identifiant, ou tout un grade) à son nom de joueur. Seules les personnes connues peuvent demander une action à la voix.</p>
      <section class="panel stack"><h2>Identités</h2>
        <table class="table responsive"><thead><tr><th>Personne TeamSpeak</th><th>Nom de joueur</th><th></th></tr></thead><tbody>
        ${rows.map(([k, v], i) => `<tr><td><input type="text" data-i="${i}" data-f="k" value="${esc(k)}" aria-label="Personne TeamSpeak">${k.startsWith("uid:") && nameOf(k.slice(4)) ? `<div class="muted">connecté : ${esc(nameOf(k.slice(4)))}</div>` : ""}</td>
          <td><input type="text" data-i="${i}" data-f="v" value="${esc(v)}" aria-label="Nom de joueur"></td><td><button class="btn ghost danger" data-del="${i}">Retirer</button></td></tr>`).join("") || `<tr><td colspan="3" class="muted">Aucune identité.</td></tr>`}
        </tbody></table>
        <div class="row"><select id="add-who" aria-label="Personne connectée"><option value="">Ajouter une personne connectée…</option>${online.map((c) => `<option value="${esc(c.uid)}">${esc(c.nickname)}</option>`).join("")}</select>
          <button class="btn" id="add-group">Ajouter un grade</button></div></section>
      <section class="panel stack"><h2>Ne pas transcrire</h2><p class="muted">Ces personnes ne sont jamais transcrites : leur voix n'atteint pas la transcription.</p>
        <ul class="people">${excluded.map((uid, i) => `<li>${esc(nameOf(uid) || uid)}<button class="btn ghost danger" style="margin-left:auto" data-unex="${i}">Retirer</button></li>`).join("") || `<li class="muted">Personne.</li>`}</ul>
        <select id="add-ex" aria-label="Exclure une personne connectée"><option value="">Exclure une personne connectée…</option>${online.filter((c) => !excluded.includes(c.uid)).map((c) => `<option value="${esc(c.uid)}">${esc(c.nickname)}</option>`).join("")}</select></section>
      <div class="row-end"><button class="btn primary" id="save">Enregistrer</button></div>`;
    main.querySelectorAll("input[data-i]").forEach((el) => (el.oninput = () => { const r = rows[el.dataset.i]; r[el.dataset.f === "k" ? 0 : 1] = el.value; }));
    main.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => { rows.splice(b.dataset.del, 1); draw(); }));
    main.querySelectorAll("[data-unex]").forEach((b) => (b.onclick = () => { excluded.splice(b.dataset.unex, 1); draw(); }));
    $("#add-who").onchange = (e) => { const c = online.find((x) => x.uid === e.target.value); if (c) { rows.push([`uid:${c.uid}`, c.identity || c.nickname]); draw(); } };
    $("#add-group").onclick = () => { rows.push(["group:", ""]); draw(); };
    $("#add-ex").onchange = (e) => { if (e.target.value) { excluded.push(e.target.value); draw(); } };
    $("#save").onclick = async () => {
      try { await api("/api/settings", { identities: Object.fromEntries(rows), opt_out_uids: excluded }); toast("Enregistré. Clique sur « Appliquer » pour que le bot les prenne en compte."); refreshBanner(); }
      catch (e) { toast(e.message, true); }
    };
  };
  draw();
}

async function pagePlugins() {
  const s = await api("/api/settings");
  const enabled = new Set(s.plugins.enabled);
  const config = s.plugins.config;
  const label = (n) => ({ topdestops: "Top des Tops", "tsbot.plugins_examples.scoreboard": "Tableau des scores (exemple)" })[n] || n;
  const draw = () => {
    main.innerHTML = `<h1>Plugins</h1><p class="lede">Les plugins donnent au bot ses actions et ses connaissances (scores, classements…). Active-les, règle-les, puis teste la connexion.</p>
      <div class="stack">${s.plugins.available.map((n) => `<section class="panel stack" data-plugin="${esc(n)}">
        <div class="row"><label class="switch"><input type="checkbox" data-on ${enabled.has(n) ? "checked" : ""}>${esc(label(n))}</label>
          <button class="btn" data-test style="margin-left:auto">Tester</button></div>
        <div data-result></div>
        <div class="kvs">${Object.entries(config[n] || {}).map(([k, v]) => `<div class="kv"><input type="text" data-k value="${esc(k)}" aria-label="Nom du réglage">
          <input type="${/password|token|api_key/.test(k) ? "password" : "text"}" data-v value="${esc(Array.isArray(v) ? v.join(", ") : v)}" aria-label="Valeur"></div>`).join("")}</div>
        <button class="btn ghost" data-add>Ajouter un réglage</button></section>`).join("")}</div>
      <div class="row-end" style="margin-top:20px"><button class="btn primary" id="save">Enregistrer</button></div>`;
    main.querySelectorAll("[data-plugin]").forEach((sec) => {
      const n = sec.dataset.plugin;
      $("[data-on]", sec).onchange = (e) => (e.target.checked ? enabled.add(n) : enabled.delete(n));
      $("[data-add]", sec).onclick = () => { collect(); (config[n] ||= {})[`reglage${Object.keys(config[n]).length + 1}`] = ""; draw(); };
      $("[data-test]", sec).onclick = async (e) => {
        e.target.disabled = true;
        const r = await api("/api/plugins/test", { name: n }).catch((err) => ({ ok: false, error: err.message }));
        e.target.disabled = false;
        $("[data-result]", sec).innerHTML = r.ok ? `<p class="good">Connexion OK. Actions : ${esc(r.actions.join(", ") || "aucune")}. Données : ${esc(Object.entries(r.context).map(([k, v]) => `${k} (${v})`).join(", "))}.</p>`
          : `<p class="bad">${esc(r.error)}</p>`;
      };
    });
    $("#save").onclick = async () => {
      collect();
      try { await api("/api/settings", { plugins: { enabled: [...enabled], config } }); toast("Enregistré. Le test utilise les réglages enregistrés ; « Appliquer » les donne au bot."); refreshBanner(); }
      catch (e) { toast(e.message, true); }
    };
  };
  // Number-looking values go back as numbers, "a, b" lists as lists when they were lists.
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
  main.innerHTML = `<h1>Statistiques</h1><p class="lede">Rapidité du bot et activité du salon.</p>
    <div class="row" style="margin-bottom:20px"><label for="days">Période</label><select id="days" style="width:auto"><option value="7">7 jours</option><option value="30" selected>30 jours</option><option value="90">90 jours</option><option value="3650">Tout</option></select></div>
    <div id="out" class="stack"></div>`;
  const draw = async () => {
    const s = await api(`/api/stats?days=${$("#days").value}`);
    const speed = (title, d, note) => `<section class="panel"><h2>${title}</h2>${d.count ? `<div class="big">${secs(d.median)}</div><p class="muted">Durée habituelle (la moitié des fois, c'est plus rapide). 9 fois sur 10 en moins de ${secs(d.p90)}, sur ${d.count} mesures.${note || ""}</p>` : `<p class="empty">Pas encore de mesure sur cette période.</p>`}</section>`;
    const bars = (rows, value, labelOf, fmt) => {
      const max = Math.max(1, ...rows.map(value));
      return `<div class="bars">${rows.map((r) => `<div class="bar"><span>${esc(labelOf(r))}</span><span class="track"><span class="fill" style="width:${(100 * value(r)) / max}%"></span></span><span class="num">${fmt(r)}</span></div>`).join("")}</div>`;
    };
    $("#out").innerHTML = `<div class="split" style="grid-template-columns:1fr 1fr">${speed("Transcription d'une phrase", s.stt_ms)}${speed("Réflexion du LLM", s.llm_ms, s.llm_failures ? ` ${s.llm_failures} échec${s.llm_failures > 1 ? "s" : ""}.` : "")}</div>
      <section class="panel"><h2>Qui parle le plus</h2>${s.speakers.length ? bars(s.speakers, (r) => r.ms, (r) => r.name, (r) => `${Math.round(r.ms / 60000)} min`) : `<p class="empty">Personne n'a parlé sur cette période.</p>`}</section>
      <section class="panel"><h2>Phrases par jour</h2>${s.per_day.length ? bars(s.per_day.slice(-31), (r) => r.phrases, (r) => new Date(r.day).toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short" }), (r) => r.phrases) : `<p class="empty">Aucune phrase sur cette période.</p>`}</section>
      <section class="panel"><h2>Actions demandées</h2>${s.actions.length ? bars(s.actions, (r) => r.n, (r) => STATUS[r.status] || r.status, (r) => r.n) : `<p class="empty">Aucune action sur cette période.</p>`}</section>`;
  };
  $("#days").onchange = draw;
  await draw();
}

async function pageLogs() {
  main.innerHTML = `<h1>Journal</h1><p class="lede">Ce que le bot note pendant qu'il tourne : connexions, phrases, réponses, erreurs. Les 500 dernières lignes depuis son démarrage.</p>
    <div class="row" style="margin-bottom:12px"><label class="switch"><input type="checkbox" id="only">Seulement les problèmes</label>
      <button class="btn danger" id="restart" style="margin-left:auto">Redémarrer le bot</button></div>
    <div class="logs" id="logs" tabindex="0" aria-label="Journal du bot"></div>`;
  let last = 0;
  let all = [];
  const draw = () => {
    const box = $("#logs");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const rows = $("#only").checked ? all.filter((l) => l.level !== "INFO" && l.level !== "DEBUG") : all;
    box.innerHTML = rows.map((l) => `<div class="${esc(l.level)}">${esc(l.text)}</div>`).join("") || `<span class="muted">Rien pour l'instant.</span>`;
    if (atBottom) box.scrollTop = box.scrollHeight;
  };
  const poll = async () => {
    try {
      const fresh = await api(`/api/logs?after=${last}`);
      if (fresh.length && fresh[0].id < last) all = []; // the bot restarted: numbering starts again
      all = all.concat(fresh).slice(-500);
      if (fresh.length) last = fresh[fresh.length - 1].id;
      draw();
    } catch { last = 0; all = []; }
  };
  $("#only").onchange = draw;
  $("#restart").onclick = async () => {
    if (!(await confirmBox("Redémarrer le bot ? Il quitte le salon une minute environ.", "Redémarrer"))) return;
    await api("/api/restart", {});
    toast("Redémarrage demandé.");
  };
  await poll();
  timer = setInterval(poll, 3000);
}

// ---- Start ----

$("#theme-toggle").onclick = () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) {}
};
$("#menu-toggle").onclick = (e) => {
  e.stopPropagation();
  const open = $("#rail").classList.toggle("open");
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
};
// Phone: a tap outside the open menu closes it.
$(".stage").addEventListener("click", () => {
  if (!$("#rail").classList.contains("open")) return;
  $("#rail").classList.remove("open");
  $("#menu-toggle").setAttribute("aria-expanded", "false");
});

(async () => {
  try { BOT = (await api("/api/info")).name; } catch {}
  // Login link sent in TeamSpeak: ?login=<client>-<code> logs in with one click.
  const link = new URLSearchParams(location.search).get("login");
  if (link) {
    const [client, code] = link.split("-");
    history.replaceState(null, "", "/");
    try { await api("/api/login/finish", { client_id: Number(client), code }); }
    catch (e) { toast(`${e.message} Redemande un code.`, true); }
  }
  try { me = await api("/api/me"); } catch { return pageLogin(); }
  drawRail();
  window.addEventListener("hashchange", show);
  await show();
  refreshBanner();
  if (me.admin) setInterval(refreshLamp, 5000); else refreshLamp();
})();
