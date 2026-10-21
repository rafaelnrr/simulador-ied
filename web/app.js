/* Simulador de IEDs IEC 61850 — interface web (seção 10).
 * Sem build: JavaScript puro, uma página, roteamento por hash e eventos em tempo real via WebSocket.
 * Nomes de objetos IEC 61850 ficam em inglês, como no CID (RN-UI-05). */
"use strict";

// ---------------------------------------------------------------------------- utilidades de DOM
const $ = (s, r = document) => r.querySelector(s);
const PROPS = new Set(["value", "checked", "disabled", "selected", "indeterminate", "open", "multiple"]);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false && !PROPS.has(k)) continue;
      if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (PROPS.has(k)) el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
/* Substitui o conteúdo ignorando null/false (o replaceChildren nativo escreveria "null"). */
function put(el, ...kids) { el.replaceChildren(); return add(el, kids); }
function sel(options, current, attrs = {}) {
  return h("select", attrs, options.map(o => {
    const [v, label, dis] = Array.isArray(o) ? o : [o, o];
    return h("option", { value: v, selected: String(v) === String(current), disabled: !!dis }, label);
  }));
}
const field = (label, input, cls) => h("label", { class: "f" + (cls ? " " + cls : "") }, label, input);
const chk = (label, checked, attrs = {}) => {
  const i = h("input", { type: "checkbox", checked: !!checked, ...attrs });
  return { el: h("label", { class: "chk" }, i, label), input: i };
};
// Colunas redimensionáveis com o mouse: arrastar a borda direita do cabeçalho; duplo clique na borda volta ao
// automático. As larguras ficam guardadas no navegador por tabela (chave) e por título de coluna.
function resizable(table, key) {
  const ths = [...table.tHead.rows[0].cells];
  const label = th => th.textContent.trim();
  let saved = {};
  try { saved = JSON.parse(store("cols." + key) || "{}") || {}; } catch { saved = {}; }
  const fix = widths => {
    let total = 0;
    ths.forEach((th, i) => { const w = Math.max(40, Math.round(widths[i])); th.style.width = w + "px"; total += w; });
    table.style.width = total + "px";
    table.classList.add("cols-fixed");
  };
  const reset = () => {
    ths.forEach(th => { th.style.width = ""; });
    table.style.width = "";
    table.classList.remove("cols-fixed");
    saved = {};
    store("cols." + key, "{}");
  };
  if (ths.some(th => saved[label(th)])) {
    requestAnimationFrame(() => fix(ths.map(th => saved[label(th)] || th.offsetWidth || 120)));
  }
  ths.forEach((th, i) => {
    const grip = h("span", { class: "col-grip", title: "arraste para ajustar a largura; duplo clique volta ao automático" });
    grip.addEventListener("pointerdown", ev => {
      ev.preventDefault();
      ev.stopPropagation();
      const widths = ths.map(x => x.getBoundingClientRect().width);
      fix(widths);
      const x0 = ev.clientX, w0 = widths[i];
      grip.setPointerCapture(ev.pointerId);
      document.body.classList.add("col-resizing");
      const move = e => { widths[i] = Math.max(40, w0 + e.clientX - x0); fix(widths); };
      const up = () => {
        grip.removeEventListener("pointermove", move);
        document.body.classList.remove("col-resizing");
        saved = Object.fromEntries(ths.map((x, j) => [label(x), Math.round(widths[j])]));
        store("cols." + key, JSON.stringify(saved));
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up, { once: true });
      grip.addEventListener("pointercancel", up, { once: true });
    });
    grip.addEventListener("dblclick", ev => { ev.stopPropagation(); reset(); });
    th.append(grip);
  });
  return table;
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function throttle(fn, ms) {
  let last = 0, timer = null;
  return () => {
    const now = Date.now();
    if (now - last >= ms) { last = now; fn(); }
    else if (!timer) timer = setTimeout(() => { timer = null; last = Date.now(); fn(); }, ms - (now - last));
  };
}
function store(k, v) {
  try { if (v === undefined) return localStorage.getItem("simied." + k); localStorage.setItem("simied." + k, v); }
  catch { return null; }
}
function parseVal(s) {
  const t = String(s).trim();
  if (t === "") return null;
  try { return JSON.parse(t); } catch { return t; }
}
const fmtVal = v => v == null ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v);

// ---------------------------------------------------------------------------- estado global
const S = {
  token: store("token") || "",
  tz: store("tz") || "local",
  info: null,
  skew: 0,           // relógio da máquina do simulador menos o do navegador (ms)
  ieds: [],
  profiles: [],
  page: null,
};

// ---------------------------------------------------------------------------- API
class ApiError extends Error {
  constructor(status, data) {
    let msg = data && (data.erro || (data.detail && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))));
    super(msg || `HTTP ${status}`);
    this.status = status;
    this.data = data || {};
  }
}

async function api(method, path, body, opt = {}) {
  const headers = {};
  if (S.token) headers.Authorization = "Bearer " + S.token;
  let b;
  if (body instanceof FormData) b = body;
  else if (opt.text != null) { b = opt.text; headers["Content-Type"] = "text/plain; charset=utf-8"; }
  else if (body !== undefined) { b = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
  const r = await fetch("/api/v1" + path, { method, headers, body: b });
  let data = null;
  try { data = await r.json(); } catch { /* sem corpo */ }
  if (r.status === 401 && path !== "/login") { showLogin(); throw new ApiError(401, data); }
  if (!r.ok) throw new ApiError(r.status, data);
  return data;
}

/* Envia uma alteração tratando as confirmações do backend:
 * 409 "confirmar" (derruba associações), 422 "fora_da_faixa" (RN-ANA-03) e "erros_importacao" (RN-CID-09).
 * Devolve a resposta, ou null se o usuário desistiu ou houve erro (já mostrado em toast). */
async function send(method, path, body = {}, opt = {}) {
  try {
    return await api(method, path, body, opt);
  } catch (e) {
    if (!(e instanceof ApiError)) { toast(String(e), "bad"); return null; }
    const code = e.data.codigo;
    if (e.status === 409 && code === "confirmar") {
      if (!await confirmDlg(e.message, "Confirmar", true)) return null;
      if (method === "DELETE") return send(method, path + (path.includes("?") ? "&" : "?") + "confirmar=true", body, opt);
      return send(method, path, { ...body, confirmar: true }, opt);
    }
    if (code === "fora_da_faixa") {
      if (!await confirmDlg(e.message + "\n\nGravar mesmo assim (exceção confirmada)?", "Gravar")) return null;
      return send(method, path, { ...body, confirmar_fora_faixa: true }, opt);
    }
    if (code === "erros_importacao") {
      if (!await confirmDlg(e.message, "Subir com o que é válido")) return null;
      return send(method, path, { ...body, aceitar_erros: true }, opt);
    }
    if (e.status !== 401) toast(e.message + (e.data.caminho ? `\n${e.data.caminho}` : ""), "bad");
    return null;
  }
}
const dl = path => "/api/v1" + path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(S.token);

// ---------------------------------------------------------------------------- avisos e diálogos
function toast(msg, kind = "info", ms = 5000) {
  const t = h("div", { class: "toast " + kind, style: { whiteSpace: "pre-line" } }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "bad" ? ms * 2 : ms);
}

function modal(title, body, buttons = [], opts = {}) {
  const d = h("dialog", { class: opts.cls || "" });
  const close = v => { d.close(); d.remove(); res(v); };
  let res;
  const p = new Promise(r => { res = r; });
  const foot = h("div", { class: "df" }, buttons.map(b => h("button", {
    class: b.cls || "", onclick: async ev => {
      if (!b.click) return close(b.value);
      const btn = ev.currentTarget;
      btn.disabled = true;  // evita envio duplicado
      try {
        const r = await b.click(close);
        if (r !== false) close(r);
      } finally { btn.disabled = false; }
    },
  }, b.label)));
  add(d, [h("div", { class: "dh" }, title, h("span", { class: "spacer" }),
    h("button", { class: "ghost sm", onclick: () => close(undefined), title: "Fechar" }, "✕")),
  h("div", { class: "db" }, body), buttons.length ? foot : null]);
  d.addEventListener("cancel", e => { e.preventDefault(); close(undefined); });
  document.body.append(d);
  d.showModal();
  p.close = close;
  p.dialog = d;
  return p;
}

function confirmDlg(text, ok = "Confirmar", danger = false) {
  return modal("Confirmação", h("div", { style: { whiteSpace: "pre-line" } }, text), [
    { label: "Cancelar", value: false },
    { label: ok, cls: danger ? "danger solid" : "primary", value: true },
  ]).then(v => !!v);
}

function promptDlg(title, fields) {
  // fields: [{k, label, value, type}]
  const inputs = {};
  const body = h("div", { class: "form" }, fields.map(f => {
    inputs[f.k] = h("input", { type: f.type || "text", value: f.value ?? "", placeholder: f.ph || "" });
    return field(f.label, inputs[f.k], "wide");
  }));
  return modal(title, body, [
    { label: "Cancelar", value: null },
    { label: "OK", cls: "primary", click: () => Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value])) },
  ]);
}

// ---------------------------------------------------------------------------- horários (RN-UI-04, RN-TMP-06)
const pad = (n, w = 2) => String(n).padStart(w, "0");
function fmtMs(ms, utc) {
  const d = new Date(ms);
  const g = utc
    ? [d.getUTCDate(), d.getUTCMonth() + 1, d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCMilliseconds()]
    : [d.getDate(), d.getMonth() + 1, d.getHours(), d.getMinutes(), d.getSeconds(), d.getMilliseconds()];
  return `${pad(g[0])}/${pad(g[1])} ${pad(g[2])}:${pad(g[3])}:${pad(g[4])}.${pad(g[5], 3)}`;
}
const toMs = x => typeof x === "number" ? x * 1000 : Date.parse(x);
function tsEl(x) {
  if (!x) return h("span", { class: "muted" }, "—");
  const ms = toMs(x);
  if (isNaN(ms)) return h("span", {}, String(x));
  const l = fmtMs(ms, false), u = fmtMs(ms, true);
  const [a, b, la, lb] = S.tz === "utc" ? [u, l, "UTC", "local"] : [l, u, "local", "UTC"];
  return h("span", { class: "nowrap mono", title: `local ${l} · UTC ${u}` },
    a, h("span", { class: "small muted" }, " " + la), h("span", { class: "ts2" }, `${lb} ${b}`));
}
const nowSrv = () => Date.now() + S.skew;

// ---------------------------------------------------------------------------- tempo real
let ws = null;
const wsHandlers = new Set();
function wsConnect() {
  if (ws) { try { ws.close(); } catch { /* ignore */ } }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/api/v1/ws?token=${encodeURIComponent(S.token)}`);
  ws = sock;
  sock.onmessage = e => {
    let batch;
    try { batch = JSON.parse(e.data); } catch { return; }
    for (const m of batch) {
      globalEvent(m);
      if (S.page && S.page.onEvent) { try { S.page.onEvent(m); } catch (err) { console.error(err); } }
    }
  };
  sock.onclose = ev => { if (ws === sock && ev.code !== 4401) setTimeout(wsConnect, 2000); };
}
const refreshIeds = throttle(async () => {
  try { S.ieds = (await api("GET", "/ieds")).ieds; updateSimbar(); } catch { /* ignore */ }
}, 1000);
function globalEvent(m) {
  if (["ied.state", "ied.created", "ied.deleted", "session.opened", "session.closed", "panic"].includes(m.evento)) refreshIeds();
  if (m.evento === "panic") toast("PARADA RÁPIDA executada: IEDs parados e pontos restaurados.", "bad");
}

// faixa MODO SIMULAÇÃO (RN-SEG-05)
function updateSimbar() {
  const run = S.ieds.filter(i => i.estado === "rodando");
  const assoc = run.reduce((a, i) => a + (i.associacoes || 0), 0);
  const err = S.ieds.filter(i => i.estado === "erro");
  const bar = $("#simbar");
  bar.classList.toggle("live", run.length > 0);
  let msg = run.length
    ? `${run.length} IED(s) rodando · ${assoc} associação(ões) MMS ativa(s). Tudo o que os SCADAs recebem destes IPs é SIMULADO.`
    : "Nenhum IED rodando. Os dados deste simulador nunca vêm de relés reais.";
  if (err.length) msg += ` · ${err.length} IED(s) em erro`;
  $("#simmsg").textContent = msg;
}

// ---------------------------------------------------------------------------- login (RN-UI-06, RN-SEG-03)
function showLogin() {
  if (S.page && S.page.destroy) S.page.destroy();
  S.page = null;
  if (S.token) S.expirou = true;  // havia login e o servidor recusou: sessão expirada ou encerrada
  const expirou = !!S.expirou;
  let saiu = false;
  try { saiu = sessionStorage.getItem("simied.saiu") === "1"; sessionStorage.removeItem("simied.saiu"); } catch { /* sem sessionStorage */ }
  S.token = "";
  store("token", "");
  document.body.classList.add("auth");
  document.body.classList.remove("side-open");
  const lembrado = store("login.usuario") || "";
  const u = h("input", { autocomplete: "username", value: lembrado, placeholder: "seu usuário", required: true, spellcheck: false });
  const p = h("input", { type: "password", autocomplete: "current-password", placeholder: "sua senha", required: true });
  const ver = h("button", { type: "button", class: "ghost lg-eye", title: "Mostrar a senha", "aria-label": "Mostrar a senha" });
  ver.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/><path class="risco" d="M4 4l16 16"/></svg>';
  ver.addEventListener("click", () => {
    const mostra = p.type === "password";
    p.type = mostra ? "text" : "password";
    ver.title = mostra ? "Ocultar a senha" : "Mostrar a senha";
    ver.classList.toggle("on", mostra);
    p.focus();
  });
  const caps = h("div", { class: "lg-caps", hidden: true }, "Caps Lock ligado");
  const capsCheck = e => { if (e.getModifierState) caps.hidden = !e.getModifierState("CapsLock"); };
  p.addEventListener("keydown", capsCheck);
  p.addEventListener("keyup", capsCheck);
  const lembrar = chk("Lembrar o usuário neste navegador", !!lembrado);
  const err = h("div", { class: "alert bad", hidden: true, role: "alert" });
  const btn = h("button", { class: "primary lg-btn", type: "submit" }, "Entrar");
  const form = h("form", { class: "lg-form", onsubmit: async e => {
    e.preventDefault();
    if (!u.value.trim() || !p.value) { err.textContent = "Informe usuário e senha."; err.hidden = false; return; }
    btn.disabled = true;
    btn.textContent = "Entrando…";
    err.hidden = true;
    try {
      const r = await api("POST", "/login", { usuario: u.value.trim(), senha: p.value });
      store("login.usuario", lembrar.input.checked ? u.value.trim() : "");
      S.token = r.token;
      S.expirou = false;
      store("token", r.token);
      document.body.classList.remove("auth");
      wsConnect();
      boot2();
    } catch (ex) {
      err.textContent = ex.status === 401 ? "Usuário ou senha incorretos." : ex.message;
      err.hidden = false;
      form.classList.remove("shake");
      void form.offsetWidth;
      form.classList.add("shake");
      p.select();
      btn.disabled = false;
      btn.textContent = "Entrar";
    }
  } },
    h("div", { class: "lg-head" }, h("h1", {}, "Entrar"), h("p", { class: "muted" }, "Use seu usuário do simulador ou do diretório (LDAP), quando configurado.")),
    expirou ? h("div", { class: "alert warn" }, "Sua sessão expirou ou foi encerrada. Entre de novo.") : null,
    saiu && !expirou ? h("div", { class: "alert info" }, "Você saiu do sistema.") : null,
    field("Usuário", u),
    h("label", { class: "f" }, "Senha", h("div", { class: "lg-pw" }, p, ver)), caps,
    lembrar.el, err, btn,
    h("p", { class: "muted small lg-note" }, "A interface está acessível pela rede, por isso o login é obrigatório. ",
      "Os papéis definem o que cada um pode fazer: leitura consulta, operação opera os IEDs, administrador também configura."));
  const feat = (ic, t, d) => h("li", {}, h("span", { class: "lg-ic" }, ic), h("div", {}, h("b", {}, t), h("span", {}, d)));
  put($("#app"), h("div", { class: "lg" },
    h("section", { class: "lg-brand" },
      h("div", { class: "lg-logo" }, h("span", { class: "lg-mark" }, h("i"), h("i"), h("i")), h("div", {}, h("b", {}, "SimIED"), h("span", {}, "Simulador de IEDs IEC 61850"))),
      h("ul", { class: "lg-feat" },
        feat("▶", "Simulação", "IEDs a partir do CID real, com MMS, reports, falhas e cenários para testar o SAGE."),
        feat("◉", "CID", "Visualizador por assunto e editor com modelo, tipos e XML, validação e comparação."),
        feat("⚙", "SAGE", "Gerador e visualizador de OPMSK, com sugestão a partir do CID.")),
      h("div", { class: "lg-warn" }, "Os dados enviados por este simulador são simulados. Use só IEDs e pontos reservados para teste."),
      h("div", { class: "lg-ver" }, S.info ? `versão ${S.info.versao} · ${location.host}` : location.host)),
    h("section", { class: "lg-side" }, h("button", { type: "button", class: "ghost icon-btn js-tema lg-theme", onclick: cycleTema }), form)));
  applyTheme();
  (lembrado ? p : u).focus();
}

// ---------------------------------------------------------------------------- roteamento
const ROUTES = {
  "": () => pageIeds(),
  rede: () => pageRede(),
  perfis: () => pagePerfis(),
  cenarios: () => pageCenarios(),
  ensaios: () => pageEnsaios(),
  cid: (sid, no) => pageCid(sid, no ? decodeURIComponent(no) : null),
  cidview: (sid, tab) => pageCidView(sid, tab),
  opmsk: () => pageOpmsk(),
  config: () => pageConfig(),
  ied: (id, tab) => pageIed(+id, tab || "painel"),
};
function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  const key = parts[0] || "";
  const fn = ROUTES[key] || ROUTES[""];
  if (S.page && S.page.destroy) S.page.destroy();
  for (const a of document.querySelectorAll("#nav a")) {
    a.classList.toggle("on", a.dataset.r === (key === "" || key === "ied" ? "ieds" : key));
  }
  const pg = fn(...parts.slice(1));
  S.page = pg;
  put($("#app"), pg.el);
}

// ---------------------------------------------------------------------------- componentes comuns
function stateBadge(st, err) {
  const cls = { rodando: "ok", parado: "neutral", erro: "bad" }[st] || "neutral";
  return h("span", { class: "badge " + cls, title: err || "" }, st || "?");
}
function qualBadge(q) {
  if (!q) return h("span", { class: "muted" }, "—");
  const cls = { good: "ok", invalid: "bad", questionable: "warn", reserved: "warn" }[q.validity] || "neutral";
  const extra = [...(q.detailQual || []), q.source === "substituted" ? "substituted" : null,
    q.test ? "test" : null, q.operatorBlocked ? "opBlk" : null].filter(Boolean);
  return h("span", { class: "badge " + cls, title: `q = ${q.hex || ""}` }, q.validity + (extra.length ? " · " + extra.join(", ") : ""));
}

async function loadProfiles() {
  try { S.profiles = (await api("GET", "/profiles")).perfis; } catch { /* ignore */ }
  return S.profiles;
}

/* Seleção de interface e IP (RN-NET-12 a RN-NET-15). */
async function ipPicker(curIp, opts = {}) {
  const wrap = h("div", { class: "stack" });
  let chosen = curIp ? { ip: curIp, iface: opts.iface || "" } : null;
  const onPick = opts.onPick || (() => {});
  async function load() {
    let d;
    try { d = await api("GET", "/net/interfaces"); } catch { return; }
    const rows = [];
    for (const it of d.interfaces) {
      const dis = !it.selecionavel;
      const usedBy = it.ieds.map(i => i.nome).join(", ");
      const ips = it.ips.length ? it.ips : [{ ip: "", prefixo: "", mascara: "" }];
      ips.forEach((a, k) => {
        const radio = h("input", {
          type: "radio", name: "ippick" + (opts.uid || ""), disabled: dis || !a.ip || (it.admin && !it.loopback && a.ip === S.info?.admin?.split(":")[0]),
          checked: !!a.ip && a.ip === chosen?.ip,
          onchange: () => { chosen = { ip: a.ip, iface: it.nome }; onPick(chosen); },
        });
        rows.push(h("tr", { class: dis ? "muted" : "" },
          h("td", {}, radio),
          k === 0 ? h("td", { rowspan: ips.length }, h("b", {}, it.nome), it.loopback ? h("span", { class: "badge info", style: { marginLeft: "6px" } }, "teste local") : null) : null,
          k === 0 ? h("td", { rowspan: ips.length, class: "mono" }, it.mac || "—") : null,
          h("td", { class: "mono" }, a.ip ? `${a.ip}/${a.prefixo}` : "sem IPv4", a.mascara ? h("span", { class: "ts2" }, a.mascara) : null),
          k === 0 ? h("td", { rowspan: ips.length }, h("span", { class: "dot " + (it.ativa ? "ok" : "bad") }), it.estado) : null,
          k === 0 ? h("td", { rowspan: ips.length, class: "nowrap" }, it.velocidade_mbps ? `${it.velocidade_mbps} Mb/s` : "—") : null,
          k === 0 ? h("td", { rowspan: ips.length, class: "small" }, usedBy || h("span", { class: "muted" }, "—"), it.admin ? h("div", { class: "badge warn" }, "administração web") : null) : null,
        ));
      });
    }
    put(wrap, 
      h("div", { class: "row between" }, h("span", { class: "muted small" },
        "Interfaces inativas ou sem IPv4 aparecem, mas não podem ser escolhidas" + (d.teste_local ? "; loopback liberado (teste local)." : ".")),
        h("button", { class: "sm", onclick: load }, "↻ Atualizar")),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
        ["", "Interface", "MAC", "IP / máscara", "Estado", "Velocidade", "IEDs que usam"].map(x => h("th", {}, x)))),
      h("tbody", {}, rows))));
  }
  await load();
  return { el: wrap, get: () => chosen, reload: load };
}

function hostLine(ip, iedName, nome) {
  const text = `${ip || "<IP-do-simulador>"}\t${iedName}\t# host_mms_<ENM> – simulador (${nome})`;
  const code = h("code", {}, text);
  return {
    el: h("div", { class: "stack" },
      h("div", { class: "hostline" }, code, h("button", {
        class: "sm", onclick: () => navigator.clipboard?.writeText(code.textContent).then(() => toast("Linha copiada", "ok")),
      }, "Copiar")),
      h("p", { class: "muted small" }, "Linha para o /etc/hosts (host_mms_<ENM>) de cada SCADA. Registre o valor anterior e reverta ao fim do teste (RN-NET-10). ",
        "Um SCADA que continuar apontando para o relé real segue recebendo os dados reais (RN-NET-11).")),
    set: (ip2) => { code.textContent = `${ip2 || "<IP-do-simulador>"}\t${iedName}\t# host_mms_<ENM> – simulador (${nome})`; },
  };
}

function opmskWidget() {
  const inp = h("input", { value: store("opmsk") || S.settings?.sage?.opmsk_padrao || "10C830", class: "mono", style: { width: "120px" } });
  const out = h("div");
  const go = async () => {
    store("opmsk", inp.value);
    try {
      const d = await api("GET", "/opmsk?valor=" + encodeURIComponent(inp.value.trim()));
      put(out, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Bit"), h("th", {}, "Ligado"), h("th", {}, "Efeito no SAGE (Anexo 17, CNF.CONFIG)"))),
        h("tbody", {}, d.bits.map(b => h("tr", { class: b.ligado ? "" : "muted" },
          h("td", { class: "mono" }, b.bit, h("div", { class: "desc" }, b.opt)),
          h("td", {}, b.ligado ? h("span", { class: "badge ok" }, "sim") : "não"),
          h("td", {}, b.efeito || "—", b.detalhe ? h("div", { class: "desc" }, b.detalhe) : null)))))));
    } catch (e) { put(out, h("div", { class: "alert bad" }, e.message)); }
  };
  inp.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  go();
  return h("div", { class: "stack" }, h("div", { class: "row" }, field("OPMSK (hexadecimal, CNF do SAGE)", inp), h("button", { onclick: go, style: { alignSelf: "flex-end" } }, "Decodificar"),
    h("a", { href: "#/opmsk", style: { alignSelf: "flex-end" } }, "gerador e comparação…")), out);
}

// ---------------------------------------------------------------------------- lista de IEDs
function pageIeds() {
  const tbody = h("tbody");
  const el = h("div", { class: "stack" },
    h("div", { class: "row between" }, h("h1", {}, "IEDs simulados"),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: () => wizardNovo() }, "+ Importar CID / novo IED"))),
    h("p", { class: "muted small" }, "Cada IED escuta num IP próprio desta máquina, na porta 102, e se comporta como o relé descrito no seu CID. ",
      "Iniciar ou parar um IED não afeta os demais."),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {},
      ["Nome", "CID", "Perfil", "IP:porta", "Estado", "Assoc.", "Reports", "Falhas", ""].map(x => h("th", {}, x)))), tbody)));

  async function load() {
    let d;
    try { d = await api("GET", "/ieds"); } catch { return; }
    S.ieds = d.ieds;
    updateSimbar();
    if (!d.ieds.length) {
      put(tbody, h("tr", {}, h("td", { colspan: 9 }, h("div", { class: "empty" },
        "Nenhum IED. Importe o CID de um relé para começar."))));
      return;
    }
    put(tbody, ...d.ieds.map(i => h("tr", {},
      h("td", {}, h("a", { href: `#/ied/${i.id}/painel` }, h("b", {}, i.nome)), i.nome_ied && i.nome_ied !== i.nome ? h("div", { class: "desc mono" }, i.nome_ied) : null),
      h("td", { class: "small" }, i.cid, i.edicao ? h("div", { class: "desc" }, i.edicao) : null),
      h("td", { class: "small mono" }, i.perfil),
      h("td", { class: "mono nowrap" }, i.ip ? `${i.ip}:${i.porta}` : h("span", { class: "badge warn" }, "sem IP")),
      h("td", {}, stateBadge(i.estado, i.erro), i.erro ? h("div", { class: "desc", style: { color: "var(--bad)" } }, i.erro) : null),
      h("td", { class: "right mono" }, `${i.associacoes ?? 0}${i.max_associacoes ? "/" + i.max_associacoes : ""}`),
      h("td", { class: "right mono" }, i.reports_enviados ?? 0),
      h("td", {}, (i.falhas_ativas || []).length ? h("span", { class: "badge bad" }, `${i.falhas_ativas.length} ativa(s)`) : h("span", { class: "muted" }, "—")),
      h("td", { class: "nowrap right" },
        i.estado === "rodando"
          ? h("button", { class: "sm danger", onclick: () => stopIed(i).then(load) }, "■ Parar")
          : h("button", { class: "sm", onclick: () => startIed(i).then(load), disabled: !i.ied_scl }, "▶ Iniciar"), " ",
        h("button", { class: "sm", onclick: () => duplicateIed(i).then(load), disabled: !i.ied_scl }, "Duplicar"), " ",
        h("button", { class: "sm danger", onclick: () => removeIed(i).then(load) }, "Remover")),
    )));
  }
  load();
  const reload = throttle(load, 1000);
  return { el, onEvent: m => { if (/^(ied\.|session\.|report\.sent|panic)/.test(m.evento)) reload(); } };
}

async function startIed(i) {
  if (!i.ip) { toast("Escolha a interface e o IP do IED no painel antes de iniciar (RN-NET-13).", "bad"); location.hash = `#/ied/${i.id}/painel`; return; }
  const ok = await confirmDlg(
    `Ligar o IED “${i.nome}” em ${i.ip}:${i.porta}?\n\n` +
    "Os SCADAs que apontarem para este IP passam a receber dados SIMULADOS. Eventos injetados aparecem para o operador e entram no .alr e no .sde reais.\n\n" +
    "Use só IED e pontos reservados para teste, em janela combinada com a COT (RN-SEG-05).", "Ligar IED", true);
  if (!ok) return;
  const r = await send("POST", `/ieds/${i.id}/start`);
  if (r) toast(`${i.nome} rodando em ${i.ip}:${i.porta}`, "ok");
}
async function stopIed(i) {
  const n = i.associacoes || 0;
  if (!await confirmDlg(`Parar o IED “${i.nome}”?${n ? `\n\n${n} associação(ões) MMS serão fechadas (RN-UI-03).` : ""}`, "Parar", true)) return;
  await send("POST", `/ieds/${i.id}/stop`);
}
async function removeIed(i) {
  if (!await confirmDlg(`Remover o IED “${i.nome}”? A configuração, o estado dos pontos e o CID guardado são apagados.`, "Remover", true)) return;
  if (await send("DELETE", `/ieds/${i.id}`)) toast("IED removido", "ok");
  if (location.hash.startsWith(`#/ied/${i.id}`)) location.hash = "#/";
}
async function duplicateIed(i) {
  const nome = h("input", { value: `${i.nome} (cópia)` });
  const nomeIed = h("input", { value: i.nome_ied || "", class: "mono" });
  const porta = h("input", { type: "number", value: i.porta || 102, class: "num" });
  const picker = await ipPicker("", { uid: "dup" });
  const r = await modal("Duplicar IED", h("div", { class: "stack" },
    h("p", { class: "muted small" }, "Um mesmo CID pode ser instanciado mais de uma vez com IPs e nomes distintos, para testar vários relés iguais."),
    h("div", { class: "form" }, field("Nome", nome), field("Nome do IED (MMS)", nomeIed), field("Porta", porta)),
    h("h3", {}, "Interface e IP"), picker.el), [
    { label: "Cancelar", value: null },
    {
      label: "Duplicar", cls: "primary", click: async () => {
        const c = picker.get();
        return await send("POST", `/ieds/${i.id}/duplicate`, { nome: nome.value, nome_ied: nomeIed.value, ip: c?.ip || "", interface: c?.iface || "" });
      },
    }]);
  if (r && +porta.value !== i.porta) await send("PATCH", `/ieds/${r.id}`, { porta: +porta.value });
  if (r) toast("IED duplicado", "ok");
}

/* Sobe um CID novo para o mesmo IED (RN-CID-08): mantém o estado dos pontos, as lógicas e a configuração dos
   comandos, remapeando as referências que mudaram de endereço. Devolve true se reimportou. */
async function reimportCid(id, iedScl, file) {
  const info = await uploadCid(file);
  if (!info) return false;
  let target = iedScl;
  if (!info.ieds.some(i => i.nome === target)) {
    if (info.ieds.length === 1) target = info.ieds[0].nome;
    else {
      const r = await promptDlg("Qual IED do arquivo?", [{ k: "n", label: info.ieds.map(i => i.nome).join(", "), value: info.ieds[0].nome }]);
      if (!r) return false;
      target = r.n;
    }
  }
  const prev = await send("POST", `/ieds/${id}/reimport`, { token: info.token, ied_scl: target, simular: true });
  if (!prev) return false;
  const mapa = await reimportPlan(prev);
  if (!mapa) return false;
  const r = await send("POST", `/ieds/${id}/reimport`, { token: info.token, ied_scl: target, mapa });
  if (!r) return false;
  await modal("Reimportação concluída", h("div", { class: "stack" },
    h("p", {}, `${r.mantidos} ponto(s) mantidos com o estado anterior.`),
    h("p", {}, `${r.logicas} lógica(s) preservada(s)` + (r.remapeados ? `; ${r.remapeados} referência(s) remapeada(s).` : ".")),
    r.orfas.length ? h("div", { class: "alert warn" }, `${r.orfas.length} referência(s) de lógica/comando sem ponto no CID novo: `,
      h("a", { href: `#/ied/${id}/logicas` }, "revisar lógicas →")) : null,
    h("details", { open: r.removidos.length > 0 }, h("summary", {}, `${r.removidos.length} ponto(s) sumiram`), h("ul", { class: "list mono" }, r.removidos.map(x => h("li", {}, x)))),
    h("details", { open: r.novos.length > 0 }, h("summary", {}, `${r.novos.length} ponto(s) novos`), h("ul", { class: "list mono" }, r.novos.map(x => h("li", {}, x)))),
    r.erros.length ? h("div", { class: "alert bad" }, h("ul", { class: "list" }, r.erros.map(x => h("li", {}, x)))) : null), [{ label: "OK", cls: "primary" }]);
  return true;
}

/* Prévia da reimportação: referências das lógicas e dos comandos que somem no CID novo, cada uma com um seletor
   já na sugestão (mesmo DO em LN da mesma classe, ou mesma descrição). Devolve o mapa {antigo: novo} ou null. */
async function reimportPlan(prev) {
  const refs = prev.referencias;
  const resumo = h("p", {}, `${prev.mantidos} ponto(s) mantidos, ${prev.removidos.length} somem, ${prev.novos.length} novos. ` +
    `${prev.logicas} lógica(s) e a configuração dos comandos continuam no IED.`);
  const erros = prev.erros.length ? h("div", { class: "alert bad" }, h("ul", { class: "list" }, prev.erros.map(x => h("li", {}, x)))) : null;
  if (!refs.length) {
    const ok = await modal("Reimportar CID", h("div", { class: "stack" }, resumo,
      h("p", { class: "muted" }, "Todas as referências das lógicas e dos comandos existem no CID novo."), erros),
      [{ label: "Cancelar", value: false }, { label: "Reimportar", cls: "primary", value: true }]);
    return ok ? {} : null;
  }
  const sels = refs.map(r => sel([["", "— deixar sem ponto —"], ...r.candidatos.map(c => [c, c])], r.sugestao || "", { class: "mono" }));
  const rows = refs.map((r, i) => h("tr", {},
    h("td", { class: "mono small" }, r.ref, h("div", { class: "muted" }, [r.tipo === "ponto" ? "ponto" : "objeto de controle", r.cdc, r.desc].filter(Boolean).join(" · "))),
    h("td", { class: "small" }, r.usos.map(u => h("div", {}, u.logica ? `${u.logica} (bloco ${u.bloco})` : u.bloco))),
    h("td", {}, sels[i], r.sugestao ? h("div", { class: "muted small" }, "sugerido") : null)));
  const ok = await modal("Reimportar CID: referências das lógicas", h("div", { class: "stack" }, resumo,
    h("div", { class: "alert warn" }, `${refs.length} referência(s) usada(s) em lógicas ou na configuração de comandos não existem no CID novo. ` +
      "Escolha o equivalente para manter a lógica funcionando; as sugestões já vêm marcadas."),
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["No CID atual", "Usado em", "No CID novo"].map(x => h("th", {}, x)))),
      h("tbody", {}, rows))), erros), [{ label: "Cancelar", value: false }, { label: "Reimportar", cls: "primary", value: true }], { cls: "wide" });
  if (!ok) return null;
  const mapa = {};
  refs.forEach((r, i) => { if (sels[i].value) mapa[r.ref] = sels[i].value; });
  return mapa;
}

function pickFile(accept) {
  return new Promise(res => {
    const i = h("input", { type: "file", accept });
    i.addEventListener("change", () => res(i.files[0] || null));
    i.addEventListener("cancel", () => res(null));
    i.click();
  });
}

async function uploadCid(file) {
  const fd = new FormData();
  fd.append("arquivo", file, file.name);
  try { return await api("POST", "/cid", fd); } catch (e) { toast("Importação recusada: " + e.message, "bad"); return null; }
}

function cidSummary(info) {
  return h("div", { class: "stack" },
    h("div", {}, h("b", {}, "SCL: "), info.edicao_texto),
    info.erros.length ? h("div", { class: "alert bad" }, h("b", {}, "Erros do arquivo"), h("ul", { class: "list" }, info.erros.map(e => h("li", {}, e)))) : null,
    info.ieds.map(i => h("div", { class: "card" },
      h("div", { class: "row between" }, h("b", { class: "mono" }, i.nome), h("span", { class: "muted small" }, [i.fabricante, i.tipo].filter(Boolean).join(" · "))),
      h("dl", { class: "kv", style: { marginTop: "8px" } },
        h("dt", {}, "IP no CID"), h("dd", { class: "mono" }, i.ip_cid || "—", h("span", { class: "muted small" }, "  (relé real — nunca usado pelo simulador, RN-NET-03)")),
        h("dt", {}, "LDs"), h("dd", { class: "mono small" }, i.ld.join(", ")),
        h("dt", {}, "Pontos"), h("dd", {}, `${i.pontos} (${i.digitais} digitais, ${i.analogicos} analógicos, ${i.somente_leitura} somente leitura)`),
        h("dt", {}, "DataSets / RCBs"), h("dd", {}, `${i.datasets} / ${i.rcbs}`)),
      i.erros.length ? h("details", {}, h("summary", { style: { color: "var(--bad)" } }, `${i.erros.length} erro(s) de importação`), h("ul", { class: "list" }, i.erros.map(e => h("li", {}, e)))) : null,
      i.avisos.length ? h("details", {}, h("summary", {}, `${i.avisos.length} aviso(s)`), h("ul", { class: "list" }, i.avisos.map(e => h("li", {}, e)))) : null)));
}

async function wizardNovo(pronto) {
  await loadProfiles();
  const file = h("input", { type: "file", accept: ".cid,.icd,.scd,.iid,.xml,.sed" });
  const area = h("div", { class: "stack" }, h("p", { class: "muted small" },
    "CID, ICD ou SCD (SCL Edição 1 ou 2). O CID é a fonte do modelo de dados: o servidor expõe só o que está nele."), file);
  let info = null, form = null;
  const dlg = modal("Novo IED a partir do CID", area, [
    { label: "Cancelar", value: null },
    {
      label: "Criar IED", cls: "primary", click: async () => {
        if (!info || !form) { toast("Escolha o arquivo CID", "bad"); return false; }
        const v = form();
        const r = await send("POST", "/ieds", v);
        if (!r) return false;
        toast(`IED ${r.nome} criado`, "ok");
        location.hash = `#/ied/${r.id}/painel`;
        return r;
      },
    }]);
  file.addEventListener("change", async () => {
    if (!file.files[0]) return;
    useInfo(await uploadCid(file.files[0]));
  });
  if (pronto) useInfo(pronto);
  async function useInfo(i) {
    info = i;
    if (!info) return;
    const names = info.ieds.map(i => i.nome);
    const iedSel = sel(names, names[0]);
    const nome = h("input", { value: names[0] });
    const nomeIed = h("input", { value: names[0], class: "mono" });
    const sugerido = () => {
      const fixo = S.settings?.novo_ied?.perfil;
      if (fixo && fixo !== "auto" && S.profiles.some(p => p.id === fixo)) return fixo;
      const ii = info.ieds.find(i => i.nome === iedSel.value) || {};
      if (/ingeteam/i.test(ii.fabricante || "")) return "ingeteam-ef-i15";
      return info.edicao >= 2 ? "generico-ed2" : "generico-ed1";
    };
    const perfil = sel(S.profiles.map(p => [p.id, `${p.id} — ${p.nome || ""}`]), sugerido());
    const porta = h("input", { type: "number", value: S.settings?.novo_ied?.porta ?? 102, class: "num" });
    const maxa = h("input", { type: "number", value: S.settings?.novo_ied?.max_associacoes ?? 3, min: 1, max: 64, class: "num" });
    const hl = hostLine("", names[0], names[0]);
    const picker = await ipPicker("", { uid: "new", onPick: c => hl.set(c.ip) });
    iedSel.addEventListener("change", () => { nome.value = iedSel.value; nomeIed.value = iedSel.value; perfil.value = sugerido(); });
    const hasErr = info.erros.length || info.ieds.some(i => i.erros.length);
    const acc = chk("Subir o IED só com o que é válido, ignorando os erros listados (RN-CID-09)", false);
    put(area, 
      cidSummary(info),
      h("h3", {}, "Instância"),
      h("div", { class: "form" },
        names.length > 1 ? field("IED do arquivo (SCD: RN-CID-02)", iedSel) : null,
        field("Nome na interface", nome), field("Nome do IED (MMS / /etc/hosts)", nomeIed),
        field("Perfil (seção 9)", perfil), field("Porta (102 = padrão do SAGE)", porta), field("Máx. associações (RN-NET-06)", maxa)),
      h("h3", {}, "Interface e IP de escuta (RN-NET-01, RN-NET-13)"), picker.el, hl.el,
      hasErr ? acc.el : null);
    form = () => {
      const c = picker.get();
      return {
        token: info.token, ied_scl: iedSel.value, nome: nome.value, nome_ied: nomeIed.value, perfil: perfil.value,
        ip: c?.ip || "", interface: c?.iface || "", porta: +porta.value, max_associacoes: +maxa.value,
        aceitar_erros: acc.input.checked,
      };
    };
  }
  return dlg;
}

// ---------------------------------------------------------------------------- página do IED
const TABS = [
  ["painel", "Painel"], ["digitais", "Sinais digitais"], ["analogicos", "Sinais analógicos"], ["vinculos", "Vínculos"],
  ["rcbs", "RCBs e DataSets"], ["comandos", "Comandos"], ["logicas", "Lógicas"], ["subida", "Subida de pontos"], ["reports", "Reports e histórico"], ["falhas", "Falhas"],
];

function pageIed(id, tab) {
  const head = h("div", { class: "stack" });
  const tabs = h("div", { class: "tabs" });
  const body = h("div");
  const el = h("div", {}, head, tabs, body);
  let sub = null;
  let cur = null;

  function renderHead(d) {
    cur = Object.assign(cur || {}, d);
    const faults = cur.falhas_ativas || [];
    put(head, 
      h("div", { class: "row between" },
        h("div", { class: "row" }, h("a", { href: "#/" }, "IEDs"), h("span", { class: "muted" }, "›"), h("h1", {}, cur.nome),
          stateBadge(cur.estado, cur.erro), h("span", { class: "mono muted" }, cur.ip ? `${cur.ip}:${cur.porta}` : "sem IP"),
          h("span", { class: "muted small" }, `${cur.associacoes || 0} assoc. · ${cur.reports_enviados || 0} reports`)),
        h("div", { class: "row" },
          h("a", { class: "btn", href: `#/cid/ied-${id}`, title: "Ver e editar o CID deste IED (modelo, tipos e XML)" }, "Editor de CID"),
          cur.estado === "rodando"
            ? h("button", { class: "danger", onclick: () => stopIed(cur) }, "■ Parar")
            : h("button", { class: "primary", onclick: () => startIed(cur) }, "▶ Iniciar"))),
      cur.erro ? h("div", { class: "alert bad" }, cur.erro) : null,
      cur.rascunho ? h("div", { class: "alert warn row between" },
        h("span", {}, h("b", {}, "CID editado e não aplicado: "), `${cur.rascunho.alteracoes} alteração(ões) no Editor de CID ainda não estão no simulador.`),
        h("a", { class: "btn sm", href: `#/cid/ied-${id}` }, "Abrir o editor e aplicar")) : null,
      faults.length ? h("div", { class: "alert bad" }, h("b", {}, "Falhas injetadas ativas: "),
        faults.map(f => `${f.regra} ${f.titulo}`).join(" · "), " — ", h("a", { href: `#/ied/${id}/falhas` }, "ver")) : null);
    put(tabs, ...TABS.map(([k, l]) => h("a", { href: `#/ied/${id}/${k}`, class: k === tab ? "on" : "" }, l,
      k === "falhas" && faults.length ? h("span", { class: "count" }, faults.length) : null)));
  }

  (async () => {
    try { cur = await api("GET", `/ieds/${id}`); } catch (e) { put(body, h("div", { class: "alert bad" }, e.message)); return; }
    try { cur.rascunho = (await api("GET", "/cid-editor")).rascunhos.find(x => x.origem === "ied" && x.ied === id) || null; } catch { cur.rascunho = null; }
    renderHead(cur);
    const make = {
      painel: subPainel, digitais: d => subPoints(d, "dig"), analogicos: d => subPoints(d, "ana"), vinculos: subVinculos,
      rcbs: subRcbs, comandos: subComandos, logicas: subLogicas, subida: subSubida, reports: subReports, falhas: subFalhas,
    }[tab] || subPainel;
    sub = make(cur, renderHead);
    put(body, sub.el);
  })();

  return {
    el,
    onEvent: m => {
      if (m.ied !== id) return;
      if (m.evento === "ied.state") renderHead(m.dados);
      if (m.evento === "ied.deleted") { location.hash = "#/"; return; }
      if (sub && sub.onEvent) sub.onEvent(m);
    },
    destroy: () => { if (sub && sub.destroy) sub.destroy(); },
  };
}

// ---------------------------------------------------------------------------- painel
function subPainel(d, renderHead) {
  const id = d.id;
  const el = h("div", { class: "grid c2" });
  let clockTimer = null;
  let clk = d.relogio;

  async function render() {
    d = await api("GET", `/ieds/${id}`);
    clk = d.relogio;
    renderHead(d);
    await loadProfiles();
    // resumo
    const resumo = h("div", { class: "card" }, h("h2", {}, "Resumo"),
      h("dl", { class: "kv" },
        h("dt", {}, "CID"), h("dd", {}, d.cid, h("span", { class: "muted small" }, ` · ${d.edicao}`)),
        h("dt", {}, "IED no CID"), h("dd", { class: "mono" }, d.ied_scl, d.nome_ied !== d.ied_scl ? ` → servido como ${d.nome_ied}` : ""),
        h("dt", {}, "Fabricante / tipo"), h("dd", {}, [d.fabricante, d.tipo].filter(Boolean).join(" · ") || "—"),
        h("dt", {}, "IP do relé real (CID)"), h("dd", { class: "mono" }, d.ip_cid || "—", h("span", { class: "muted small" }, " nunca usado (RN-NET-03)")),
        h("dt", {}, "LDs"), h("dd", { class: "mono small" }, d.ld.join(", ")),
        h("dt", {}, "Perfil"), h("dd", { class: "mono" }, d.perfil),
        h("dt", {}, "Pontos"), h("dd", {}, d.pontos),
        h("dt", {}, "Associações"), h("dd", {}, `${d.associacoes} de ${d.max_associacoes}`),
        h("dt", {}, "Reports / mudanças"), h("dd", {}, `${d.reports_enviados} / ${d.mudancas}`),
        h("dt", {}, "Iniciado em"), h("dd", {}, d.estado === "rodando" ? tsEl(d.iniciado_em) : "—")),
      d.erros_importacao.length ? h("details", { style: { marginTop: "10px" } }, h("summary", { style: { color: "var(--bad)" } },
        `${d.erros_importacao.length} erro(s) de importação`), h("ul", { class: "list" }, d.erros_importacao.map(e => h("li", {}, e)))) : null,
      d.avisos.length ? h("details", {}, h("summary", {}, `${d.avisos.length} aviso(s)`), h("ul", { class: "list" }, d.avisos.map(e => h("li", {}, e)))) : null);

    // relógio (seção 8)
    const cg = h("div", { class: "clockgrid" });
    const modo = sel([["sistema", "Relógio do sistema"], ["deslocamento", "Sistema + deslocamento"], ["congelado", "Congelado"]], clk.modo);
    const off = Math.abs(clk.deslocamento_s);
    const sign = sel([["+", "+ adiantado"], ["-", "− atrasado"]], clk.deslocamento_s < 0 ? "-" : "+");
    const hh = h("input", { type: "number", class: "num", style: { width: "70px" }, value: Math.floor(off / 3600) });
    const mm = h("input", { type: "number", class: "num", style: { width: "70px" }, value: Math.floor(off % 3600 / 60) });
    const ss = h("input", { type: "number", class: "num", style: { width: "80px" }, value: +(off % 60).toFixed(3), step: "0.001" });
    const fz = new Date((clk.congelado_em || nowSrv() / 1000) * 1000);
    const frozen = h("input", { type: "datetime-local", step: "0.001", value: `${fz.getFullYear()}-${pad(fz.getMonth() + 1)}-${pad(fz.getDate())}T${pad(fz.getHours())}:${pad(fz.getMinutes())}:${pad(fz.getSeconds())}` });
    const tq = h("input", { value: clk.qualidade_t, class: "mono", style: { width: "80px" } });
    const falha = chk("Relógio com falha (liga falha e não sincronizado — RN-TMP-05)", clk.falha);
    const tick = () => {
      const now = nowSrv();
      const iedMs = clk.modo === "congelado" ? clk.congelado_em * 1000 : clk.modo === "deslocamento" ? now + clk.deslocamento_s * 1000 : now;
      const diff = (iedMs - now) / 1000;
      const dh = Math.trunc(diff / 3600), dm = Math.trunc(Math.abs(diff) % 3600 / 60), ds = Math.abs(diff) % 60;
      put(cg, 
        h("div", {}, h("b", {}, "Hora local (máquina)"), h("span", {}, fmtMs(now, false))),
        h("div", {}, h("b", {}, "Hora UTC"), h("span", {}, fmtMs(now, true))),
        h("div", {}, h("b", {}, "Hora do IED (UTC no fio)"), h("span", {}, fmtMs(iedMs, true))),
        h("div", {}, h("b", {}, "Deslocamento"), h("span", { class: Math.abs(diff) > 0.5 ? "diff" : "" },
          Math.abs(diff) < 0.0005 ? "0" : `${diff < 0 ? "−" : "+"}${Math.abs(dh)}h${pad(dm)}m${ds.toFixed(1)}s`)));
    };
    tick();
    clearInterval(clockTimer);
    clockTimer = setInterval(tick, 200);
    const relogio = h("div", { class: "card" }, h("h2", {}, "Relógio do IED"), cg,
      h("p", { class: "muted small" }, `Qualidade de tempo efetiva: ${clk.qualidade_t_efetiva}. O deslocamento vale só para estampas novas (RN-TMP-03).`),
      h("div", { class: "form" }, field("Fonte", modo), field("Sentido", sign),
        field("Horas", hh), field("Minutos", mm), field("Segundos", ss),
        field("Congelado em (hora local)", frozen), field("Qualidade de t (byte)", tq)),
      h("div", { class: "row", style: { marginTop: "8px" } }, falha.el, h("span", { class: "spacer" }),
        h("button", { onclick: () => saveClock({ modo: "deslocamento", horas: 3, minutos: 0, segundos: 0, negativo: true }) }, "Simular i15 (−3 h)"),
        h("button", {
          class: "primary", onclick: () => saveClock({
            modo: modo.value, horas: +hh.value || 0, minutos: +mm.value || 0, segundos: +ss.value || 0, negativo: sign.value === "-",
            congelado_em: frozen.value || null, qualidade_t: tq.value, falha: falha.input.checked,
          }),
        }, "Aplicar")));
    async function saveClock(b) {
      const r = await send("PUT", `/ieds/${id}/clock`, b);
      if (r) { toast("Relógio alterado", "ok"); render(); }
    }

    // SAGE e rede
    const hl = hostLine(d.ip, d.nome_ied, d.nome);
    const porta = h("input", { type: "number", class: "num", value: d.porta });
    const picker = await ipPicker(d.ip, { iface: d.config.interface, uid: "p" + id, onPick: c => hl.set(c.ip) });
    const rede = h("div", { class: "card", style: { gridColumn: "1 / -1" } }, h("h2", {}, "Configuração para o SAGE e rede"),
      hl.el, h("h3", {}, "Interface de escuta (RN-NET-12 a RN-NET-17)"), picker.el,
      h("div", { class: "row", style: { marginTop: "8px" } }, field("Porta TCP/MMS", porta),
        h("span", { class: "muted small", style: { alignSelf: "flex-end" } }, "O SAGE conecta sempre na 102; outra porta só serve a clientes que a aceitem (RN-NET-05)."),
        h("span", { class: "spacer" }),
        h("button", {
          class: "primary", style: { alignSelf: "flex-end" }, onclick: async () => {
            const c = picker.get();
            const r = await send("PATCH", `/ieds/${id}`, { ip: c?.ip || "", interface: c?.iface || "", porta: +porta.value });
            if (r) { toast("Interface/IP salvos", "ok"); render(); }
          },
        }, "Salvar interface e IP")));

    // parâmetros e CID
    const nome = h("input", { value: d.nome });
    const nomeIed = h("input", { value: d.nome_ied, class: "mono" });
    const perfil = sel(S.profiles.map(p => [p.id, p.id]), d.perfil);
    const maxa = h("input", { type: "number", class: "num", value: d.max_associacoes, min: 1, max: 64 });
    const rest = chk("Ao reiniciar a aplicação, voltar os pontos ao último valor salvo", d.config.opcoes?.restaurar_valores !== false);
    const reFile = h("input", { type: "file", accept: ".cid,.icd,.scd,.iid,.xml" });
    const params = h("div", { class: "card" }, h("h2", {}, "Parâmetros"),
      h("div", { class: "form" }, field("Nome", nome), field("Nome do IED (MMS)", nomeIed), field("Perfil", perfil), field("Máx. associações", maxa)),
      h("div", { style: { marginTop: "8px" } }, rest.el),
      h("p", { class: "muted small" }, "Trocar perfil ou nome do IED com o IED rodando pede confirmação e derruba as associações (RN-PRF-04)."),
      h("div", { class: "row" }, h("span", { class: "spacer" }), h("button", {
        class: "primary", onclick: async () => {
          const r = await send("PATCH", `/ieds/${id}`, {
            nome: nome.value, nome_ied: nomeIed.value, perfil: perfil.value, max_associacoes: +maxa.value,
            opcoes: { restaurar_valores: rest.input.checked },
          });
          if (r) { toast("Parâmetros salvos", "ok"); render(); }
        },
      }, "Salvar")),
      h("h3", {}, "CID"),
      h("div", { class: "row" }, h("a", { class: "btn", href: dl(`/ieds/${id}/export-cid`) }, "⤓ Exportar CID com extras (RN-CID-06)")),
      h("div", { class: "row", style: { marginTop: "8px" } }, field("Subir novo CID do mesmo relé (mantém estado, lógicas e comandos)", reFile)));
    reFile.addEventListener("change", async () => {
      const f = reFile.files[0];
      reFile.value = "";
      if (f && await reimportCid(id, d.ied_scl, f)) render();
    });

    // sessões
    const sess = h("div", { class: "card" }, h("h2", {}, "Clientes associados (RN-NET-11)"),
      d.sessoes.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Cliente", "Desde", "Req.", "RCBs habilitados", "Reservados"].map(x => h("th", {}, x)))),
        h("tbody", {}, d.sessoes.map(s => h("tr", {}, h("td", { class: "mono" }, s.cliente), h("td", {}, tsEl(s.inicio)), h("td", { class: "right" }, s.requisicoes),
          h("td", { class: "mono small" }, s.rcbs_habilitados.map(x => x.split("$").pop()).join(", ") || "—"),
          h("td", { class: "mono small" }, s.rcbs_reservados.map(x => x.split("$").pop()).join(", ") || "—"))))))
        : h("p", { class: "muted" }, d.estado === "rodando" ? "Nenhum cliente associado." : "IED parado."));

    const falhas = h("div", { class: "card" }, h("h2", {}, "Falhas ativas"),
      d.falhas_ativas.length ? h("ul", { class: "list" }, d.falhas_ativas.map(f => h("li", {}, h("b", {}, f.regra), " ", f.titulo)))
        : h("p", { class: "muted" }, "Nenhuma falha injetada."), h("a", { href: `#/ied/${id}/falhas` }, "Configurar falhas →"));

    const opm = h("div", { class: "card" }, h("h2", {}, "Decodificar OPMSK do SAGE"), opmskWidget());

    put(el, resumo, relogio, rede, params, h("div", { class: "stack" }, sess, falhas), opm);
  }
  render().catch(e => put(el, h("div", { class: "alert bad" }, e.message)));
  const re = throttle(() => render().catch(() => {}), 1500);
  return {
    el,
    onEvent: m => { if (["session.opened", "session.closed", "clock.changed"].includes(m.evento)) re(); },
    destroy: () => clearInterval(clockTimer),
  };
}

// ---------------------------------------------------------------------------- sinais digitais e analógicos (seções 5 e 6)
const MODOS = [["manual", "Manual"], ["espelhado", "Espelhado"], ["invertido", "Espelhado invertido"], ["forcado", "Forçado"]];
const DPS_OPTS = [[0, "00 interm."], [1, "01 aberto"], [2, "10 fechado"], [3, "11 inválido"]];
const DETAIL = ["overflow", "outOfRange", "badReference", "oscillatory", "failure", "oldData", "inconsistent", "inaccurate"];

function subPoints(d, kind) {
  const id = d.id;
  const rows = new Map();
  const selected = new Set();
  let last = [];
  const q = h("input", { placeholder: "Buscar por caminho ou descrição", style: { width: "280px" }, value: store(`q.${kind}`) || "" });
  const ln = sel([["", "Todos os LNs"]], "");
  const ds = sel([["", "Todos os DataSets"]], "");
  const modo = sel([["", "Todos os modos"], ...MODOS], "");
  const ro = chk("incluir somente leitura", false);
  const keepT = chk("manter t ao editar (RN-ANA-06)", false);
  const info = h("span", { class: "muted small" });
  const groupBar = h("div", { class: "row card hidden", style: { padding: "8px 12px" } });
  const tbody = h("tbody");
  const allChk = h("input", { type: "checkbox", title: "Selecionar todos os visíveis" });
  const heads = kind === "dig"
    ? ["Caminho", "CDC", "DataSets", "Valor", "Modo", "Qualidade", "t", ""]
    : ["Caminho", "CDC", "Valor", "Unidade / faixa", "Zona morta", "Qualidade", "t", "Modo", ""];
  const csvBtn = kind === "ana" ? h("button", { onclick: () => importCsv() }, "Importar CSV (RN-ANA-07)") : null;
  const scBtn = h("button", { onclick: () => saveScenario() }, "Salvar estado como cenário");
  const el = h("div", { class: "stack" },
    h("div", { class: "row" }, q, ln, ds, modo, ro.el, kind === "ana" ? keepT.el : null, h("span", { class: "spacer" }), csvBtn, scBtn),
    info, groupBar,
    h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, allChk), heads.map(x => h("th", {}, x)))), tbody)));
  let first = true;

  async function load() {
    const params = new URLSearchParams({ tipo: kind + (ro.input.checked ? ",ro" : ""), q: q.value, ln: ln.value, dataset: ds.value, modo: modo.value, limit: 1000 });
    store(`q.${kind}`, q.value);
    let r;
    try { r = await api("GET", `/ieds/${id}/points?${params}`); } catch { return; }
    if (first) {
      first = false;
      put(ln, ...[["", "Todos os LNs"], ...r.lns.map(x => [x, x])].map(([v, l]) => h("option", { value: v }, l)));
      put(ds, ...[["", "Todos os DataSets"], ...r.datasets.map(x => [x, x.split("/").pop()])].map(([v, l]) => h("option", { value: v }, l)));
    }
    last = r.pontos;
    info.textContent = `${r.total} ponto(s)${r.total > r.pontos.length ? `, mostrando ${r.pontos.length} — refine a busca` : ""}.` +
      (kind === "dig" ? " Mudar a chave grava stVal e t com o relógio do IED e dispara o report se o ponto estiver num DataSet com RCB habilitado." : "");
    rows.clear();
    put(tbody, ...r.pontos.map(p => { const row = makeRow(p); rows.set(p.id, row); return row.tr; }));
    if (!r.pontos.length) tbody.append(h("tr", {}, h("td", { colspan: heads.length + 1 }, h("div", { class: "empty" }, "Nenhum ponto com esses filtros."))));
    updateGroup();
  }

  function pathCell(p) {
    const rel = p.path.split("/").slice(1).join("/");
    return h("td", {}, h("div", { class: "path", title: p.path }, rel), p.desc ? h("div", { class: "desc" }, p.desc) : null);
  }
  const locked = p => p.modo === "espelhado" || p.modo === "invertido";

  function valueControl(p) {
    if (p.tipo === "ro") return h("span", { class: "muted small", title: "CDC não tratado: valor padrão, somente leitura (RN-CID-05)" }, fmtVal(p.valor), " 🔒");
    if (kind === "dig") {
      if (p.cdc === "DPS" || p.cdc === "DPC") {
        return h("div", { class: "row", style: { gap: "4px" } }, locked(p) ? h("span", { class: "lock", title: "Espelhado: passe para forçado para mudar (RN-UI-02)" }, "🔒") : null,
          h("div", { class: "seg" }, DPS_OPTS.map(([v, l]) => h("button", {
            class: p.valor === v ? "on" : "", disabled: locked(p), onclick: () => setVal(p, v),
          }, l))));
      }
      const on = !!p.valor;
      return h("div", { class: "row", style: { gap: "4px" } },
        h("button", { class: "sw" + (on ? " on" : ""), disabled: locked(p), title: locked(p) ? "Espelhado: passe para forçado (RN-UI-02)" : "", onclick: () => setVal(p, !on) },
          h("span", { class: "lbl" }, on ? "ON" : "OFF")),
        locked(p) ? h("span", { class: "lock", title: "Valor vindo do relé real (RN-UI-02)" }, "🔒") : null);
    }
    // analógicos
    const dis = locked(p);
    if (p.cdc === "CMV") {
      const mag = h("input", { class: "num", value: p.valor?.mag ?? 0, disabled: dis, title: "cVal.mag.f" });
      const ang = h("input", { class: "num", value: p.valor?.ang ?? 0, disabled: dis, title: "cVal.ang.f" });
      const commit = () => setVal(p, { mag: +String(mag.value).replace(",", "."), ang: +String(ang.value).replace(",", ".") });
      for (const i of [mag, ang]) { i.addEventListener("change", commit); i.addEventListener("keydown", e => { if (e.key === "Enter") i.blur(); }); }
      return h("div", { class: "row", style: { gap: "4px" } }, h("span", { class: "small muted" }, "mód"), mag, h("span", { class: "small muted" }, "âng"), ang, dis ? "🔒" : null);
    }
    if (p.enum) {
      return sel(Object.entries(p.enum).map(([o, t]) => [o, `${o} ${t}`]), p.valor, { disabled: dis, onchange: e => setVal(p, +e.target.value) });
    }
    const inp = h("input", { class: "num", value: p.valor ?? 0, disabled: dis, title: p.cdc === "MV" ? "mag.f" : "stVal" });
    inp.addEventListener("change", () => setVal(p, String(inp.value).replace(",", ".")));
    inp.addEventListener("keydown", e => { if (e.key === "Enter") inp.blur(); if (e.key === "Escape") { inp.value = p.valor; inp.blur(); } });
    return h("div", { class: "row", style: { gap: "4px" } }, inp, dis ? "🔒" : null,
      p.valor_i != null ? h("span", { class: "small muted mono", title: "inteiro equivalente (mag.i)" }, `i=${p.valor_i}`) : null);
  }

  function modeControl(p) {
    if (p.tipo === "ro") return h("span", { class: "muted" }, "—");
    const s = sel(MODOS, p.modo, { onchange: () => changeMode(p, s.value, s) });
    return h("div", { class: "row", style: { gap: "4px" } }, s,
      p.modo === "forcado" ? h("button", { class: "sm", title: `Voltar para ${p.modo_anterior}`, onclick: () => patch(p, { liberar: true }) }, "Liberar") : null,
      p.vinculo ? h("span", { class: "badge info", title: `${p.vinculo.ip}:${p.vinculo.porta || 102} ${p.vinculo.caminho || ""}` }, "vínculo") : null);
  }

  function makeRow(p) {
    const tr = h("tr", { class: selected.has(p.id) ? "sel" : "" });
    const cb = h("input", { type: "checkbox", checked: selected.has(p.id), disabled: p.tipo === "ro" });
    cb.addEventListener("change", () => { cb.checked ? selected.add(p.id) : selected.delete(p.id); tr.classList.toggle("sel", cb.checked); updateGroup(); });
    function fill(pp) {
      p = pp;
      const qcell = h("td", {}, h("a", { href: "javascript:void 0", onclick: () => p.tipo !== "ro" && qualityDlg([p]) }, qualBadge(p.qualidade)));
      const tcell = h("td", {}, tsEl(p.t), p.t_qualidade ? h("span", { class: "ts2" }, `TimeQuality ${p.t_qualidade}`) : null);
      const acts = h("td", { class: "nowrap right" },
        kind === "dig" && p.tipo !== "ro" ? h("button", { class: "sm", disabled: locked(p), onclick: () => pulseDlg(p) }, "Pulso") : null, " ",
        p.tipo !== "ro" ? h("button", { class: "sm", onclick: () => linkDlg(p) }, "Vínculo") : null);
      const cells = kind === "dig"
        ? [h("td", {}, cb), pathCell(p), h("td", {}, h("span", { class: "badge neutral" }, p.cdc)),
          h("td", {}, h("div", { class: "pill-list" }, p.datasets.map(x => h("span", { class: "badge info", title: x.ds }, `${x.ds.split("$").pop()} #${x.pos}`)))),
          h("td", {}, valueControl(p)), h("td", {}, modeControl(p)), qcell, tcell, acts]
        : [h("td", {}, cb), pathCell(p), h("td", {}, h("span", { class: "badge neutral" }, p.cdc)),
          h("td", {}, valueControl(p)),
          h("td", { class: "small" }, p.unidade || "", p.faixa ? h("div", { class: "desc mono" }, `[${p.faixa[0]} … ${p.faixa[1]}]`) : null,
            p.sVC ? h("div", { class: "desc mono" }, `sVC ${p.sVC.join(" / ")}`) : null),
          h("td", { class: "small mono" }, p.db || p.zeroDb ? `db ${p.db} · zeroDb ${p.zeroDb}` : "—"),
          qcell, tcell, h("td", {}, modeControl(p)), acts];
      put(tr, ...cells);
    }
    fill(p);
    return { tr, update: pp => { fill(pp); tr.classList.remove("flash"); void tr.offsetWidth; tr.classList.add("flash"); }, get: () => p };
  }

  function updateGroup() {
    allChk.checked = last.length > 0 && last.every(p => p.tipo === "ro" || selected.has(p.id));
    if (!selected.size) { groupBar.classList.add("hidden"); return; }
    groupBar.classList.remove("hidden");
    const ids = [...selected];
    const pts = ids.map(i => rows.get(i)?.get()).filter(Boolean);
    put(groupBar, h("b", {}, `${selected.size} selecionado(s)`), h("span", { class: "muted small" }, "mudam na mesma requisição e entram juntos no report quando estão no mesmo DataSet (RN-DIG-09)"),
      h("span", { class: "spacer" }),
      kind === "dig" ? [h("button", { onclick: () => batch(pts, p => ({ valor: p.cdc === "DPS" || p.cdc === "DPC" ? 2 : true })) }, "Todos ON"),
        h("button", { onclick: () => batch(pts, p => ({ valor: p.cdc === "DPS" || p.cdc === "DPC" ? 1 : false })) }, "Todos OFF")]
        : h("button", { onclick: async () => { const r = await promptDlg("Valor para os selecionados", [{ k: "v", label: "Valor", value: "0" }]); if (r) batch(pts, () => ({ valor: r.v.replace(",", ".") })); } }, "Definir valor"),
      h("button", { onclick: () => qualityDlg(pts) }, "Qualidade…"),
      h("button", { class: "ghost", onclick: () => { selected.clear(); load(); } }, "Limpar seleção"));
  }
  allChk.addEventListener("change", () => {
    for (const p of last) if (p.tipo !== "ro") allChk.checked ? selected.add(p.id) : selected.delete(p.id);
    for (const [pid, r] of rows) r.tr.classList.toggle("sel", selected.has(pid));
    for (const cb of tbody.querySelectorAll("td:first-child input")) cb.checked = allChk.checked && !cb.disabled;
    updateGroup();
  });

  async function patch(p, body) {
    const r = await send("PATCH", `/ieds/${id}/points/${p.id}`, body);
    if (r) rows.get(p.id)?.update(r);
    else rows.get(p.id)?.update(p);
    return r;
  }
  const setVal = (p, v) => patch(p, kind === "ana" && keepT.input.checked ? { valor: v, manter_t: true } : { valor: v });
  async function batch(pts, fn) {
    const r = await send("POST", `/ieds/${id}/points:batch`, { operacoes: pts.map(p => ({ id: p.id, ...fn(p), ...(kind === "ana" && keepT.input.checked ? { manter_t: true } : {}) })) });
    if (r) toast(`${r.resultados.filter(x => x.alterado).length} de ${r.resultados.length} ponto(s) alterados`, "ok");
  }
  async function changeMode(p, m, s) {
    if ((m === "espelhado" || m === "invertido") && !p.vinculo) {
      s.value = p.modo;
      return linkDlg(p, m);
    }
    const r = await patch(p, { modo: m });
    if (!r) s.value = p.modo;
  }

  function qualityDlg(pts) {
    const q0 = pts[0].qualidade || { validity: "good", detailQual: [], source: "process" };
    const v = sel(["good", "invalid", "questionable", "reserved"], q0.validity);
    const src = sel(["process", "substituted"], q0.source);
    const dets = DETAIL.map(n => chk(n, (q0.detailQual || []).includes(n)));
    const test = chk("test", q0.test), ob = chk("operatorBlocked", q0.operatorBlocked);
    return modal(`Qualidade — ${pts.length === 1 ? pts[0].path.split("/").pop() : pts.length + " pontos"}`, h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Mudar só a qualidade gera qchg; junto com o valor, um único evento com dchg e qchg (RN-DIG-02)."),
      h("div", { class: "form" }, field("validity", v), field("source", src)),
      h("h3", {}, "detailQual"), h("div", { class: "row" }, dets.map(x => x.el)),
      h("div", { class: "row" }, test.el, ob.el)), [
      { label: "Cancelar" },
      {
        label: "Aplicar", cls: "primary", click: async () => {
          const qual = { validity: v.value, source: src.value, detailQual: DETAIL.filter((_, i) => dets[i].input.checked), test: test.input.checked, operatorBlocked: ob.input.checked };
          if (pts.length === 1) return await patch(pts[0], { qualidade: qual });
          return await send("POST", `/ieds/${id}/points:batch`, { operacoes: pts.map(p => ({ id: p.id, qualidade: qual })) });
        },
      }]);
  }

  function pulseDlg(p) {
    const tipo = sel([["pulso", "Pulso (liga e desliga após N ms)"], ["osc", "Oscilação (N ciclos com período)"]], "pulso");
    const ms = h("input", { type: "number", class: "num", value: 500 });
    const ciclos = h("input", { type: "number", class: "num", value: 5 });
    const per = h("input", { type: "number", class: "num", value: 200 });
    const oscF = h("div", { class: "form hidden" }, field("Ciclos", ciclos), field("Período (ms)", per));
    const pulF = h("div", { class: "form" }, field("Duração (ms)", ms));
    tipo.addEventListener("change", () => { oscF.classList.toggle("hidden", tipo.value !== "osc"); pulF.classList.toggle("hidden", tipo.value === "osc"); });
    return modal(`Pulso / oscilação — ${p.path.split("/").pop()}`, h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Para testar pontos que alternam rápido (RN-DIG-08). O valor volta ao atual no fim."), field("Modo", tipo), pulF, oscF), [
      { label: "Cancelar" },
      {
        label: "Executar", cls: "primary", click: () => send("POST", `/ieds/${id}/points/${p.id}/pulse`,
          tipo.value === "osc" ? { ms: +per.value / 2, ciclos: +ciclos.value, periodo_ms: +per.value } : { ms: +ms.value, ciclos: 1 }),
      }]);
  }

  function linkDlg(p, wantMode) {
    const l = p.vinculo || {};
    const ip = h("input", { value: l.ip || "", class: "mono", placeholder: "IP do relé real" });
    const porta = h("input", { type: "number", class: "num", value: l.porta || 102 });
    const ied = h("input", { value: l.ied || d.nome_ied, class: "mono" });
    const cam = h("input", { value: l.caminho || "", class: "mono", placeholder: p.path.split("/").slice(1).join("/") });
    const iv = h("input", { type: "number", class: "num", value: l.intervalo_ms || 1000 });
    const rt = chk("replicar t do relé (padrão — compara com o que o SAGE recebe do relé real)", l.replica_t !== false);
    const rq = chk("replicar qualidade do relé", !!l.replica_q);
    const inv = chk("qualidade inválida na perda de comunicação (RN-DIG-07)", l.invalida_na_perda !== false);
    const qp = sel(["invalid", "questionable"], l.qualidade_na_perda || "invalid");
    const m = sel(MODOS.slice(1, 3), wantMode || (p.modo === "invertido" ? "invertido" : "espelhado"));
    const vinc = () => ({
      ip: ip.value.trim(), porta: +porta.value || 102, ied: ied.value.trim(), caminho: cam.value.trim(), intervalo_ms: +iv.value || 1000,
      replica_t: rt.input.checked, replica_q: rq.input.checked, invalida_na_perda: inv.input.checked, qualidade_na_perda: qp.value,
    });
    return modal(`Vínculo com o relé real — ${p.path.split("/").pop()}`, h("div", { class: "stack" },
      h("div", { class: "alert warn" }, "O cliente do vínculo só lê (RN-SEG-02), mas conta como mais uma associação no relé, que já atende os SCADAs. ",
        "Confirme o limite de associações do relé e evite horário crítico (RN-SEG-06)."),
      h("div", { class: "form" }, field("IP do relé real", ip), field("Porta", porta), field("Nome do IED remoto", ied),
        field("Caminho remoto (vazio = mesmo do ponto)", cam, "wide"), field("Intervalo de leitura (ms)", iv), field("Modo", m), field("Qualidade na perda", qp)),
      h("div", { class: "stack" }, rt.el, rq.el, inv.el),
      h("p", { class: "muted small" }, "Leitura periódica por Read: habilitar um RCB no relé real seria escrita, o que RN-SEG-02 proíbe.")), [
      p.vinculo ? { label: "Remover vínculo", cls: "danger", click: () => patch(p, { vinculo: {}, modo: "manual" }) } : null,
      { label: "Cancelar" },
      {
        label: "Salvar e espelhar", cls: "primary", click: async () => {
          const v = vinc();
          if (!v.ip) { toast("Informe o IP do relé real", "bad"); return false; }
          return await patch(p, { vinculo: v, modo: m.value });
        },
      }].filter(Boolean));
  }

  async function importCsv() {
    const ta = h("textarea", { rows: 10, placeholder: "caminho;valor\nMMXU1$MX$Hz;60,02\nMMXU1$MX$TotW;12.5" });
    const file = h("input", { type: "file", accept: ".csv,.txt" });
    file.addEventListener("change", async () => { if (file.files[0]) ta.value = await file.files[0].text(); });
    return modal("Importar valores por CSV", h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Uma linha por ponto: caminho e valor, separados por ponto e vírgula (vírgula decimal) ou vírgula. Fora da faixa entra como exceção confirmada."),
      file, ta), [{ label: "Cancelar" }, {
      label: "Aplicar", cls: "primary", click: async () => {
        const r = await send("POST", `/ieds/${id}/points:csv`, undefined, { text: ta.value });
        if (r) toast(`${r.resultados.filter(x => x.alterado).length} de ${r.resultados.length} ponto(s) alterados`, "ok");
        return r || false;
      },
    }]);
  }

  async function saveScenario() {
    const r = await promptDlg("Salvar estado atual como cenário", [{ k: "nome", label: "Nome do cenário", value: `${d.nome} ${kind === "dig" ? "digitais" : "analógicos"}` }]);
    if (!r || !r.nome) return;
    const s = await send("POST", "/scenarios", { de_ied: id, nome: r.nome, tipos: [kind] });
    if (s) toast(`Cenário “${s.nome}” salvo com ${s.passos.length} passo(s)`, "ok");
  }

  const reload = debounce(load, 250);
  for (const x of [ln, ds, modo]) x.addEventListener("change", load);
  ro.input.addEventListener("change", load);
  q.addEventListener("input", reload);
  load();
  return {
    el,
    onEvent: m => {
      if (m.evento === "point.changed") { const r = rows.get(m.dados.id); if (r) r.update(m.dados); }
      else if (m.evento === "points.reload") load();
    },
  };
}

// ---------------------------------------------------------------------------- vínculos
function subVinculos(d) {
  const id = d.id;
  const el = h("div", { class: "stack" });
  async function load() {
    let r;
    try { r = await api("GET", `/ieds/${id}/links`); } catch { return; }
    put(el, 
      h("div", { class: "alert warn" }, "Cada relé vinculado recebe uma associação de leitura deste simulador além das dos SCADAs (RN-SEG-06). O vínculo só funciona com o IED rodando."),
      h("h3", {}, "Conexões com relés reais"),
      r.grupos.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Relé", "Estado", "Última leitura", "Intervalo", "Pontos", "Erro"].map(x => h("th", {}, x)))),
        h("tbody", {}, r.grupos.map(g => h("tr", {}, h("td", { class: "mono" }, `${g.ip}:${g.porta}`),
          h("td", {}, h("span", { class: "badge " + ({ conectado: "ok", conectando: "warn", erro: "bad" }[g.estado] || "neutral") }, g.estado)),
          h("td", {}, tsEl(g.ultima_atualizacao || null)), h("td", { class: "mono" }, `${g.intervalo_s} s`), h("td", { class: "right" }, g.pontos.length),
          h("td", { class: "small", style: { color: "var(--bad)" } }, g.erro || "")))))) : h("p", { class: "muted" }, d.estado === "rodando" ? "Nenhum vínculo ativo." : "IED parado: os vínculos conectam quando ele inicia."),
      h("h3", {}, "Pontos com vínculo"),
      r.pontos.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Ponto", "Modo", "Valor", "Qualidade", "t", "Relé / caminho remoto"].map(x => h("th", {}, x)))),
        h("tbody", {}, r.pontos.map(p => h("tr", {}, h("td", {}, h("div", { class: "path" }, p.path.split("/").slice(1).join("/")), h("div", { class: "desc" }, p.desc)),
          h("td", {}, h("span", { class: "badge info" }, p.modo)), h("td", { class: "mono" }, fmtVal(p.valor)), h("td", {}, qualBadge(p.qualidade)), h("td", {}, tsEl(p.t)),
          h("td", { class: "mono small" }, `${p.vinculo.ip}:${p.vinculo.porta || 102}`, h("div", { class: "desc" }, `${p.vinculo.ied || d.nome_ied} · ${p.vinculo.caminho || "(mesmo caminho)"}`))))))) :
        h("p", { class: "muted" }, "Nenhum ponto vinculado. Use o botão Vínculo nas telas de sinais."));
  }
  load();
  const re = throttle(load, 800);
  return { el, onEvent: m => { if (m.evento === "link.state" || (m.evento === "point.changed" && m.dados.vinculo)) re(); } };
}

// ---------------------------------------------------------------------------- comandos (serviço de controle)
const canOperate = () => !S.eu || S.eu.papel !== "leitura";
const DBPOS_TXT = { 0: "intermediário (00)", 1: "ABERTO (01)", 2: "FECHADO (10)", 3: "inválido (11)" };
function estadoBadge(cdc, v) {
  if (v == null) return h("span", { class: "muted" }, "—");
  if (cdc === "DPC" || cdc === "DPS") return h("span", { class: "badge " + ({ 1: "ok", 2: "bad", 0: "warn", 3: "warn" }[v] || "neutral") }, DBPOS_TXT[v] ?? v);
  if (typeof v === "boolean") return h("span", { class: "badge " + (v ? "bad" : "ok") }, v ? "LIGADO (true)" : "DESLIGADO (false)");
  return h("span", { class: "mono" }, fmtVal(v));
}
// Ponto de controle (CGF) do SAGE: KCONV conforme o ctlModel do CID (Manual de Configuração do SAGE, Anexo 17).
function sageCell(c) {
  const sg = c.sage || {};
  if (sg.kconv == null) return h("span", { class: "muted", title: sg.texto || "" }, "não comandável");
  return [h("div", { class: "row", style: { gap: "6px" } }, h("span", { class: "muted" }, "KCONV"),
      sg.kconv ? h("span", { class: "badge info mono" }, sg.kconv) : h("span", { class: "badge neutral" }, "vazio")),
    h("div", { class: "mono small muted", title: "ID do CGF no SAGE (precedido do prefixo do IED na sua base)" }, "…" + sg.cgf),
    h("div", { class: "desc" }, sg.texto)];
}
const ctlButtons = c => c.cdc === "DPC" ? [["ABRIR", false], ["FECHAR", true]] : c.cdc === "SPC" ? [["DESLIGAR", false], ["LIGAR", true]] : [];

// ---------------------------------------------------------------------------- subida de pontos
function subSubida(d) {
  const id = d.id, u = `/ieds/${id}/subida`;
  const el = h("div", { class: "stack" });
  const DPS = { 0: "intermediario", 1: "aberto", 2: "fechado", 3: "invalido" };
  const ETAPA = { "": ["neutral", "pendente"], inicial: ["info", "no inicial"], final: ["ok", "executada"], retornou: ["ok", "executada (voltou ao inicial)"] };
  let st = null, pontos = null, filtroSit = "todas";
  const marcadas = new Set();
  const trs = new Map();
  const status = h("div"), resumo = h("div", { class: "row small" }), exec = h("div", { class: "row" });
  const tbody = h("tbody"), vazio = h("div");
  const filtro = h("input", { placeholder: "filtrar por ponto, descrição ou observação", style: { minWidth: "260px" } });

  // ------------------------------------------------ entrada manual
  const dlid = "sb-pts-" + id;
  const dlist = h("datalist", { id: dlid });
  const inPonto = h("input", { list: dlid, placeholder: "ponto: escolha ou digite (XCBR1$ST$Pos, i151-XCBR1$ST$Pos…)", style: { minWidth: "360px" }, class: "mono" });
  const caixaIni = h("span"), caixaFim = h("span");
  const inObs = h("input", { placeholder: "observação (opcional)" });
  const ajuda = h("div", { class: "desc" });
  let inIni = h("input", { class: "num", disabled: true }), inFim = h("input", { class: "num", disabled: true });
  put(caixaIni, inIni); put(caixaFim, inFim);
  function editorValor(p, atual) {
    if (p && p.opcoes) return sel([["", "—"], ...p.opcoes.map(o => [o, rotulo(p, o)])], atual ?? "");
    return h("input", { class: "num", value: atual ?? "", placeholder: p ? "valor" : "", disabled: !p });
  }
  function rotulo(p, v) {
    if (p && p.tipo === "dig" && !(p.cdc === "DPS" || p.cdc === "DPC")) return v === "1" ? "1 (ligado)" : v === "0" ? "0 (desligado)" : v;
    return v;
  }
  function acharPonto(txt) {
    if (!pontos || !txt) return null;
    const t = txt.trim();
    return pontos.find(p => p.path === t) || (pontos.filter(p => p.sage === t.replace(/^[A-Za-z]\w*-/, "")).length === 1
      ? pontos.find(p => p.sage === t.replace(/^[A-Za-z]\w*-/, "")) : null);
  }
  function aoEscolher() {
    const p = acharPonto(inPonto.value);
    inIni = editorValor(p, p && p.opcoes && p.opcoes.length === 4 ? "fechado" : p && p.tipo === "dig" ? "1" : "");
    inFim = editorValor(p, p && p.opcoes && p.opcoes.length === 4 ? "aberto" : p && p.tipo === "dig" ? "0" : "");
    put(caixaIni, inIni); put(caixaFim, inFim);
    put(ajuda, p ? [h("b", {}, p.cdc), ` · ${p.desc || "sem descrição"} · aceita: ${p.aceitos} · valor atual: ${p.valor_atual}`] : inPonto.value.trim() ? "ponto não encontrado na lista (a gravação confere de novo)" : "");
  }
  inPonto.addEventListener("change", aoEscolher);
  inPonto.addEventListener("input", debounce(aoEscolher, 200));
  async function adicionar() {
    if (!inPonto.value.trim()) { inPonto.focus(); return; }
    const r = await send("POST", u + "/linhas", { linhas: [{ ponto: inPonto.value.trim(), inicial: inIni.value, final: inFim.value, obs: inObs.value }] });
    if (!r) return;
    inPonto.value = ""; inObs.value = ""; aoEscolher(); inPonto.focus();
    carregar();
  }
  for (const x of [inPonto, inObs]) x.addEventListener("keydown", e => { if (e.key === "Enter") adicionar(); });
  async function carregarPontos() {
    if (pontos) return;
    try { pontos = (await api("GET", u + "/pontos")).pontos; } catch { pontos = []; }
    put(dlist, pontos.map(p => h("option", { value: p.path }, `${p.cdc} · ${p.desc || p.sage}`)));
  }

  // ------------------------------------------------ importação
  const arquivo = h("input", { type: "file", accept: ".xlsx,.xlsm,.csv,.txt", style: { display: "none" } });
  arquivo.addEventListener("change", async () => {
    const f = arquivo.files[0];
    arquivo.value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("arquivo", f);
    let p;
    try { p = await api("POST", `/ieds/${id}/subida-importar`, fd); } catch (e) { toast(e.message, "bad"); return; }
    previa(f.name, p);
  });
  function previa(nome, p) {
    const modo = sel([["acrescentar", "Acrescentar ao fim da lista"], ["substituir", "Substituir a lista atual"]], st && st.linhas.length ? "acrescentar" : "substituir");
    const so = chk("Mostrar só linhas com erro ou aviso", p.com_erro > 0);
    const tb = h("tbody");
    const desenha = () => put(tb, p.linhas.filter(x => !so.input.checked || x.erros.length || x.avisos.length).map(x => h("tr", { class: x.erros.length ? "sb-erro" : "" },
      h("td", { class: "num" }, String(x.linha)),
      h("td", { class: "mono small" }, x.entrada.ponto, x.ponto && x.ponto !== x.entrada.ponto ? h("div", { class: "desc" }, "→ " + x.ponto) : null),
      h("td", { class: "small" }, x.entrada.inicial, x.inicial !== x.entrada.inicial && !x.erros.length ? h("div", { class: "desc" }, "→ " + x.inicial) : null),
      h("td", { class: "small" }, x.entrada.final, x.final !== x.entrada.final && !x.erros.length ? h("div", { class: "desc" }, "→ " + x.final) : null),
      h("td", { class: "small" }, x.obs),
      h("td", { class: "small" }, x.erros.map(e => h("div", { class: "sb-msg bad" }, "✗ " + e)), x.avisos.map(a => h("div", { class: "sb-msg warn" }, "⚠ " + a)),
        !x.erros.length && !x.avisos.length ? h("span", { class: "badge ok" }, "ok") : null))));
    so.input.addEventListener("change", desenha);
    desenha();
    modal(`Importar ${nome}`, h("div", { class: "stack" },
      h("div", { class: "row" }, h("span", { class: "badge ok" }, `${p.validas} válida(s)`), " ",
        p.ignoradas ? h("span", { class: "badge neutral" }, `${p.ignoradas} com Incluir = N (ignoradas)`) : null, " ",
        p.com_erro ? h("span", { class: "badge bad" }, `${p.com_erro} com erro`) : null, " ",
        p.linhas.some(x => x.avisos.length) ? h("span", { class: "badge warn" }, `${p.linhas.filter(x => x.avisos.length).length} com aviso`) : null),
      p.com_erro ? h("div", { class: "alert warn" }, "As linhas com erro não serão importadas. Corrija a planilha (o número é a linha no Excel) e importe de novo, ou importe só as válidas agora.") : null,
      h("div", { class: "row" }, field("Como importar", modo), so.el),
      h("div", { class: "tablewrap", style: { maxHeight: "50vh" } }, h("table", {},
        h("thead", {}, h("tr", {}, ["Linha", "Ponto", "Inicial", "Final", "Observação", "Situação"].map(x => h("th", {}, x)))), tb))),
    [{ label: "Cancelar", value: null },
      {
        label: `Importar ${p.validas} linha(s)`, cls: "primary", click: async () => {
          if (!p.validas) { toast("Nenhuma linha válida", "bad"); return false; }
          if (modo.value === "substituir" && st && st.linhas.length && !await confirmDlg(`Substituir as ${st.linhas.length} linhas atuais (e os resultados delas)?`, "Substituir", true)) return false;
          const r = await send("POST", u + "/linhas", { linhas: p.linhas.filter(x => !x.erros.length).map(x => ({ linha: x.linha, ponto: x.ponto, inicial: x.inicial, final: x.final, obs: x.obs })), modo: modo.value });
          if (!r) return false;
          toast(`${r.adicionadas} linha(s) importada(s)`, "ok");
          carregar();
          return true;
        },
      }], { cls: "wide" });
  }

  async function planilhaPreenchida() {
    await carregarPontos();
    const dig = chk("Digitais (SPS, DPS, …)", true), ana = chk("Analógicos e enumerados", false);
    const soDs = chk("Só pontos que estão em algum DataSet (os que o SAGE recebe por report)", true);
    const conta = h("p", { class: "small" });
    const tipos = () => [dig.input.checked && "dig", ana.input.checked && "ana"].filter(Boolean);
    const recontar = () => {
      const ts = tipos();
      const n = pontos.filter(p => ts.includes(p.tipo) && (!soDs.input.checked || p.datasets.length)).length;
      put(conta, h("b", {}, String(n)), " sinal(is) entram na planilha.");
    };
    for (const c of [dig, ana, soDs]) c.input.addEventListener("change", recontar);
    recontar();
    await modal("Planilha preenchida", h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Cada sinal vem numa linha com estados sugeridos: DPS fechado → aberto, digitais 0 → 1, medidas e enumerados a partir do valor atual. ",
        "Revise, troque os estados que precisar e marque N na coluna Incluir para o que não for testar; depois importe aqui."),
      dig.el, ana.el, soDs.el, conta),
    [{ label: "Cancelar", value: null },
      {
        label: "⤓ Baixar", cls: "primary", click: () => {
          if (!tipos().length) { toast("Escolha digitais e/ou analógicos", "bad"); return false; }
          location.href = dl(`/ieds/${id}/subida-planilha?modo=preenchida&tipos=${tipos().join(",")}&so_dataset=${soDs.input.checked}`);
          return true;
        },
      }]);
  }

  // ------------------------------------------------ execução
  const espIni = h("input", { type: "number", class: "num", min: 0, step: 0.5 });
  const espEntre = h("input", { type: "number", class: "num", min: 0, step: 0.5 });
  const retornar = chk("Voltar ao estado inicial depois do final", false);
  const salvarCfg = debounce(() => send("PUT", u + "/config", { espera_inicial_s: +espIni.value || 0, espera_entre_s: +espEntre.value || 0, retornar: retornar.input.checked }), 400);
  for (const x of [espIni, espEntre]) x.addEventListener("input", salvarCfg);
  retornar.input.addEventListener("change", salvarCfg);
  async function executar(ids, soPendentes) {
    const r = await send("POST", u + "/executar", { ids, so_pendentes: soPendentes });
    if (r) { st.execucao = r; desenhaExec(); }
  }
  const ctl = async acao => { const r = await send("POST", `${u}/${acao}`); if (r) { st.execucao = r; desenhaExec(); } };
  function desenhaExec() {
    const e = st.execucao, n = st.linhas.length;
    const atual = e.atual != null ? st.linhas.findIndex(x => x.id === e.atual) + 1 : 0;
    put(exec,
      e.rodando ? h("span", { class: "badge " + (e.pausado ? "warn" : "info") }, e.pausado ? "pausada" : "executando") : null,
      e.rodando ? h("span", { class: "small" }, atual ? `linha ${atual} de ${n} · ${e.fila.length} na fila` : `${e.fila.length} na fila`) : null,
      !e.rodando ? h("button", { class: "primary", onclick: () => executar(null, true), title: "Linhas sem execução e sem conferência" }, "▶ Executar pendentes") : null,
      !e.rodando && marcadas.size ? h("button", { onclick: () => executar([...marcadas], false) }, `▶ Executar marcadas (${marcadas.size})`) : null,
      !e.rodando ? h("button", { onclick: async () => { if (await confirmDlg("Executar de novo todas as linhas, inclusive as já executadas?", "Executar todas")) executar(null, false); } }, "▶ Todas") : null,
      e.rodando && !e.pausado ? h("button", { onclick: () => ctl("pausar") }, "⏸ Pausar") : null,
      e.rodando && e.pausado ? h("button", { class: "primary", onclick: () => ctl("continuar") }, "▶ Continuar") : null,
      e.rodando ? h("button", { class: "danger", onclick: () => ctl("parar") }, "■ Parar") : null);
  }

  // ------------------------------------------------ tabela
  function passa(ln) {
    const f = filtro.value.trim().toLowerCase();
    if (f && !`${ln.ponto} ${ln.desc || ""} ${ln.obs} ${ln.sage || ""}`.toLowerCase().includes(f)) return false;
    return {
      todas: true, pendentes: !ln.executado_em && !ln.resultado, executadas: !!ln.executado_em && !ln.resultado,
      ok: ln.resultado === "ok", falha: ln.resultado === "falha", erro: !!ln.erro || ln.existe === false,
    }[filtroSit];
  }
  async function patch(ln, body) {
    const r = await send("PATCH", `${u}/linhas/${ln.id}`, body);
    if (r) atualizaLinha(r);
    return r;
  }
  function celulaValor(ln, campo) {
    const p = ln.existe ? { opcoes: ln.opcoes, tipo: ln.tipo, cdc: ln.cdc } : null;
    const ed = editorValor(p, ln[campo]);
    ed.disabled = !ln.existe || (st.execucao.rodando && st.execucao.atual === ln.id);
    ed.addEventListener("change", async () => { if (!await patch(ln, { [campo]: ed.value })) ed.value = ln[campo]; });
    return ed;
  }
  function linhaTr(ln, i) {
    const cb = h("input", { type: "checkbox", checked: marcadas.has(ln.id) });
    cb.addEventListener("change", () => { cb.checked ? marcadas.add(ln.id) : marcadas.delete(ln.id); desenhaExec(); });
    const [ecls, etxt] = ETAPA[ln.etapa] || ["neutral", ln.etapa];
    const emExec = st.execucao.atual === ln.id;
    const nota = h("input", { value: ln.nota || "", placeholder: "nota", class: "small", style: { width: "120px" } });
    nota.addEventListener("change", () => patch(ln, { nota: nota.value }));
    const conf = v => h("button", {
      class: "sm " + (ln.resultado === v ? (v === "ok" ? "primary" : "danger solid") : ""), title: v === "ok" ? "O SAGE mostrou a mudança certa" : "O SAGE não mostrou ou mostrou errado",
      onclick: () => patch(ln, { resultado: ln.resultado === v ? "" : v }),
    }, v === "ok" ? "✓ OK" : "✗ Falha");
    const acao = (etapa, txt, title) => h("button", {
      class: "sm", title, disabled: !ln.existe || st.execucao.rodando,
      onclick: async () => { const r = await send("POST", `${u}/linhas/${ln.id}/aplicar`, { etapa }); if (r) atualizaLinha(r); },
    }, txt);
    return h("tr", { class: emExec ? "sel" : "", "data-id": ln.id },
      h("td", {}, cb),
      h("td", { class: "num muted" }, String(i + 1)),
      h("td", {}, h("div", { class: "path mono small" }, ln.ponto),
        ln.existe ? h("div", { class: "desc" }, [ln.desc, ln.sage].filter(Boolean).join(" · ")) : h("div", { class: "sb-msg bad" }, ln.erro_ponto),
        ln.obs ? h("div", { class: "desc" }, "obs: " + ln.obs) : null),
      h("td", { class: "small" }, ln.cdc || "—"),
      h("td", {}, celulaValor(ln, "inicial")),
      h("td", {}, celulaValor(ln, "final")),
      h("td", { class: "mono", title: ln.modo && ln.modo !== "manual" ? `modo ${ln.modo}` : "" }, ln.existe ? rotulo(ln, ln.valor_atual) : "—",
        ln.modo && ln.modo !== "manual" ? h("div", { class: "desc" }, ln.modo) : null),
      h("td", {}, h("span", { class: "badge " + (ln.erro ? "bad" : emExec ? "info" : ecls) }, ln.erro ? "erro" : emExec ? "em execução" : etxt),
        ln.executado_em ? h("div", { class: "desc" }, tsEl(ln.executado_em)) : null,
        ln.erro ? h("div", { class: "sb-msg bad" }, ln.erro) : null),
      h("td", { class: "nowrap" }, conf("ok"), " ", conf("falha"), h("div", {}, nota)),
      h("td", { class: "nowrap" }, acao("inicial", "Inicial", "Aplica só o estado inicial"), " ", acao("final", "Final", "Aplica só o estado final"), " ",
        acao("comutar", "⇄ Comutar", "Inicial, espera e final (usa as esperas configuradas)"),
        h("div", { style: { marginTop: "4px" } },
          h("button", { class: "sm ghost", title: "Subir", disabled: i === 0, onclick: () => mover(ln, i - 1) }, "↑"),
          h("button", { class: "sm ghost", title: "Descer", disabled: i === st.linhas.length - 1, onclick: () => mover(ln, i + 1) }, "↓"),
          h("button", { class: "sm ghost danger", title: "Remover a linha", onclick: async () => { if (await send("DELETE", `${u}/linhas/${ln.id}`)) carregar(); } }, "✕"))));
  }
  async function mover(ln, pos) { if (await send("PATCH", `${u}/linhas/${ln.id}`, { posicao: pos })) carregar(); }
  function atualizaLinha(ln) {
    const i = st.linhas.findIndex(x => x.id === ln.id);
    if (i < 0) return;
    st.linhas[i] = ln;
    const velho = trs.get(ln.id);
    if (velho && velho.isConnected && !velho.contains(document.activeElement)) {
      const novo = linhaTr(ln, i);
      velho.replaceWith(novo);
      trs.set(ln.id, novo);
    }
    desenhaResumo();
  }
  function desenhaResumo() {
    const ls = st.linhas;
    const c = { total: ls.length, pend: ls.filter(x => !x.executado_em && !x.resultado).length, exe: ls.filter(x => x.executado_em).length,
      ok: ls.filter(x => x.resultado === "ok").length, falha: ls.filter(x => x.resultado === "falha").length, erro: ls.filter(x => x.erro || x.existe === false).length };
    put(resumo, h("span", { class: "badge neutral" }, `${c.total} linha(s)`), h("span", { class: "badge neutral" }, `${c.pend} pendente(s)`),
      h("span", { class: "badge info" }, `${c.exe} executada(s)`), h("span", { class: "badge ok" }, `${c.ok} OK no SAGE`),
      c.falha ? h("span", { class: "badge bad" }, `${c.falha} falha(s)`) : null, c.erro ? h("span", { class: "badge bad" }, `${c.erro} com erro`) : null);
  }
  function desenha() {
    trs.clear();
    const vis = st.linhas.map((ln, i) => [ln, i]).filter(([ln]) => passa(ln));
    put(tbody, vis.map(([ln, i]) => { const tr = linhaTr(ln, i); trs.set(ln.id, tr); return tr; }));
    put(vazio, st.linhas.length ? (vis.length ? null : h("div", { class: "empty" }, "Nenhuma linha com esse filtro."))
      : h("div", { class: "empty" }, "Lista vazia. Adicione sinais acima ou importe a planilha modelo."));
    const ie = st.ied;
    put(status, ie.estado !== "rodando"
      ? h("div", { class: "alert warn" }, `O IED está ${ie.estado}: os valores mudam no simulador, mas o SAGE não recebe nada. Inicie o IED.`)
      : !ie.clientes.length ? h("div", { class: "alert warn" }, "Nenhum cliente associado ao IED: o SAGE não vai ver as mudanças. Confira em Rede › Quem está tentando conectar.")
        : h("p", { class: "small" }, h("span", { class: "badge ok" }, "associado"), ` ${ie.clientes.join(", ")}`));
    desenhaResumo();
    desenhaExec();
  }
  async function carregar() {
    try { st = await api("GET", u); } catch (e) { put(el, h("div", { class: "alert bad" }, e.message)); return; }
    if (document.activeElement !== espIni) espIni.value = st.config.espera_inicial_s;
    if (document.activeElement !== espEntre) espEntre.value = st.config.espera_entre_s;
    retornar.input.checked = st.config.retornar;
    for (const x of [...marcadas]) if (!st.linhas.some(l => l.id === x)) marcadas.delete(x);
    desenha();
  }
  const recarregar = throttle(carregar, 800);
  filtro.addEventListener("input", debounce(desenha, 200));
  const sitSel = sel([["todas", "todas"], ["pendentes", "pendentes"], ["executadas", "executadas sem conferência"], ["ok", "OK no SAGE"], ["falha", "falha"], ["erro", "com erro"]], filtroSit);
  sitSel.addEventListener("change", () => { filtroSit = sitSel.value; desenha(); });
  const marcarTodas = h("input", { type: "checkbox", title: "Marcar todas as linhas visíveis" });
  marcarTodas.addEventListener("change", () => {
    for (const ln of st.linhas.filter(passa)) marcarTodas.checked ? marcadas.add(ln.id) : marcadas.delete(ln.id);
    desenha();
  });

  el.append(
    h("div", { class: "card stack" },
      h("div", { class: "row between" }, h("h2", {}, "Subida de pontos"),
        h("div", { class: "row" },
          h("a", { class: "btn", href: dl(`/ieds/${id}/subida-planilha`), title: "Planilha em branco com a lista dos pontos deste IED, listas suspensas e instruções" }, "⤓ Planilha modelo"),
          h("button", { onclick: planilhaPreenchida, title: "Planilha já com os sinais do IED e estados sugeridos, para revisar e importar" }, "⤓ Planilha preenchida"),
          h("button", { onclick: () => arquivo.click(), title: "xlsx (planilha modelo) ou csv com as colunas Ponto; Estado inicial; Estado final; Observação" }, "⤒ Importar planilha"), arquivo,
          h("a", { class: "btn", href: dl(`/ieds/${id}/subida-planilha?resultado=true`), title: "A lista com o resultado de cada linha (pode ser importada de novo)" }, "⤓ Exportar resultado"))),
      h("p", { class: "muted small" }, "Para cada sinal, o simulador aplica o estado inicial, espera e aplica o final, gerando o evento que o SAGE deve mostrar. ",
        "Confira no SAGE e marque OK ou Falha em cada linha. A execução roda no servidor: pode fechar o navegador."),
      status,
      h("div", { class: "row", style: { alignItems: "flex-end" } },
        field("Ponto", inPonto), field("Estado inicial", caixaIni), field("Estado final", caixaFim), field("Observação", inObs),
        h("button", { class: "primary", onclick: adicionar }, "+ Adicionar"), dlist),
      ajuda),
    h("div", { class: "card stack" },
      h("div", { class: "row", style: { alignItems: "flex-end" } },
        field("Espera após o inicial (s)", espIni), field("Espera entre pontos (s)", espEntre), retornar.el,
        h("span", { style: { flex: "1" } }), exec)),
    h("div", { class: "row between" }, resumo,
      h("div", { class: "row" }, filtro, sitSel,
        h("button", { class: "sm", onclick: async () => { if (await confirmDlg("Apagar os resultados (execução, OK/Falha e notas) e manter a lista?", "Limpar resultados", true) && await send("DELETE", u + "?resultados=true")) carregar(); } }, "Limpar resultados"),
        h("button", { class: "sm danger", onclick: async () => { if (await confirmDlg("Apagar toda a lista da subida de pontos deste IED?", "Apagar lista", true) && await send("DELETE", u)) carregar(); } }, "Apagar lista"))),
    h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, marcarTodas), ...["#", "Ponto", "CDC", "Estado inicial", "Estado final", "Valor atual", "Execução", "Conferência no SAGE", "Ações"].map(x => h("th", {}, x)))),
      tbody)), vazio);

  carregarPontos();
  carregar();
  return {
    el,
    onEvent: m => {
      if (!st) return;
      if (m.evento === "subida") {
        if (m.dados.execucao) st.execucao = m.dados.execucao;
        if (m.dados.linha) atualizaLinha(m.dados.linha); else recarregar();
        desenhaExec();
        for (const [lid, tr] of trs) tr.classList.toggle("sel", st.execucao.atual === lid);
      } else if (m.evento === "point.changed" && m.dados.path) {
        const v = m.dados.valor;
        for (const ln of st.linhas.filter(x => x.ponto === m.dados.path)) {
          ln.valor_atual = (ln.cdc === "DPS" || ln.cdc === "DPC") ? DPS[v] ?? String(v)
            : ln.tipo === "dig" ? (v ? "1" : "0") : m.dados.enum && m.dados.enum[v] != null ? String(m.dados.enum[v]) : typeof v === "object" && v ? String(v.mag) : String(v);
          atualizaLinha(ln);
        }
      } else if (["ied.state", "session.opened", "session.closed"].includes(m.evento)) recarregar();
    },
  };
}

function subComandos(d) {
  const id = d.id;
  const el = h("div", { class: "stack" });
  const ctlBox = h("div");
  const tb = h("tbody");
  let rows = [];
  let paused = false;
  const pauseChk = chk("pausar atualização ao vivo", false);
  pauseChk.input.addEventListener("change", () => { paused = pauseChk.input.checked; });

  async function loadCtl() {
    let r;
    try { r = await api("GET", `/ieds/${id}/controles`); } catch (e) { put(ctlBox, h("div", { class: "alert bad" }, e.message)); return; }
    if (!r.controles.length) {
      put(ctlBox, h("p", { class: "muted" }, "Este CID não tem objetos controláveis (DOs com Oper, SBOw ou SBO em FC=CO). Comandos do SAGE em outros pontos são recusados e aparecem na aba Reports e histórico › Serviços MMS."));
      return;
    }
    const cmd = r.controles.filter(c => c.ctlModel_n > 0), so = r.controles.filter(c => c.ctlModel_n === 0);
    const objCell = c => h("td", {}, h("div", { class: "path" }, c.ref.split("/").slice(1).join("/")), h("div", { class: "desc" }, c.desc || ""),
      h("div", { class: "mono small muted", title: "Endereço MMS do Operate (domínio/item)" }, c.mms));
    put(ctlBox, cmd.length ? h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Objeto", "CDC", "ctlModel / tempos", "Configuração no SAGE (CGF)", "Estado atual", "Quem executa", "Bloqueios ativos", ""].map(x => h("th", {}, x)))),
      h("tbody", {}, cmd.map(c => h("tr", {},
        objCell(c),
        h("td", { class: "mono" }, c.cdc || "?"),
        h("td", { class: "small" }, h("b", {}, c.ctlModel), h("span", { class: "muted" }, ` (${c.ctlModel_n})`),
          c.ctlModel_origem !== "CID" ? h("div", { class: "desc" }, c.ctlModel_origem) : null,
          h("div", { class: "desc", title: `Atributos no CID: ${c.servicos.join(", ")}. Os outros são recusados com este ctlModel.` },
            "aceita ", (c.servicos_validos || c.servicos).join(" · ")),
          c.ctlModel_n === 3 || c.ctlModel_n === 4 ? h("div", { class: "desc", title: "Prazo para o estado chegar à posição pedida; depois disso a terminação sai negativa (Time-limit-over)" }, `operTimeout ${c.operTimeout_ms} ms`, c.operTimeout_origem !== "CID" ? ` (${c.operTimeout_origem})` : "") : null,
          c.ctlModel_n === 2 || c.ctlModel_n === 4 ? h("div", { class: "desc", title: "Tempo que a seleção fica válida esperando o Operate" }, `sboTimeout ${c.sboTimeout_ms} ms`, c.sboTimeout_origem !== "CID" ? ` (${c.sboTimeout_origem})` : "") : null,
          c.selecionado_por ? h("div", {}, h("span", { class: "badge warn" }, `selecionado por ${c.selecionado_por}`)) : null,
          c.em_execucao ? h("div", {}, h("span", { class: "badge info" }, "em execução")) : null),
        h("td", { class: "small" }, sageCell(c)),
        h("td", {}, estadoBadge(c.cdc, c.estado)),
        h("td", { class: "small" }, c.execucao === "lógica"
          ? h("a", { href: `#/ied/${id}/logicas` }, h("span", { class: "badge info" }, "lógica"))
          : h("span", { class: "badge neutral", title: "Sem lógica: o estado do próprio DO passa a valer o comando" }, "eco automático"),
          c.execucao === "eco" && c.config.tempo_ms ? h("div", { class: "desc" }, `manobra ${c.config.tempo_ms} ms`) : null),
        h("td", { class: "small" }, c.bloqueios.length ? c.bloqueios.map(b => h("div", {}, h("span", { class: "badge bad" }, `${b.direcao}: ${b.causa}`), h("span", { class: "desc" }, ` ${b.logica}`))) : h("span", { class: "muted" }, "nenhum")),
        h("td", { class: "nowrap right" },
          canOperate() ? ctlButtons(c).map(([t, v]) => h("button", { class: "sm" + (v ? " danger" : ""), title: "Comando de teste pela tela: passa pelas mesmas verificações de um comando do SAGE", onclick: () => testar(c, v, t) }, t)) : null,
          isAdmin() ? h("button", { class: "sm ghost", title: "Ajustes do eco e da terminação", onclick: () => ajustes(c) }, "⚙") : null))))))
      : h("p", { class: "muted" }, "Nenhum objeto deste CID aceita comando: todos estão com ctlModel status-only."),
      so.length ? h("details", { style: { marginTop: "12px" } },
        h("summary", { class: "small" }, `Somente estado (ctlModel status-only): ${so.length} objeto(s) — têm atributos de controle no CID, mas recusam qualquer comando`),
        h("div", { class: "tablewrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["Objeto", "CDC", "Estado atual"].map(x => h("th", {}, x)))),
          h("tbody", {}, so.map(c => h("tr", {}, objCell(c), h("td", { class: "mono" }, c.cdc || "?"), h("td", {}, estadoBadge(c.cdc, c.estado)))))))) : null);
  }

  async function testar(c, v, t) {
    const r = await send("POST", `/ieds/${id}/controles/comando`, { chave: c.chave, valor: v });
    if (!r) return;
    toast(r.resultado === "aceito" ? `${t} ${c.ref.split("/").pop()}: aceito` : `${t} recusado: ${r.causa_txt || r.causa}`, r.resultado === "aceito" ? "ok" : "bad");
    loadCtl();
  }

  async function ajustes(c) {
    const tempo = h("input", { type: "number", min: 0, value: c.config.tempo_ms });
    const tout = h("input", { type: "number", min: 0, value: c.config.timeout_ms });
    const rej = chk("Recusar comando para a posição em que já está (AddCause Position-reached)", c.config.rejeitar_mesma_posicao);
    await modal(`Ajustes de ${c.ref.split("/").pop()}`, h("div", { class: "stack" },
      h("div", { class: "form" }, field("Tempo de manobra no eco (ms)", tempo), field("Prazo da terminação (ms) se o CID não tiver operTimeout", tout)),
      rej.el,
      h("div", { class: "muted small" }, "O tempo de manobra deixa o estado em intermediário (00) antes de chegar à posição pedida. Só vale para o eco automático; com lógica, o tempo fica no bloco de saída.")), [
      { label: "Cancelar", value: null },
      { label: "Salvar", cls: "primary", click: async () => !!(await send("PATCH", `/ieds/${id}/controles/config`, { [c.chave]: { tempo_ms: +tempo.value, timeout_ms: +tout.value, rejeitar_mesma_posicao: rej.input.checked } })) }]);
    loadCtl();
  }

  const resBadge = r => r.resultado === "aceito" ? h("span", { class: "badge ok" }, "aceito")
    : r.resultado === "recusado" ? h("span", { class: "badge bad", title: r.causa_txt || "" }, "recusado") : h("span", { class: "badge neutral" }, r.resultado || "—");
  function row(r) {
    const tr = h("tr", { style: { cursor: "pointer" }, onclick: () => detalhe(r) },
      h("td", {}, tsEl(r.ts), h("div", { class: "desc mono" }, r.cliente || "")),
      h("td", {}, h("div", { class: "path" }, (r.ref || "").split("/").slice(1).join("/")), r.desc ? h("div", { class: "desc" }, r.desc) : null,
        h("div", { class: "mono small muted cmd-mms" }, r.mms || "")),
      h("td", { class: "small" }, h("b", {}, r.servico), h("div", { class: "desc" }, r.ctlModel || "")),
      h("td", { class: "small" }, r.valor_txt ? h("b", {}, r.valor_txt) : h("span", { class: "muted" }, "—")),
      h("td", { class: "small" }, r.origem ? [r.origem.orCat_txt, " ", h("span", { class: "mono muted" }, r.origem.orIdent)] : "—",
        h("div", { class: "desc" }, r.ctlNum != null ? `ctlNum ${r.ctlNum}` : "", " ",
          r.check && r.check.intertravamento ? h("span", { class: "tm" }, "interlock-check") : null, " ",
          r.check && r.check.sincronismo ? h("span", { class: "tm" }, "synchro-check") : null, r.teste ? h("span", { class: "tm warn" }, "Test") : null)),
      h("td", { class: "small" }, resBadge(r), r.causa ? h("div", { class: "desc", style: { color: "var(--bad)" } }, `${r.causa} — ${r.causa_txt || ""}`) : null,
        r.dica_sage ? h("div", { class: "desc" }, h("b", {}, "→ "), r.dica_sage) : null,
        r.detalhe ? h("div", { class: "desc" }, r.detalhe) : null,
        r.execucao ? h("div", { class: "desc" }, r.execucao === "lógica" ? `executado pela lógica ${(r.logicas || []).join(", ")}` : "eco automático") : null),
      h("td", { class: "small" }, r.terminacao ? h("span", { class: "badge " + (r.terminacao === "positiva" ? "ok" : r.terminacao === "aguardando" ? "warn" : "bad") }, r.terminacao) : h("span", { class: "muted" }, "—")));
    tr.dataset.id = r.id;
    return tr;
  }
  function detalhe(r) {
    modal(`${r.servico} ${(r.ref || "").split("/").pop()}`, h("div", { class: "stack" },
      h("dl", { class: "kv" },
        h("dt", {}, "Hora"), h("dd", {}, tsEl(r.ts)),
        h("dt", {}, "Cliente"), h("dd", { class: "mono" }, r.cliente),
        h("dt", {}, "Objeto (61850)"), h("dd", { class: "mono" }, (r.ref || "") + (r.servico === "Operate" ? ".Oper.ctlVal" : "")),
        h("dt", {}, "Endereço MMS"), h("dd", { class: "mono" }, r.mms),
        h("dt", {}, "ctlModel"), h("dd", {}, r.ctlModel),
        h("dt", {}, "Valor escrito"), h("dd", {}, r.valor_txt || "—", r.valor != null ? h("span", { class: "mono muted" }, ` (ctlVal = ${JSON.stringify(r.valor)})`) : null),
        h("dt", {}, "Origem"), h("dd", {}, r.origem ? `${r.origem.orCat_txt} (orCat ${r.origem.orCat}) · orIdent ${r.origem.orIdent || "(vazio)"}` : "—"),
        h("dt", {}, "ctlNum"), h("dd", {}, r.ctlNum ?? "—"),
        h("dt", {}, "T (hora do comando)"), h("dd", {}, r.T ? tsEl(r.T) : "—"),
        h("dt", {}, "Test / Check"), h("dd", {}, `Test=${r.teste ? "sim" : "não"} · interlock-check=${r.check?.intertravamento ? "sim" : "não"} · synchro-check=${r.check?.sincronismo ? "sim" : "não"}`),
        h("dt", {}, "Resultado"), h("dd", {}, resBadge(r), r.causa ? ` ${r.causa} — ${r.causa_txt || ""}` : "", r.detalhe ? ` · ${r.detalhe}` : ""),
        r.dica_sage ? [h("dt", {}, "Como corrigir"), h("dd", {}, r.dica_sage)] : null,
        h("dt", {}, "Terminação"), h("dd", {}, r.terminacao || "—", r.t_terminacao ? h("span", { class: "muted" }, ` em ${((r.t_terminacao - r.ts) * 1000).toFixed(0)} ms`) : null)),
      r.dados ? h("details", {}, h("summary", {}, "Dados brutos recebidos"), h("pre", { class: "json" }, JSON.stringify(r.dados, null, 1))) : null),
    [{ label: "OK", cls: "primary", value: true }]);
  }
  async function loadLog() {
    let r;
    try { r = await api("GET", `/ieds/${id}/comandos?limit=500`); } catch { return; }
    rows = r.comandos;
    put(tb, rows.length ? rows.map(row) : h("tr", {}, h("td", { colspan: 7, class: "muted" }, "Nenhum comando recebido ainda. Com o IED rodando, os comandos do SAGE (Operate, Select, SelectWithValue, Cancel) aparecem aqui na hora.")));
  }

  put(el,
    h("div", { class: "card" }, h("h2", {}, "Objetos de controle"), ctlBox,
      h("p", { class: "muted small" }, "Configuração no SAGE: KCONV do ponto de controle físico (CGF) conforme o ctlModel do CID — vazio = direto; SBO = Select por leitura; SBOw = SelectWithValue; TERM = espera o CommandTermination (segurança reforçada). O OPCOES do CGF continua livre: I/S/SI liga interlock-check/synchro-check e Ox define o orCat (padrão O2, station-control)."),
      h("p", { class: "muted small" }, "Sem lógica, o simulador faz o eco: o estado do próprio objeto (ex.: CSWI.Pos.stVal) passa a valer o comando aceito. Para ligar o comando ao disjuntor (XCBR), intertravar ou simular tempo de manobra, crie uma lógica na aba Lógicas.")),
    h("div", { class: "card" }, h("div", { class: "row between" }, h("h2", {}, "Comandos recebidos"),
      h("div", { class: "row" }, pauseChk.el, h("button", { class: "sm", onclick: loadLog }, "↻ Atualizar"),
        isAdmin() ? h("button", { class: "sm danger", onclick: async () => { if (await confirmDlg("Apagar o registro de comandos deste IED?", "Apagar", true) && await send("DELETE", `/ieds/${id}/comandos`)) loadLog(); } }, "Limpar") : null)),
      h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Hora / cliente", "Objeto / endereço MMS", "Serviço", "Valor escrito", "Origem / ctlNum / Check", "Resultado", "Terminação"].map(x => h("th", {}, x)))), tb)),
      h("p", { class: "muted small" }, "Clique numa linha para ver todos os campos do comando (origem, ctlNum, T, Test, Check e os dados brutos).")));
  loadCtl();
  loadLog();
  const reCtl = throttle(loadCtl, 500);
  return {
    el,
    onEvent: m => {
      if (m.evento === "comando") {
        reCtl();
        if (paused) return;
        const r = m.dados;
        const old = tb.querySelector(`tr[data-id="${r.id}"]`);
        if (old) old.replaceWith(row(r));
        else { if (!rows.length) put(tb); rows.unshift(r); tb.prepend(row(r)); }
      } else if (m.evento === "point.changed") reCtl();
    },
  };
}

// ---------------------------------------------------------------------------- lógicas (editor visual de blocos)
const LG_DBL = new Set(["DPS", "DPC"]), LG_DIG = new Set(["SPS", "SPC", "ACT", "ACD"]);
// Sentido do comando pelo ctlVal: DPC on = Close (1), off = Trip (0); SPC on/off.
function ctlSentido(cdc, v) {
  if (v === true) return cdc === "SPC" ? "On (1)" : "Close (1)";
  if (v === false) return cdc === "SPC" ? "Off (0)" : "Trip (0)";
  return v == null ? "—" : JSON.stringify(v);
}
const LG_GRUPO_COR = { Entradas: "lg-in", "Lógica": "lg-logic", "Tempo e memória": "lg-time", "Saídas": "lg-out" };
const LG_GRID = 10, LG_W = 180;

function subLogicas(d) {
  const id = d.id;
  const admin = isAdmin();
  const el = h("div", { class: "lg-wrap" });
  const dbg = h("div", { class: "lg-debug" });
  const split = h("div", { class: "lg-split", title: "Arraste para ajustar a altura da tabela (duplo clique: tamanho padrão)" });
  { const hh = +store("lg.dbg.h"); if (hh > 0) Object.assign(dbg.style, { height: hh + "px", maxHeight: "none" }); }
  split.addEventListener("pointerdown", e => {
    e.preventDefault();
    split.setPointerCapture(e.pointerId);
    const y0 = e.clientY, h0 = dbg.getBoundingClientRect().height;
    const max = dbg.parentElement.getBoundingClientRect().height - 120;  // o desenho fica com pelo menos ~120 px
    split.onpointermove = ev => {
      const nh = Math.max(60, Math.min(max, h0 - (ev.clientY - y0)));
      Object.assign(dbg.style, { height: nh + "px", maxHeight: "none" });
      wires();
    };
    split.onpointerup = () => { split.onpointermove = split.onpointerup = null; store("lg.dbg.h", Math.round(dbg.getBoundingClientRect().height)); };
  });
  split.addEventListener("dblclick", () => { dbg.style.height = ""; dbg.style.maxHeight = ""; store("lg.dbg.h", ""); wires(); });
  let dbgRows = [], dbgOpen = null, dbgTick = 0;
  let catalogo = {}, logicas = [], pontos = {}, controles = [], ctlByKey = {};
  let cur = null;          // lógica em edição (cópia)
  let dirty = false;
  let selB = null, selW = null;
  let live = null, liveTimer = null;

  const lista = h("div", { class: "lg-list" });
  const paleta = h("div", { class: "lg-palette" });
  const props = h("div", { class: "lg-props" });
  const bar = h("div", { class: "lg-bar" });
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "lg-wires");
  const layer = h("div", { class: "lg-layer" });
  const canvas = h("div", { class: "lg-canvas" }, svg, layer);
  const scroller = h("div", { class: "lg-scroll" }, canvas);
  const empty = h("div", { class: "lg-empty" });
  put(el, h("aside", { class: "lg-side" }, h("h3", {}, "Lógicas"), lista, h("h3", {}, "Blocos"), paleta),
    h("section", { class: "lg-main" }, bar, h("div", { class: "lg-stage" }, scroller, empty), split, dbg), h("aside", { class: "lg-right" }, props));

  // ------------------------------------------------------------ portas e rótulos (iguais ao servidor)
  const cdcOf = path => (pontos[path] || {}).cdc || "";
  function portsOf(b) {
    const p = b.p || {};
    switch (b.tipo) {
      case "entrada": { const c = cdcOf(p.ponto); return [[], LG_DBL.has(c) ? ["fechado", "aberto"] : LG_DIG.has(c) ? ["q"] : ["v"]]; }
      case "comando": {
        const o = ctlByKey[p.controle];
        if (o && !LG_DBL.has(o.cdc) && !LG_DIG.has(o.cdc)) return [[], ["rec"]];
        const sinais = p.sinais || "ambos";
        return [[], ["on", "off"].filter(x => sinais === "ambos" || sinais === x)];
      }
      case "const": return [[], ["q"]];
      case "and": case "or": case "xor": { const n = Math.max(2, Math.min(8, +p.n || 2)); return [Array.from({ length: n }, (_, i) => `in${i + 1}`), ["q"]]; }
      case "not": case "ton": case "tof": case "pulso": return [["a"], ["q"]];
      case "sr": return [["s", "r"], ["q"]];
      case "comparar": return [["v"], ["q"]];
      case "saida": { const c = cdcOf(p.ponto); return [LG_DBL.has(c) ? ["on", "off"] : LG_DIG.has(c) ? ["a"] : ["v"], []]; }
      case "bloqueio": return [["a"], []];
    }
    return [[], []];
  }
  function portLabel(b, port) {
    const p = b.p || {};
    const ctlCdc = (ctlByKey[p.controle] || {}).cdc;
    const map = {
      fechado: "fechado", aberto: "aberto", q: b.tipo === "entrada" ? "estado" : "saída", v: "valor", rec: "recebido", a: b.tipo === "bloqueio" ? "bloquear" : b.tipo === "saida" ? "valor" : "entrada",
      s: "S liga", r: "R desliga",
      on: b.tipo === "saida" ? "fechar" : ctlSentido(ctlCdc, true),
      off: b.tipo === "saida" ? "abrir" : ctlSentido(ctlCdc, false),
    };
    return map[port] || port.replace("in", "");
  }
  const shortPath = path => path ? path.split("/").slice(1).join("/").replace(/\$ST\$|\$MX\$/, ".").replace(/\$/g, ".") : "";
  function subtitle(b) {
    const p = b.p || {};
    switch (b.tipo) {
      case "entrada": case "saida": return p.ponto ? [shortPath(p.ponto), (pontos[p.ponto] || {}).desc] : ["escolha o ponto", ""];
      case "comando": case "bloqueio": {
        const o = ctlByKey[p.controle];
        const extra = b.tipo === "bloqueio" ? `${p.direcao || "ambos"} · ${p.causa || "intertravamento"}` : (o ? o.desc : "");
        return o ? [o.ref.split("/").slice(1).join("/"), extra] : ["escolha o objeto", ""];
      }
      case "const": return [String(p.valor ?? false), ""];
      case "ton": case "tof": case "pulso": return [`${p.ms || 0} ms`, ""];
      case "comparar": return [`valor ${p.op || ">"} ${p.limite ?? 0}`, ""];
      case "sr": return [`prioridade ${p.prioridade === "set" ? "S" : "R"}`, ""];
      case "and": case "or": case "xor": return [`${Math.max(2, +p.n || 2)} entradas`, ""];
    }
    return ["", ""];
  }

  // ------------------------------------------------------------ carga
  async function load(keep) {
    let r;
    try { r = await api("GET", `/ieds/${id}/logicas`); } catch (e) { put(el, h("div", { class: "alert bad" }, e.message)); return; }
    catalogo = r.blocos; logicas = r.logicas; Object.assign(pontos, r.pontos); controles = r.controles;
    ctlByKey = Object.fromEntries(controles.map(c => [c.chave, c]));
    renderPalette();
    const want = keep || (cur && cur.id) || store(`lg.${id}`);
    const lg = logicas.find(x => x.id === want) || logicas[0];
    open(lg ? JSON.parse(JSON.stringify(lg)) : null);
  }
  function renderList() {
    put(lista, logicas.map(lg => h("div", { class: "lg-item" + (cur && cur.id === lg.id ? " on" : ""), onclick: () => trocar(lg) },
      h("span", { class: "lg-dot " + (lg.ativa ? "on" : "") }), h("span", { class: "grow" }, lg.nome), h("span", { class: "muted small" }, `${lg.blocos.length} blocos`))),
    !logicas.length ? h("p", { class: "muted small" }, "Nenhuma lógica ainda.") : null,
    admin ? h("div", { class: "stack", style: { gap: "6px", marginTop: "6px" } },
      h("button", { class: "sm", onclick: () => nova() }, "+ Lógica em branco"),
      h("button", { class: "sm primary", onclick: modeloDisjuntor, title: "Comando → disjuntor (com tempo de manobra) e bloqueio de fechamento/abertura" }, "+ Disjuntor com intertravamento"),
      h("button", { class: "sm", onclick: novoCid, title: "Troca o CID do relé mantendo estas lógicas; referências que mudaram de endereço são remapeadas" }, "⤒ Subir novo CID (mantém lógicas)")) : null);
  }
  async function novoCid() {
    if (dirty && !await confirmDlg("Há alterações não salvas nesta lógica. Descartar?", "Descartar", true)) return;
    const f = await pickFile(".cid,.icd,.scd,.iid,.xml");
    if (f && await reimportCid(id, null, f)) await load();
  }
  async function trocar(lg) {
    if (dirty && !await confirmDlg("Há alterações não salvas nesta lógica. Descartar?", "Descartar", true)) return;
    open(JSON.parse(JSON.stringify(lg)));
  }
  function renderPalette() {
    const grupos = {};
    for (const [t, b] of Object.entries(catalogo)) (grupos[b.grupo] ||= []).push([t, b]);
    put(paleta, Object.entries(grupos).map(([g, items]) => [h("div", { class: "lg-pg" }, g),
      items.map(([t, b]) => h("div", { class: `lg-pi ${LG_GRUPO_COR[g] || ""}`, draggable: admin, title: b.desc,
        onclick: () => admin && addBlock(t), ondragstart: e => e.dataTransfer.setData("text/lg-tipo", t) }, b.nome))]));
  }

  // ------------------------------------------------------------ edição
  function open(lg) {
    cur = lg; dirty = false; selB = selW = null;
    if (lg) store(`lg.${id}`, lg.id || "");
    renderList(); renderBar(); draw(); renderProps(); startLive();
    dbgOpen = null; loadDbg();
  }
  function touch() { dirty = true; renderBar(); }
  function nova(lg) {
    open(lg || { id: "", nome: "Nova lógica", ativa: true, blocos: [], fios: [] });
    if (!lg) touch();
  }
  const newId = t => { let n = 1; while (cur.blocos.some(b => b.id === `${t}${n}`)) n++; return `${t}${n}`; };
  function addBlock(tipo, x, y) {
    if (!cur) nova();
    const sc = scroller;
    const b = { id: newId(tipo), tipo, x: x ?? snap(sc.scrollLeft + 60 + (cur.blocos.length % 5) * 30), y: y ?? snap(sc.scrollTop + 40 + (cur.blocos.length % 5) * 30), p: {} };
    if (["ton", "tof", "pulso"].includes(tipo)) b.p.ms = 1000;
    if (tipo === "bloqueio") Object.assign(b.p, { direcao: "fechar", causa: "intertravamento" });
    if (tipo === "comparar") Object.assign(b.p, { op: ">", limite: 0 });
    if (["and", "or", "xor"].includes(tipo)) b.p.n = 2;
    cur.blocos.push(b);
    selB = b.id; selW = null;
    touch(); draw(); renderProps();
    if (["entrada", "saida"].includes(tipo)) pickPoint(b);
  }
  const snap = v => Math.max(0, Math.round(v / LG_GRID) * LG_GRID);
  canvas.addEventListener("dragover", e => { if (admin) e.preventDefault(); });
  canvas.addEventListener("drop", e => {
    const t = e.dataTransfer.getData("text/lg-tipo");
    if (!t || !admin) return;
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    addBlock(t, snap(e.clientX - r.left - LG_W / 2), snap(e.clientY - r.top - 20));
  });

  function renderBar() {
    if (!cur) { put(bar); return; }
    const nome = h("input", { value: cur.nome, disabled: !admin, class: "lg-name", oninput: e => { cur.nome = e.target.value; touch(); } });
    const ativa = chk("ativa", cur.ativa, { disabled: !admin });
    ativa.input.addEventListener("change", () => { cur.ativa = ativa.input.checked; touch(); });
    put(bar, nome, ativa.el,
      dirty ? h("span", { class: "badge warn" }, "não salva") : cur.id ? h("span", { class: "badge ok" }, "salva") : null,
      h("span", { class: "spacer" }),
      live && cur.id && !dirty ? h("span", { class: "badge info", title: "Valores lidos do simulador a cada 0,4 s" }, "● ao vivo") : null,
      admin ? [
        h("button", { class: "primary", disabled: !dirty, onclick: salvar }, "Salvar"),
        cur.id ? h("button", { class: "sm", disabled: !dirty, onclick: () => trocar(logicas.find(x => x.id === cur.id)) }, "Descartar") : null,
        cur.id ? h("button", { class: "sm danger", onclick: apagar }, "Excluir lógica") : null] : null);
  }
  async function salvar() {
    const r = cur.id ? await send("PUT", `/ieds/${id}/logicas/${cur.id}`, cur) : await send("POST", `/ieds/${id}/logicas`, cur);
    if (!r) return;
    toast(r.avisos.length ? "Lógica salva, com avisos:\n" + r.avisos.join("\n") : "Lógica salva e em execução", r.avisos.length ? "info" : "ok", 6000);
    await load(r.logica.id);
  }
  async function apagar() {
    if (!await confirmDlg(`Excluir a lógica “${cur.nome}”? Comandos que ela tratava voltam ao eco automático.`, "Excluir", true)) return;
    if (await send("DELETE", `/ieds/${id}/logicas/${cur.id}`)) { cur = null; await load(); }
  }

  // ------------------------------------------------------------ desenho
  const nodes = new Map();  // id -> {el, ports: {in:{p:dot}, out:{p:dot}}}
  function draw() {
    put(layer); nodes.clear();
    empty.hidden = !!(cur && cur.blocos.length);
    put(empty, !cur ? h("div", {}, h("b", {}, "Nenhuma lógica aberta."), h("p", {}, admin ? "Crie uma lógica em branco ou use o modelo de disjuntor com intertravamento." : "Seu perfil só consulta."))
      : h("div", {}, h("b", {}, "Lógica vazia."), h("p", {}, "Arraste blocos da lista à esquerda para cá (ou clique neles). Ligue uma saída (bolinha à direita) a uma entrada (bolinha à esquerda) arrastando.")));
    if (!cur) { wires(); return; }
    for (const b of cur.blocos) layer.append(block(b));
    wires();
  }
  function block(b) {
    const cat = catalogo[b.tipo] || { nome: b.tipo, grupo: "" };
    const [ins, outs] = portsOf(b);
    const [s1, s2] = subtitle(b);
    const pIn = {}, pOut = {};
    const row = (port, side) => {
      const dot = h("span", { class: "lg-dot-p", "data-b": b.id, "data-p": port, "data-side": side });
      (side === "in" ? pIn : pOut)[port] = dot;
      return h("div", { class: "lg-port " + side }, side === "in" ? [dot, h("span", {}, portLabel(b, port))] : [h("span", {}, portLabel(b, port)), dot]);
    };
    const n = Math.max(ins.length, outs.length);
    const el = h("div", { class: `lg-block ${LG_GRUPO_COR[cat.grupo] || ""}${selB === b.id ? " sel" : ""}`, style: { left: b.x + "px", top: b.y + "px", width: LG_W + "px" }, "data-b": b.id },
      h("div", { class: "lg-bh" }, h("span", { class: "lg-bt" }, cat.nome), h("span", { class: "lg-bid" }, b.id)),
      h("div", { class: "lg-bs", title: s2 || s1 }, h("div", { class: "mono" }, s1), s2 ? h("div", { class: "muted" }, s2) : null),
      h("div", { class: "lg-ports" }, Array.from({ length: n }, (_, i) => h("div", { class: "lg-prow" },
        ins[i] ? row(ins[i], "in") : h("span"), outs[i] ? row(outs[i], "out") : h("span")))),
      h("div", { class: "lg-live" }));
    nodes.set(b.id, { el, pIn, pOut });
    el.addEventListener("pointerdown", e => onBlockDown(e, b));
    return el;
  }
  function portXY(bid, side, port) {
    const n = nodes.get(bid);
    const dot = n && (side === "in" ? n.pIn : n.pOut)[port];
    if (!dot) return null;
    const r = dot.getBoundingClientRect(), c = canvas.getBoundingClientRect();
    return [r.left + r.width / 2 - c.left, r.top + r.height / 2 - c.top];
  }
  const curve = ([x1, y1], [x2, y2]) => { const dx = Math.max(40, Math.abs(x2 - x1) / 2); return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`; };
  function wires() {
    svg.replaceChildren();
    const W = Math.max(2400, ...[...(cur?.blocos || [])].map(b => b.x + LG_W + 200)), H = Math.max(1400, ...[...(cur?.blocos || [])].map(b => b.y + 260));
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    svg.setAttribute("width", W); svg.setAttribute("height", H);
    if (!cur) return;
    const out = live && cur.id && !dirty ? (live.logicas[cur.id] || {}).saidas || {} : null;
    cur.fios.forEach((f, i) => {
      const a = portXY(f.de, "out", f.saida), z = portXY(f.para, "in", f.entrada);
      if (!a || !z) return;
      const v = out ? (out[f.de] || {})[f.saida] : undefined;
      const on = v === true || (typeof v === "number" && v !== 0);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", curve(a, z));
      path.setAttribute("class", "lg-wire" + (on ? " on" : "") + (selW === i ? " sel" : "") + (out && v === undefined ? "" : ""));
      path.addEventListener("pointerdown", e => { e.stopPropagation(); selW = i; selB = null; wires(); renderProps(); markSel(); });
      svg.append(path);
      if (out && typeof v === "number") {
        const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
        t.setAttribute("x", (a[0] + z[0]) / 2); t.setAttribute("y", (a[1] + z[1]) / 2 - 4); t.setAttribute("class", "lg-wtext");
        t.textContent = +v.toFixed(3);
        svg.append(t);
      }
    });
    paintLive();
  }
  function markSel() { for (const [bid, n] of nodes) n.el.classList.toggle("sel", bid === selB); }

  // ------------------------------------------------------------ arrastar blocos e ligar fios
  let drag = null;
  function onBlockDown(e, b) {
    if (e.button !== 0) return;
    const dot = e.target.closest(".lg-dot-p");
    selB = b.id; selW = null; markSel(); wires(); renderProps();
    if (!admin) return;
    if (dot) { startWire(e, dot); return; }
    if (e.target.closest("input,select,button")) return;
    e.preventDefault();
    const n = nodes.get(b.id);
    drag = { b, n, sx: e.clientX, sy: e.clientY, x0: b.x, y0: b.y, moved: false };
    n.el.setPointerCapture(e.pointerId);
    n.el.onpointermove = ev => {
      const dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
      drag.moved = true;
      b.x = snap(drag.x0 + dx); b.y = snap(drag.y0 + dy);
      n.el.style.left = b.x + "px"; n.el.style.top = b.y + "px";
      wires();
    };
    n.el.onpointerup = () => { n.el.onpointermove = n.el.onpointerup = null; if (drag.moved) touch(); drag = null; };
  }
  function startWire(e, dot) {
    e.preventDefault(); e.stopPropagation();
    const side = dot.dataset.side, bid = dot.dataset.b, port = dot.dataset.p;
    // começar numa entrada já ligada: solta o fio dela para religar
    let from;
    if (side === "in") {
      const i = cur.fios.findIndex(f => f.para === bid && f.entrada === port);
      if (i < 0) return;
      const f = cur.fios.splice(i, 1)[0];
      from = { bid: f.de, port: f.saida };
      touch(); wires();
    } else from = { bid, port };
    const temp = document.createElementNS("http://www.w3.org/2000/svg", "path");
    temp.setAttribute("class", "lg-wire temp");
    svg.append(temp);
    const a = portXY(from.bid, "out", from.port);
    const c = canvas.getBoundingClientRect();
    const move = ev => {
      const cr = canvas.getBoundingClientRect();
      temp.setAttribute("d", curve(a, [ev.clientX - cr.left, ev.clientY - cr.top]));
      const t = document.elementFromPoint(ev.clientX, ev.clientY);
      layer.querySelectorAll(".lg-dot-p.hover").forEach(x => x.classList.remove("hover"));
      if (t && t.classList.contains("lg-dot-p") && t.dataset.side === "in") t.classList.add("hover");
    };
    move({ clientX: e.clientX || a[0] + c.left, clientY: e.clientY || a[1] + c.top });
    const up = ev => {
      document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", up);
      temp.remove();
      const t = document.elementFromPoint(ev.clientX, ev.clientY);
      if (t && t.classList.contains("lg-dot-p") && t.dataset.side === "in" && t.dataset.b !== from.bid) {
        cur.fios = cur.fios.filter(f => !(f.para === t.dataset.b && f.entrada === t.dataset.p));
        cur.fios.push({ de: from.bid, saida: from.port, para: t.dataset.b, entrada: t.dataset.p });
        touch();
      }
      wires();
    };
    document.addEventListener("pointermove", move); document.addEventListener("pointerup", up);
  }
  canvas.addEventListener("pointerdown", e => {
    if (e.target === canvas || e.target === svg || e.target === layer) { selB = null; selW = null; markSel(); wires(); renderProps(); }
  });
  const onKey = e => {
    if (!admin || !cur || !el.isConnected || e.target.closest("input,select,textarea")) return;
    if (e.key === "Delete" || e.key === "Backspace") {
      if (selW != null) { cur.fios.splice(selW, 1); selW = null; touch(); wires(); renderProps(); e.preventDefault(); }
      else if (selB) { removeBlock(selB); e.preventDefault(); }
    }
  };
  document.addEventListener("keydown", onKey);
  function removeBlock(bid) {
    cur.blocos = cur.blocos.filter(b => b.id !== bid);
    cur.fios = cur.fios.filter(f => f.de !== bid && f.para !== bid);
    selB = null; touch(); draw(); renderProps();
  }

  // ------------------------------------------------------------ propriedades
  function renderProps() {
    if (!cur) { put(props, h("p", { class: "muted small" }, "—")); return; }
    if (selW != null && cur.fios[selW]) {
      const f = cur.fios[selW];
      put(props, h("h3", {}, "Fio"), h("p", { class: "mono small" }, `${f.de}.${f.saida} → ${f.para}.${f.entrada}`),
        admin ? h("button", { class: "sm danger", onclick: () => { cur.fios.splice(selW, 1); selW = null; touch(); wires(); renderProps(); } }, "Remover fio (Delete)") : null);
      return;
    }
    const b = cur.blocos.find(x => x.id === selB);
    if (!b) {
      put(props, h("h3", {}, "Como usar"), h("ul", { class: "list small lg-help" },
        h("li", {}, "Arraste um bloco da lista à esquerda para a área de desenho."),
        h("li", {}, "Ligue a bolinha de saída (direita) à de entrada (esquerda) de outro bloco."),
        h("li", {}, "Clique num fio ou bloco e aperte Delete para remover. Arraste a ponta de um fio já ligado para religar."),
        h("li", {}, "O bloco Comando recebido assume o objeto: o eco automático desliga e é a lógica que decide o que acontece com o estado."),
        h("li", {}, "O bloco Bloqueio recusa o comando (LastApplError com Blocked-by-interlocking) enquanto o sinal estiver ligado."),
        h("li", {}, "Salve para a lógica rodar no simulador; com ela salva, os fios ficam verdes quando o sinal está ligado.")),
        live && cur.id ? liveSummary() : null);
      return;
    }
    const cat = catalogo[b.tipo];
    const p = b.p;
    const set = (k, v) => { p[k] = v; touch(); const n = nodes.get(b.id); if (n) { const nb = block(b); n.el.replaceWith(nb); } cleanWires(b); wires(); markSel(); };
    const f = [];
    if (b.tipo === "entrada" || b.tipo === "saida") {
      f.push(field("Ponto", h("div", { class: "stack", style: { gap: "4px" } },
        h("div", { class: "mono small" }, p.ponto ? shortPath(p.ponto) : h("span", { class: "muted" }, "nenhum")),
        p.ponto && pontos[p.ponto] ? h("div", { class: "muted small" }, `${pontos[p.ponto].cdc} · ${pontos[p.ponto].desc || ""}`) : null,
        admin ? h("button", { class: "sm", onclick: () => pickPoint(b) }, p.ponto ? "Trocar ponto…" : "Escolher ponto…") : null)));
      if (b.tipo === "saida" && LG_DBL.has(cdcOf(p.ponto)))
        f.push(field("Tempo de manobra (ms) — fica em intermediário (00) antes da posição final", h("input", { type: "number", min: 0, value: p.tempo_ms || 0, disabled: !admin, onchange: e => set("tempo_ms", +e.target.value) })));
    }
    if (b.tipo === "comando" || b.tipo === "bloqueio")
      f.push(field("Objeto de controle", sel([["", "— escolha —"], ...controles.map(c => [c.chave, `${c.ref.split("/").slice(1).join("/")} (${c.cdc}${c.desc ? " · " + c.desc : ""})`])], p.controle || "", { disabled: !admin, onchange: e => set("controle", e.target.value) })));
    if (b.tipo === "comando" && (!ctlByKey[p.controle] || LG_DBL.has(ctlByKey[p.controle].cdc) || LG_DIG.has(ctlByKey[p.controle].cdc))) {
      const cdc = (ctlByKey[p.controle] || {}).cdc;
      f.push(field("Sinais que o SAGE manda", sel([["ambos", `os dois: ${ctlSentido(cdc, true)} e ${ctlSentido(cdc, false)}`], ["on", `só ${ctlSentido(cdc, true)} (ctlVal = true)`], ["off", `só ${ctlSentido(cdc, false)} (ctlVal = false)`]],
        p.sinais || "ambos", { disabled: !admin, onchange: e => set("sinais", e.target.value) })));
    }
    if (b.tipo === "bloqueio") {
      f.push(field("Bloqueia", sel([["fechar", "Close / On (ctlVal = true)"], ["abrir", "Trip / Off (ctlVal = false)"], ["ambos", "os dois sentidos"]], p.direcao || "ambos", { disabled: !admin, onchange: e => set("direcao", e.target.value) })));
      f.push(field("Motivo informado ao SAGE (AddCause)", sel([["intertravamento", "intertravamento (Blocked-by-interlocking)"], ["sincronismo", "sincronismo (Blocked-by-synchrocheck)"], ["processo", "processo (Blocked-by-process)"]], p.causa || "intertravamento", { disabled: !admin, onchange: e => set("causa", e.target.value) })));
      const c = chk("Só quando o SAGE pede o check (Check.interlock-check / synchro-check)", !!p.so_com_check, { disabled: !admin });
      c.input.addEventListener("change", () => set("so_com_check", c.input.checked));
      f.push(c.el);
    }
    if (["ton", "tof", "pulso"].includes(b.tipo)) f.push(field("Tempo (ms)", h("input", { type: "number", min: 0, value: p.ms || 0, disabled: !admin, onchange: e => set("ms", +e.target.value) })));
    if (["and", "or", "xor"].includes(b.tipo)) f.push(field("Entradas", sel([2, 3, 4, 5, 6, 7, 8].map(n => [n, String(n)]), p.n || 2, { disabled: !admin, onchange: e => set("n", +e.target.value) })));
    if (b.tipo === "sr") f.push(field("Se S e R juntos", sel([["reset", "desliga (R tem prioridade)"], ["set", "liga (S tem prioridade)"]], p.prioridade || "reset", { disabled: !admin, onchange: e => set("prioridade", e.target.value) })));
    if (b.tipo === "const") f.push(field("Valor", h("input", { value: String(p.valor ?? "false"), disabled: !admin, onchange: e => { const v = e.target.value.trim(); set("valor", v === "true" ? true : v === "false" ? false : isNaN(+v) ? v : +v); } })));
    if (b.tipo === "comparar") {
      f.push(field("Operação", sel([">", ">=", "<", "<=", "==", "!="].map(x => [x, x]), p.op || ">", { disabled: !admin, onchange: e => set("op", e.target.value) })));
      f.push(field("Limite", h("input", { type: "number", value: p.limite ?? 0, disabled: !admin, onchange: e => set("limite", +e.target.value) })));
    }
    const lv = live && cur.id && !dirty ? live.logicas[cur.id] : null;
    const err = lv && lv.erros[b.id];
    const ult = lv && lv.ultimo[b.id];
    put(props, h("h3", {}, cat.nome, " ", h("span", { class: "mono muted small" }, b.id)), h("p", { class: "muted small" }, cat.desc),
      h("div", { class: "stack" }, f),
      ult ? h("div", { class: "alert info small" }, "Último comando: ", h("b", {}, ctlSentido((ctlByKey[p.controle] || {}).cdc, ult.valor)), " · ", tsEl(ult.ts)) : null,
      err ? h("div", { class: "alert bad small" }, err) : null,
      admin ? h("button", { class: "sm danger", onclick: () => removeBlock(b.id) }, "Excluir bloco (Delete)") : null);
  }
  function cleanWires(b) {
    const [ins, outs] = portsOf(b);
    cur.fios = cur.fios.filter(f => (f.de !== b.id || outs.includes(f.saida)) && (f.para !== b.id || ins.includes(f.entrada)));
  }
  function liveSummary() {
    const bl = live.bloqueios || {};
    const ks = Object.keys(bl);
    return h("div", { class: "stack small" }, h("h3", {}, "Ao vivo"),
      h("div", {}, `${live.varreduras} varreduras`),
      ks.length ? ks.map(k => h("div", {}, h("span", { class: "badge bad" }, "bloqueado"), " ", (ctlByKey[k]?.ref || k).split("/").pop(), ": ", bl[k].map(x => x.direcao).join(", ")))
        : h("div", { class: "muted" }, "Nenhum comando bloqueado agora."));
  }

  // ------------------------------------------------------------ escolher ponto
  async function pickPoint(b) {
    const q = h("input", { placeholder: "Buscar por caminho ou descrição (ex.: XCBR1 Pos, MOLA, Ind3)", style: { width: "100%" } });
    const res = h("div", { class: "lg-pick" });
    const tipo = b.tipo === "saida" ? "dig,ana" : "";
    let seq = 0;
    const busca = async () => {
      const my = ++seq;
      let r;
      try { r = await api("GET", `/ieds/${id}/points?limit=200&q=${encodeURIComponent(q.value.trim())}${tipo ? "&tipo=" + tipo : ""}`); } catch { return; }
      if (my !== seq) return;
      put(res, r.pontos.length ? r.pontos.map(pt => h("div", { class: "lg-pick-i", onclick: () => {
        pontos[pt.path] = { cdc: pt.cdc, desc: pt.desc };
        b.p.ponto = pt.path; touch(); cleanWires(b); draw(); renderProps(); dlg.close(true);
      } }, h("span", { class: "mono" }, shortPath(pt.path)), h("span", { class: "tm" }, pt.cdc), h("span", { class: "muted small grow" }, pt.desc || ""),
        h("span", {}, estadoBadge(pt.cdc, pt.valor)))) : h("p", { class: "muted" }, "Nada encontrado."));
    };
    q.addEventListener("input", throttle(busca, 250));
    const dlg = modal(b.tipo === "saida" ? "Ponto que a lógica vai escrever" : "Ponto que a lógica vai ler", h("div", { class: "stack" }, q, res), [{ label: "Cancelar", value: null }], { cls: "wide" });
    busca();
    setTimeout(() => q.focus(), 50);
    await dlg;
  }

  // ------------------------------------------------------------ modelo pronto: disjuntor com intertravamento
  async function modeloDisjuntor() {
    if (!controles.length) { toast("Este CID não tem objetos de controle (Oper em FC=CO).", "bad"); return; }
    const dpc = controles.filter(c => c.cdc === "DPC");
    const ctl = sel((dpc.length ? dpc : controles).map(c => [c.chave, `${c.ref.split("/").slice(1).join("/")}${c.desc ? " · " + c.desc : ""}`]));
    const st = { path: "" }, blq = { path: "" };
    const stLbl = h("span", { class: "mono small" }, "(escolha)"), blqLbl = h("span", { class: "mono small" }, "(nenhum)");
    const pick = async (alvo, lbl, titulo) => {
      const q = h("input", { placeholder: "Buscar ponto…", style: { width: "100%" } });
      const res = h("div", { class: "lg-pick" });
      const busca = async () => {
        const r = await api("GET", `/ieds/${id}/points?limit=200&q=${encodeURIComponent(q.value.trim())}`);
        put(res, r.pontos.map(pt => h("div", { class: "lg-pick-i", onclick: () => { pontos[pt.path] = { cdc: pt.cdc, desc: pt.desc }; alvo.path = pt.path; lbl.textContent = shortPath(pt.path) + (pt.desc ? " · " + pt.desc : ""); d2.close(true); } },
          h("span", { class: "mono" }, shortPath(pt.path)), h("span", { class: "tm" }, pt.cdc), h("span", { class: "muted small grow" }, pt.desc || ""))));
      };
      q.addEventListener("input", throttle(busca, 250));
      const d2 = modal(titulo, h("div", { class: "stack" }, q, res), [{ label: "Cancelar", value: null }], { cls: "wide" });
      busca();
      await d2;
    };
    const sugerir = () => {
      const c = ctlByKey[ctl.value];
      if (!c) return;
      const dom = c.chave.split("/")[0];
      // sugere o XCBR do mesmo LD
      api("GET", `/ieds/${id}/points?limit=50&q=XCBR&cdc=DPC,DPS`).then(r => {
        const x = r.pontos.find(pt => pt.path.startsWith(dom + "/") && /\$Pos$/.test(pt.path)) || r.pontos.find(pt => /\$Pos$/.test(pt.path));
        if (x && !st.path) { pontos[x.path] = { cdc: x.cdc, desc: x.desc }; st.path = x.path; stLbl.textContent = shortPath(x.path) + (x.desc ? " · " + x.desc : ""); }
      }).catch(() => {});
    };
    ctl.addEventListener("change", sugerir);
    sugerir();
    const dir = sel([["fechar", "bloquear o FECHAMENTO"], ["abrir", "bloquear a ABERTURA"], ["ambos", "bloquear os dois"]], "fechar");
    const tempo = h("input", { type: "number", min: 0, value: 200 });
    const inv = chk("Bloquear quando o ponto estiver DESLIGADO (inverte o sinal com um NÃO)", false);
    const ok = await modal("Modelo: disjuntor com intertravamento", h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Cria: Comando recebido → saída no estado do disjuntor (com tempo de manobra) e no estado do próprio objeto de comando, mais um bloqueio ligado ao ponto de intertravamento. Depois dá para ajustar tudo no desenho."),
      field("Objeto de controle (comando do SAGE)", ctl),
      field("Estado do disjuntor (ex.: XCBR1.Pos)", h("div", { class: "row" }, stLbl, h("button", { class: "sm", onclick: () => pick(st, stLbl, "Estado do disjuntor") }, "Escolher…"))),
      field("Tempo de manobra (ms)", tempo),
      field("Ponto de intertravamento (opcional, ex.: MOLA DESCARREGADA, SF6 BLOQUEIO)", h("div", { class: "row" }, blqLbl, h("button", { class: "sm", onclick: () => pick(blq, blqLbl, "Ponto de intertravamento") }, "Escolher…"))),
      field("Intertravamento", dir), inv.el), [
      { label: "Cancelar", value: null }, { label: "Criar", cls: "primary", value: true }]);
    if (!ok) return;
    const c = ctlByKey[ctl.value];
    const cst = `${c.chave.split("$")[0]}$ST$${c.chave.split("$").slice(1).join("$")}`;
    const blocos = [{ id: "cmd", tipo: "comando", x: 30, y: 40, p: { controle: c.chave } }];
    const fios = [];
    if (st.path) {
      blocos.push({ id: "disj", tipo: "saida", x: 300, y: 20, p: { ponto: st.path, tempo_ms: +tempo.value || 0 } });
      fios.push({ de: "cmd", saida: "on", para: "disj", entrada: "on" }, { de: "cmd", saida: "off", para: "disj", entrada: "off" });
    }
    if (cst !== st.path) {
      pontos[cst] = pontos[cst] || { cdc: c.cdc, desc: c.desc };
      blocos.push({ id: "estado", tipo: "saida", x: 300, y: 170, p: { ponto: cst, tempo_ms: +tempo.value || 0 } });
      fios.push({ de: "cmd", saida: "on", para: "estado", entrada: "on" }, { de: "cmd", saida: "off", para: "estado", entrada: "off" });
    }
    if (blq.path) {
      blocos.push({ id: "intertrav", tipo: "entrada", x: 30, y: 330, p: { ponto: blq.path } });
      blocos.push({ id: "bloq", tipo: "bloqueio", x: inv.input.checked ? 480 : 300, y: 330, p: { controle: c.chave, direcao: dir.value, causa: "intertravamento" } });
      const out = LG_DBL.has(cdcOf(blq.path)) ? "fechado" : LG_DIG.has(cdcOf(blq.path)) ? "q" : "v";
      if (inv.input.checked) {
        blocos.push({ id: "nao", tipo: "not", x: 250, y: 350, p: {} });
        fios.push({ de: "intertrav", saida: out, para: "nao", entrada: "a" }, { de: "nao", saida: "q", para: "bloq", entrada: "a" });
      } else fios.push({ de: "intertrav", saida: out, para: "bloq", entrada: "a" });
    }
    nova({ id: "", nome: `Disjuntor ${c.ref.split("/").pop().replace(".Pos", "")}`, ativa: true, blocos, fios });
    touch();
  }

  // ------------------------------------------------------------ depuração: o que o SAGE mandou
  const dbgRefs = () => [...new Set((cur?.blocos || []).filter(b => ["comando", "bloqueio"].includes(b.tipo))
    .map(b => ctlByKey[b.p.controle]?.ref).filter(Boolean))];
  async function loadDbg() {
    const refs = dbgRefs();
    if (!refs.length) { dbgRows = []; renderDbg(); return; }
    let rows;
    try { rows = (await api("GET", `/ieds/${id}/comandos?limit=30&ref=${encodeURIComponent(refs.join(","))}`)).comandos; } catch { return; }
    if (JSON.stringify(rows) === JSON.stringify(dbgRows) && dbg.childNodes.length) return;
    dbgRows = rows;
    renderDbg();
  }
  function naLogica(r) {
    if (r.resultado === "recusado") return h("span", { class: "badge bad", title: "O comando foi recusado antes de chegar à lógica" }, "não chegou");
    if (r.servico === "SelectWithValue" || r.servico === "Select") return h("span", { class: "muted" }, "seleção (aguarda o Operate)");
    if (r.servico === "Cancel") return h("span", { class: "muted" }, "seleção cancelada");
    if (r.execucao === "lógica") return [h("span", { class: "badge info" }, `pulso ${ctlSentido(r.cdc, r.valor)}`), h("div", { class: "desc" }, (r.logicas || []).join(", "))];
    if (r.execucao === "eco") return h("span", { class: "badge neutral", title: "Nenhuma lógica ativa usa este objeto num bloco Comando" }, "eco automático");
    return "—";
  }
  // colunas da tabela de sinais: ordem e visibilidade ficam guardadas neste navegador
  const DBG_COLS = [
    { k: "hora", t: "Hora / cliente", td: r => [tsEl(r.ts), h("div", { class: "desc mono" }, r.cliente || "")] },
    { k: "servico", t: "Serviço", td: r => [h("b", {}, r.servico), h("div", { class: "desc" }, r.ctlModel || "")] },
    { k: "mms", t: "Endereço MMS completo", cls: "mono small", style: { wordBreak: "break-all" }, td: r => r.mms || "" },
    { k: "sinal", t: "Sinal (ctlVal)", td: r => r.valor !== undefined ? [h("b", {}, ctlSentido(r.cdc, r.valor)), h("div", { class: "desc mono" }, `ctlVal = ${JSON.stringify(r.valor)}`)] : h("span", { class: "muted" }, "—") },
    { k: "origem", t: "Origem (orCat / orIdent)", td: r => r.origem ? [r.origem.orCat_txt, ` (${r.origem.orCat})`, h("div", { class: "desc mono", style: { wordBreak: "break-all" } }, r.origem.orIdent || "(vazio)")] : "—" },
    { k: "ctlnum", t: "ctlNum", td: r => r.ctlNum ?? "—" },
    { k: "t", t: "T (hora do comando)", cls: "mono small", td: r => r.T || "—" },
    { k: "check", t: "Test / Check", td: r => h("div", { class: "desc" }, `Test ${r.teste ? "sim" : "não"} · interlock ${r.check?.intertravamento ? "sim" : "não"} · synchro ${r.check?.sincronismo ? "sim" : "não"}`) },
    { k: "resultado", t: "Resultado", td: r => [r.resultado === "aceito" ? h("span", { class: "badge ok" }, "aceito") : r.resultado === "recusado" ? h("span", { class: "badge bad" }, "recusado") : h("span", { class: "badge neutral" }, r.resultado || "—"),
      r.causa ? h("div", { class: "desc", style: { color: "var(--bad)" } }, `${r.causa} — ${r.causa_txt || ""}`) : r.causa_txt ? h("div", { class: "desc", style: { color: "var(--bad)" } }, r.causa_txt) : null,
      r.detalhe ? h("div", { class: "desc" }, r.detalhe) : null] },
    { k: "term", t: "Terminação", td: r => [r.terminacao ? h("span", { class: "badge " + (r.terminacao === "positiva" ? "ok" : r.terminacao === "aguardando" ? "warn" : "bad") }, r.terminacao) : h("span", { class: "muted" }, "—"),
      r.t_terminacao ? h("div", { class: "desc" }, `${((r.t_terminacao - r.ts) * 1000).toFixed(0)} ms`) : null] },
    { k: "logica", t: "Na lógica", td: r => naLogica(r) },
  ];
  const DBG_BY = Object.fromEntries(DBG_COLS.map(c => [c.k, c]));
  let dbgCfg = (() => { try { return JSON.parse(store("lg.dbg.cols")) || null; } catch { return null; } })();
  function dbgCols() {
    const ordem = (dbgCfg?.ordem || []).filter(k => DBG_BY[k]);
    for (const c of DBG_COLS) if (!ordem.includes(c.k)) ordem.push(c.k);  // colunas novas entram no fim
    const ocultas = new Set(dbgCfg?.ocultas || []);
    return ordem.filter(k => !ocultas.has(k)).map(k => DBG_BY[k]);
  }
  function saveCols(ordem, ocultas) {
    dbgCfg = { ordem, ocultas: [...ocultas] };
    store("lg.dbg.cols", JSON.stringify(dbgCfg));
    renderDbg();
  }
  async function escolherColunas() {
    const ordem = (dbgCfg?.ordem || []).filter(k => DBG_BY[k]);
    for (const c of DBG_COLS) if (!ordem.includes(c.k)) ordem.push(c.k);
    const ocultas = new Set(dbgCfg?.ocultas || []);
    const lista = h("div", { class: "stack" });
    const draw = () => put(lista, ordem.map((k, i) => {
      const c = chk(DBG_BY[k].t, !ocultas.has(k));
      c.input.addEventListener("change", () => { c.input.checked ? ocultas.delete(k) : ocultas.add(k); });
      return h("div", { class: "row between" }, c.el, h("span", { class: "nowrap" },
        h("button", { class: "sm ghost", disabled: i === 0, title: "Mover para a esquerda", onclick: () => { ordem.splice(i - 1, 0, ordem.splice(i, 1)[0]); draw(); } }, "↑"),
        h("button", { class: "sm ghost", disabled: i === ordem.length - 1, title: "Mover para a direita", onclick: () => { ordem.splice(i + 1, 0, ordem.splice(i, 1)[0]); draw(); } }, "↓")));
    }));
    draw();
    const r = await modal("Colunas da tabela de sinais", h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Marque as colunas que quer ver e ajuste a ordem. Também dá para arrastar o título de uma coluna na tabela."), lista), [
      { label: "Restaurar padrão", value: "padrao" }, { label: "Cancelar", value: null }, { label: "Aplicar", cls: "primary", value: "ok" }]);
    if (r === "padrao") { dbgCfg = null; store("lg.dbg.cols", ""); renderDbg(); }
    else if (r === "ok") saveCols(ordem, ocultas);
  }
  let dbgDrag = null;  // coluna sendo arrastada pelo título (pausa a atualização ao vivo)
  function dropCol(alvo) {
    const de = dbgDrag; dbgDrag = null;
    if (!de || de === alvo) { renderDbg(); return; }
    const ordem = (dbgCfg?.ordem || []).filter(k => DBG_BY[k]);
    for (const c of DBG_COLS) if (!ordem.includes(c.k)) ordem.push(c.k);
    ordem.splice(ordem.indexOf(alvo), 0, ordem.splice(ordem.indexOf(de), 1)[0]);
    saveCols(ordem, new Set(dbgCfg?.ocultas || []));
  }
  function renderDbg() {
    if (dbgDrag) return;
    const refs = dbgRefs();
    const cols = dbgCols();
    const linhas = [];
    for (const r of dbgRows) {
      const aberto = dbgOpen === r.id;
      linhas.push(h("tr", { style: { cursor: "pointer" }, class: aberto ? "sel" : "", onclick: () => { dbgOpen = aberto ? null : r.id; renderDbg(); } },
        cols.map(c => h("td", { class: c.cls || "small", style: c.style || {} }, c.td(r)))));
      if (aberto) linhas.push(h("tr", {}, h("td", { colspan: cols.length },
        r.dica_sage ? h("div", { class: "alert info small" }, h("b", {}, "Como corrigir: "), r.dica_sage) : null,
        h("dl", { class: "kv small" },
          h("dt", {}, "Objeto (61850)"), h("dd", { class: "mono" }, r.ref || ""),
          h("dt", {}, "Endereço MMS"), h("dd", { class: "mono" }, r.mms || "")),
        r.dados ? h("pre", { class: "json" }, JSON.stringify(r.dados, null, 1)) : h("span", { class: "muted small" }, "sem dados brutos"))));
    }
    const th = c => h("th", { draggable: true, title: "Arraste para mudar a coluna de lugar", style: { cursor: "grab" },
      ondragstart: e => { dbgDrag = c.k; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", c.k); },
      ondragover: e => { if (dbgDrag) { e.preventDefault(); e.currentTarget.classList.add("drop"); } },
      ondragleave: e => e.currentTarget.classList.remove("drop"),
      ondrop: e => { e.preventDefault(); dropCol(c.k); },
      ondragend: () => { if (dbgDrag) { dbgDrag = null; renderDbg(); } } }, c.t);
    put(dbg, h("div", { class: "row between" }, h("h3", {}, "Sinais recebidos pelos objetos desta lógica"),
        h("div", { class: "row" },
          h("button", { class: "sm", onclick: escolherColunas, title: "Mostrar, ocultar e ordenar colunas" }, "☰ Colunas"),
          h("button", { class: "sm", onclick: loadDbg }, "↻ Atualizar"))),
      !refs.length ? h("p", { class: "muted small" }, "Coloque um bloco Comando recebido (ou Bloqueio) para ver aqui o que o SAGE manda para o objeto.")
        : h("div", { class: "tablewrap" }, h("table", {},
          h("thead", {}, h("tr", {}, cols.map(th))),
          h("tbody", {}, linhas.length ? linhas : h("tr", {}, h("td", { colspan: cols.length, class: "muted" }, `Nenhum comando recebido ainda para ${refs.map(x => x.split("/").pop()).join(", ")}.`))))),
      refs.length ? h("p", { class: "muted small" }, "Mostra todos os serviços (Select, SelectWithValue, Operate, Cancel), inclusive os recusados antes de chegar à lógica. Clique numa linha para ver os dados brutos e como corrigir; arraste a barra acima da tabela para mudar a altura.") : null);
  }

  // ------------------------------------------------------------ ao vivo
  function paintLive() {
    const lv = live && cur && cur.id && !dirty ? live.logicas[cur.id] : null;
    for (const [bid, n] of nodes) {
      const o = lv ? lv.saidas[bid] || {} : {};
      for (const [port, dot] of Object.entries(n.pOut)) {
        const v = o[port];
        dot.classList.toggle("on", v === true || (typeof v === "number" && v !== 0));
        dot.title = v === undefined ? "" : String(v);
      }
      for (const [port, dot] of Object.entries(n.pIn)) {
        const f = cur.fios.find(x => x.para === bid && x.entrada === port);
        const v = f && lv ? (lv.saidas[f.de] || {})[f.saida] : undefined;
        dot.classList.toggle("on", v === true || (typeof v === "number" && v !== 0));
        dot.classList.toggle("linked", !!f);
      }
      const box = n.el.querySelector(".lg-live");
      const b = cur.blocos.find(x => x.id === bid);
      const txt = [];
      if (lv && b) {
        if (b.tipo === "bloqueio") txt.push(o.ativo ? h("span", { class: "badge bad" }, "BLOQUEANDO") : h("span", { class: "badge ok" }, "liberado"));
        if (b.tipo === "comando" && lv.ultimo[bid]) txt.push(h("span", { class: "badge info" }, `último: ${ctlSentido((ctlByKey[b.p.controle] || {}).cdc, lv.ultimo[bid].valor)}`));
        if (lv.erros[bid]) txt.push(h("span", { class: "badge bad", title: lv.erros[bid] }, "erro"));
      }
      put(box, txt);
      n.el.classList.toggle("err", !!(lv && lv.erros[bid]));
    }
  }
  function startLive() {
    clearInterval(liveTimer);
    live = null;
    if (!cur || !cur.id) return;
    const tick = async () => {
      if (!el.isConnected) { clearInterval(liveTimer); return; }
      try { live = await api("GET", `/ieds/${id}/logicas/estado`); } catch { return; }
      if (!dirty) { wires(); renderBar(); if (selB == null && selW == null) renderProps(); }
      if (++dbgTick % 3 === 0) loadDbg();
    };
    tick();
    liveTimer = setInterval(tick, 400);
  }

  load();
  return {
    el,
    destroy: () => { clearInterval(liveTimer); document.removeEventListener("keydown", onKey); },
  };
}

// ---------------------------------------------------------------------------- RCBs e DataSets
const TRG = ["dchg", "qchg", "dupd", "period", "gi"];
const OPT = ["seqNum", "timeStamp", "dataSet", "reasonCode", "dataRef", "bufOvfl", "entryID", "configRef"];
const POLITICAS = [["livre", "livre — aceita as escritas"], ["ocupado", "ocupado — erro 2 (DatSet e RptEna: erro 3)"],
  ["negado", "negado — erro 3 em tudo"], ["somente_leitura", "somente leitura — erro 3 e nunca habilita"]];
const POL_BADGE = { livre: "ok", ocupado: "warn", negado: "bad", somente_leitura: "bad" };
const RCB_ATTRS = {
  BRCB: ["RptID", "RptEna", "DatSet", "OptFlds", "BufTm", "TrgOps", "IntgPd", "GI", "PurgeBuf", "EntryID", "ResvTms"],
  URCB: ["Resv", "RptID", "RptEna", "DatSet", "OptFlds", "BufTm", "TrgOps", "IntgPd", "GI"],
};

function subRcbs(d) {
  const id = d.id;
  const el = h("div", { class: "stack" });
  const rcbRows = new Map();
  const selected = new Set();  // nomes-base selecionados para a configuração em lote
  let allBox = null, selInfo = null, bulkBtns = [];
  const inst = dom => dom.startsWith(d.nome_ied) ? dom.slice(d.nome_ied.length) : dom;
  let dss = [];

  async function derrubar(x) {
    const r = await send("POST", `/ieds/${id}/rcbs/${encodeURIComponent(x.nome)}/disable-client`);
    if (r) toast(r.derrubados.length ? `Habilitação derrubada: ${r.derrubados.map(y => y.cliente).join(", ")}` : "Nenhum cliente habilitado", "ok");
  }

  function syncSel() {
    for (const row of rcbRows.values()) if (row.box) row.box.checked = selected.has(row.state.base);
    const bases = new Set([...rcbRows.values()].map(row => row.state.base));
    if (allBox) {
      allBox.checked = bases.size > 0 && [...bases].every(b => selected.has(b));
      allBox.indeterminate = selected.size > 0 && !allBox.checked;
    }
    if (selInfo) selInfo.textContent = selected.size ? `${selected.size} RCB(s) selecionado(s): ${[...selected].join(", ")}` : "Nenhum RCB selecionado";
    bulkBtns.forEach(b => { b.disabled = !selected.size; });
  }

  function selectWhere(pred) {
    for (const row of rcbRows.values()) if (pred(row.state)) selected.add(row.state.base);
    syncSel();
  }

  async function applyBulk(change, derrubarChk) {
    if (!selected.size) return;
    const names = [...selected];
    const what = change.restaurar_perfil ? "voltar à política do perfil" : `política "${change.politica.replace("_", " ")}"`;
    const drop = derrubarChk.checked;
    if (!await confirmDlg(`Aplicar ${what} em ${names.length} RCB(s)?\n\n${names.join("\n")}` +
      (drop ? "\n\nAs habilitações atuais desses RCBs serão derrubadas." : ""), "Aplicar", drop)) return;
    const r = await send("PATCH", `/ieds/${id}/rcbs/policy`, { rcbs: names, ...change, derrubar: drop });
    if (r) { toast(`Política aplicada em ${r.rcbs.length} RCB(s)`, "ok"); load(); }
  }

  function rcbRow(r) {
    const tr = h("tr");
    let cur = r, box = null;
    const polCell = x => {
      const p = x.politica || {};
      const ov = Object.entries(p.atributos || {});
      return h("td", { class: "small", style: { minWidth: "150px" } },
        h("span", { class: "badge " + (POL_BADGE[x.estado_politica] || "neutral") }, (x.estado_politica || "livre").replace("_", " ")),
        p.ocultar ? h("span", { class: "badge neutral", style: { marginLeft: "4px" }, title: "fora do GetNameList" }, "oculto") : null,
        p.ips && p.ips.length ? h("div", { class: "desc", title: "IPs tratados como livre" }, `IPs: ${p.ips.join(", ")}`) : null,
        ov.length ? h("div", { class: "desc mono" }, ov.map(([k, v]) => `${k}=${v}`).join(" ")) : null,
        h("div", { class: "desc" }, `${x.recusadas || 0} escrita(s) recusada(s)`),
        h("div", { class: "row", style: { gap: "4px", marginTop: "4px" } },
          h("button", { class: "sm", onclick: () => policyForm(cur) }, "política…"),
          x.RptEna || x.reservado_por ? h("button", { class: "sm danger", title: "desabilita o RCB que o cliente habilitou", onclick: () => derrubar(cur) }, "derrubar") : null));
    };
    const fill = x => put(tr,
      h("td", { class: "sel" }, box = h("input", { type: "checkbox", checked: selected.has(x.base), title: `selecionar ${x.base} (todas as instâncias)`,
        onchange: e => { if (e.target.checked) selected.add(x.base); else selected.delete(x.base); syncSel(); } })),
      h("td", {}, h("div", { class: "path nowrap", title: x.ref }, x.nome), h("div", { class: "desc" }, `${x.ln}$${x.tipo === "BRCB" ? "BR" : "RP"} · ${inst(x.ld)}`),
        // a coluna Política fica no fim da tabela, fora da tela em monitores menores: o acesso também fica aqui
        h("button", { class: "sm", style: { marginTop: "4px" }, title: "política de acesso e resposta por atributo (GI, DatSet…)", onclick: () => policyForm(cur) },
          "política: ", (x.estado_politica || "livre").replace("_", " "))),
      h("td", {}, h("span", { class: "badge " + (x.tipo === "BRCB" ? "info" : "neutral") }, x.tipo), x.extra ? h("span", { class: "badge warn", style: { marginLeft: "4px" } }, "extra") : null),
      h("td", {}, x.RptEna ? h("span", { class: "badge ok" }, "habilitado") : h("span", { class: "muted" }, "não")),
      h("td", { class: "mono small" }, x.cliente || "—", x.reservado_por && x.reservado_por !== x.cliente ? h("div", { class: "desc" }, `reserva: ${x.reservado_por}`) : x.reservado_por ? h("div", { class: "desc" }, "reservado") : null),
      h("td", { class: "mono small" }, x.RptID, x.RptID_cid && x.RptID !== x.RptID_cid ? h("div", { class: "desc" }, `CID: ${x.RptID_cid}`) : null),
      h("td", { class: "mono small" }, x.DatSet.split("/").pop(), x.dataset_ok ? null : h("span", { class: "badge bad" }, "inexistente")),
      h("td", { class: "mono right" }, x.ConfRev),
      h("td", { class: "small", style: { minWidth: "110px" } }, x.TrgOps_efetivo.join(" "), x.TrgOps.join() !== x.TrgOps_efetivo.join() ? h("div", { class: "desc", title: "o perfil limita os gatilhos" }, `pedido: ${x.TrgOps.join(" ")}`) : null),
      h("td", { class: "small", style: { minWidth: "240px" } }, x.OptFlds.join(" ")),
      h("td", { class: "mono small" }, `${x.BufTm} / ${x.IntgPd}`),
      h("td", { class: "mono small" }, x.tipo === "BRCB" ? x.EntryID : "—", x.buffer != null ? h("div", { class: "desc" }, `buffer ${x.buffer}`) : null),
      h("td", { class: "mono right" }, `${x.SqNum} / ${x.reports_enviados}`),
      polCell(x));
    fill(r);
    return {
      tr, get state() { return cur; }, get box() { return box; },
      update: x => { cur = x; fill(x); tr.classList.remove("flash"); void tr.offsetWidth; tr.classList.add("flash"); },
    };
  }

  async function policyForm(x) {
    let p;
    try { p = await api("GET", `/ieds/${id}/rcbs/${encodeURIComponent(x.base)}/policy`); } catch (e) { toast(e.message, "bad"); return; }
    const orig = k => p.origem[k] === "padrao" ? "padrão" : p.origem[k] === "perfil" ? `perfil ${p.perfil}` : "IED";
    const pol = sel(POLITICAS, p.politica);
    const ips = h("input", { class: "mono", value: p.ips.join(", "), placeholder: "vazio = todos os clientes" });
    const ocultar = chk("ocultar (some do GetNameList e do GetVariableAccessAttributes)", p.ocultar);
    const inst = sel([["", "regra de indexação do perfil"], ["true", "com sufixo (01, 02, …)"], ["false", "sem sufixo (como o i15)"]],
      p.instanciar == null ? "" : String(p.instanciar));
    const attrs = [...new Set([...(RCB_ATTRS[x.tipo] || RCB_ATTRS.URCB), ...Object.keys(p.atributos)])];
    const selAttr = Object.fromEntries(attrs.map(a => [a, sel([["", "— política"], ["ok", "ok"], ["erro2", "erro 2"], ["erro3", "erro 3"]], p.atributos[a] || "")]));
    const hist = h("div", { class: "muted small" }, "carregando…");
    api("GET", `/ieds/${id}/rcb-denied?${new URLSearchParams({ rcb: x.base, limit: 20 })}`).then(r => put(hist,
      r.recusas.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Hora", "RCB", "Atributo", "Código", "Cliente"].map(c => h("th", {}, c)))),
        h("tbody", {}, r.recusas.map(y => h("tr", {}, h("td", { class: "small" }, new Date(y.ts * 1000).toLocaleTimeString()),
          h("td", { class: "mono small" }, y.rcb.split("$").pop()), h("td", { class: "mono small" }, y.atributo),
          h("td", { class: "mono small" }, y.codigo), h("td", { class: "mono small" }, y.cliente))))) : "Nenhuma escrita recusada.")).catch(() => put(hist, "—"));
    const body = () => ({
      politica: pol.value, ips: ips.value, ocultar: ocultar.input.checked,
      instanciar: inst.value === "" ? null : inst.value === "true",
      atributos: Object.fromEntries(attrs.filter(a => selAttr[a].value).map(a => [a, selAttr[a].value])),
    });
    const save = async extra => {
      const r = await send("PATCH", `/ieds/${id}/rcbs/${encodeURIComponent(x.base)}/policy`, { ...body(), ...extra });
      if (r) { toast(`Política de ${x.base}: ${r.politica}`, "ok"); load(); }
      return r || false;
    };
    await modal(`Política de acesso — ${x.base}`, h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Decide como o simulador responde às escritas MMS neste RCB (todas as instâncias). ",
        "A resposta é por item do Write. Muda com o IED rodando e vale para as próximas escritas; 'instanciar' altera o modelo e reinicia o IED."),
      h("div", { class: "form" },
        field(`Política (${orig("politica")})`, pol, "wide"),
        field(`IPs permitidos, tratados como livre (${orig("ips")})`, ips, "wide"),
        field(`Instâncias (${orig("instanciar")})`, inst)),
      ocultar.el,
      h("h3", {}, `Resposta por atributo (${orig("atributos")}) — prevalece sobre a política`),
      h("div", { class: "form" }, attrs.map(a => field(a, selAttr[a]))),
      h("h3", {}, "Instâncias"),
      h("ul", { class: "list mono small" }, p.instancias.map(i => h("li", {}, `${i.nome}: ${i.habilitado ? `habilitado por ${i.cliente}` : "não habilitado"}, ${i.recusadas} recusa(s)`))),
      h("details", {}, h("summary", {}, "Últimas escritas recusadas"), hist)), [
      { label: "Cancelar" },
      { label: "Voltar ao perfil", click: () => save({ restaurar_perfil: true, politica: undefined, ips: undefined, ocultar: undefined, instanciar: undefined, atributos: undefined }) },
      { label: "Salvar e derrubar habilitação", cls: "danger", click: () => save({ derrubar: true }) },
      { label: "Salvar", cls: "primary", click: () => save({}) }]);
  }

  async function load() {
    let r, s;
    try { [r, s] = await Promise.all([api("GET", `/ieds/${id}/rcbs`), api("GET", `/ieds/${id}/datasets`)]); } catch { return; }
    dss = s.datasets;
    rcbRows.clear();
    const bases = new Set(r.rcbs.map(x => x.base));
    for (const b of [...selected]) if (!bases.has(b)) selected.delete(b);
    const tb = h("tbody", {}, r.rcbs.map(x => { const row = rcbRow(x); rcbRows.set(x.ref, row); return row.tr; }));
    allBox = h("input", { type: "checkbox", title: "selecionar todos", onchange: e => { bases.forEach(b => e.target.checked ? selected.add(b) : selected.delete(b)); syncSel(); } });
    selInfo = h("div", { class: "small", style: { flex: "1 1 100%" } });
    const bulkPol = sel(POLITICAS, "livre");
    const bulkDrop = chk("derrubar habilitação atual", false);
    bulkBtns = [
      h("button", { class: "primary sm", onclick: () => applyBulk({ politica: bulkPol.value }, bulkDrop.input) }, "Aplicar política"),
      h("button", { class: "sm", title: "apaga a sobrescrita do IED e volta ao que o perfil define", onclick: () => applyBulk({ restaurar_perfil: true }, bulkDrop.input) }, "Voltar ao perfil"),
      h("button", { class: "sm ghost", onclick: () => { selected.clear(); syncSel(); } }, "Limpar seleção")];
    const bulkBar = h("div", { class: "row bulkbar" },
      h("span", { class: "small muted" }, "Selecionar:"),
      h("button", { class: "sm ghost", onclick: () => selectWhere(() => true) }, "todos"),
      h("button", { class: "sm ghost", onclick: () => selectWhere(x => x.tipo === "BRCB") }, "BRCB"),
      h("button", { class: "sm ghost", onclick: () => selectWhere(x => x.tipo === "URCB") }, "URCB"),
      h("button", { class: "sm ghost", onclick: () => selectWhere(x => x.estado_politica === "livre") }, "livres"),
      h("button", { class: "sm ghost", onclick: () => selectWhere(x => x.estado_politica !== "livre") }, "não livres"),
      h("span", { class: "spacer" }), h("span", { class: "small muted" }, "Em lote:"), bulkPol, bulkDrop.el, ...bulkBtns, selInfo);
    put(el, 
      h("div", { class: "row between" }, h("h2", { style: { margin: 0 } }, `RCBs (${r.rcbs.length} instâncias)`),
        h("div", { class: "row" }, h("button", { onclick: () => rcbForm() }, "+ RCB extra de teste"), h("a", { class: "btn", href: dl(`/ieds/${id}/export-cid`) }, "⤓ CID com extras"))),
      h("p", { class: "muted small" }, "Para 3 SCADAs em paralelo, cada um precisa de uma instância (…01, …02, …03), conforme RptEnabled max do CID (RN-RCB-04). ",
        "Perfil: RCBs em ", h("b", {}, d.perfil_efetivo.rcb.posicao), "; atributos ", Object.entries(d.perfil_efetivo.rcb.atributos).map(([k, v]) => `${k}=${v}`).join(", "), "."),
      bulkBar,
      h("div", { class: "tablewrap" }, resizable(h("table", {}, h("thead", {}, h("tr", {}, h("th", { class: "sel" }, allBox), ["RCB", "Tipo", "RptEna", "Cliente", "RptID", "DatSet", "ConfRev", "TrgOps", "OptFlds", "BufTm / IntgPd", "EntryID", "SqNum / enviados", "Política"].map(x => h("th", {}, x)))), tb), "rcbs")),
      r.extras.length ? h("div", { class: "card" }, h("h3", { style: { marginTop: 0 } }, "RCBs extras"),
        h("ul", { class: "list" }, r.extras.map(x => h("li", {}, h("span", { class: "mono" }, `${x.ld}/${x.ln}.${x.name}`), ` → ${x.datset} (${x.buffered ? "BRCB" : "URCB"}, ${x.max} inst., ConfRev ${x.conf_rev}) `,
          h("button", { class: "sm danger", onclick: async () => { if (await confirmDlg(`Remover o RCB extra ${x.name}?`, "Remover", true) && await send("DELETE", `/ieds/${id}/rcbs/${x.ld}/${x.ln || "LLN0"}/${x.name}`)) load(); } }, "remover"))))) : null,
      h("div", { class: "row between", style: { marginTop: "10px" } }, h("h2", { style: { margin: 0 } }, `DataSets (${dss.length}${s.max ? ` de ${s.max}` : ""})`),
        h("button", { onclick: () => dsForm() }, "+ DataSet extra de teste")),
      h("div", { class: "tablewrap" }, resizable(h("table", {}, h("thead", {}, h("tr", {}, ["DataSet", "Origem", "Membros (ordem do CID)", "RCBs"].map(x => h("th", {}, x)))),
        h("tbody", {}, dss.map(x => h("tr", {},
          h("td", {}, h("div", { class: "path" }, x.ref.split("/").pop()), h("div", { class: "desc" }, x.desc || x.ld)),
          h("td", {}, x.dinamico ? h("span", { class: "badge warn" }, "dinâmico (MMS)") : x.extra ? h("span", { class: "badge warn" }, "extra") : h("span", { class: "badge neutral" }, "CID"),
            x.extra && !x.dinamico ? h("div", {}, h("button", { class: "sm danger", style: { marginTop: "4px" }, onclick: async () => { if (await confirmDlg(`Remover o DataSet extra ${x.nome}?`, "Remover", true) && await send("DELETE", `/ieds/${id}/datasets/${inst(x.ld)}/${x.ln}/${x.nome}`)) load(); } }, "remover")) : null),
          h("td", {}, h("details", {}, h("summary", {}, `${x.membros.length} membro(s)`),
            h("ol", { start: 0, class: "list mono" }, x.membros.map(m => h("li", {}, `${m.ref.split("/").pop()} [${m.fc}]`))))),
          h("td", { class: "mono small" }, x.rcbs.map(r2 => r2.split("$").pop()).join(", ") || "—"))))), "datasets")));
    syncSel();
  }

  async function dsForm() {
    const ld = sel(d.ld, d.ld[0]);
    const ln = h("input", { value: "LLN0", class: "mono" });
    const nome = h("input", { value: "DS_ESTADOS_SAGE", class: "mono" });
    const desc = h("input", { value: "extra de teste" });
    const members = [];
    const list = h("ol", { start: 0, class: "list mono" });
    const search = h("input", { placeholder: "buscar ponto para acrescentar" });
    const results = h("div", { class: "stack", style: { maxHeight: "180px", overflow: "auto" } });
    const renderList = () => put(list, ...members.map((m, i) => h("li", {}, m.split("/").pop(), " ",
      h("button", { class: "sm ghost", disabled: i === 0, onclick: () => { [members[i - 1], members[i]] = [members[i], members[i - 1]]; renderList(); } }, "↑"),
      h("button", { class: "sm ghost", onclick: () => { members.splice(i, 1); renderList(); } }, "✕"))));
    const doSearch = debounce(async () => {
      const r = await api("GET", `/ieds/${id}/points?${new URLSearchParams({ q: search.value, limit: 30, ld: ld.value })}`);
      put(results, ...r.pontos.map(p => h("div", { class: "row" }, h("button", { class: "sm", onclick: () => { if (!members.includes(p.path)) members.push(p.path); renderList(); } }, "+"),
        h("span", { class: "mono small" }, p.path.split("/").pop()), h("span", { class: "muted small" }, `${p.cdc} ${p.desc || ""}`))));
    }, 200);
    search.addEventListener("input", doSearch);
    ld.addEventListener("change", doSearch);
    doSearch();
    await modal("DataSet extra de teste (RN-CID-06)", h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Fica marcado como extra e pode ser exportado num CID novo. Membros na ordem da lista; alterar um DataSet em uso incrementa o ConfRev (RN-RCB-13)."),
      h("div", { class: "form" }, field("LD", ld), field("LN", ln), field("Nome", nome), field("Descrição", desc)),
      h("h3", {}, "Membros"), list, search, results), [
      { label: "Cancelar" },
      {
        label: "Criar", cls: "primary", click: async () => {
          const r = await send("POST", `/ieds/${id}/datasets`, { ld: ld.value, ln: ln.value, nome: nome.value, desc: desc.value, membros: members });
          if (r) { toast("DataSet criado", "ok"); load(); }
          return r || false;
        },
      }]);
  }

  async function rcbForm() {
    const ld = sel(d.ld, d.ld[0]);
    const ln = h("input", { value: "LLN0", class: "mono" });
    const nome = h("input", { value: "urcbESTADOS_SAGE", class: "mono" });
    const dsSel = h("select");
    const fillDs = () => put(dsSel, ...dss.filter(x => x.ld === ld.value && x.ln === ln.value).map(x => h("option", { value: x.nome }, x.nome)));
    ld.addEventListener("change", fillDs);
    ln.addEventListener("input", fillDs);
    fillDs();
    const tipo = sel([["0", "URCB (RP)"], ["1", "BRCB (BR)"]], "0");
    const rptid = h("input", { class: "mono", placeholder: "vazio = referência do RCB" });
    const buf = h("input", { type: "number", class: "num", value: 0 });
    const intg = h("input", { type: "number", class: "num", value: 0 });
    const max = h("input", { type: "number", class: "num", value: 3, min: 1, max: 99 });
    const conf = h("input", { type: "number", class: "num", value: 1 });
    const trg = TRG.map(t => chk(t, ["dchg", "qchg", "gi"].includes(t)));
    const opt = OPT.map(t => chk(t, ["seqNum", "timeStamp", "dataSet", "reasonCode", "entryID", "configRef"].includes(t)));
    await modal("RCB extra de teste (RN-CID-06)", h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Por exemplo URCB_ESTADOS_SAGE1/2/3 enquanto o CID atualizado do i15 não chega. Instâncias: …01 a …N."),
      h("div", { class: "form" }, field("LD", ld), field("LN", ln), field("Nome base", nome), field("DataSet (mesmo LN)", dsSel), field("Tipo", tipo),
        field("RptID", rptid), field("BufTm (ms)", buf), field("IntgPd (ms)", intg), field("Instâncias (RptEnabled max)", max), field("ConfRev", conf)),
      h("h3", {}, "TrgOps"), h("div", { class: "row" }, trg.map(x => x.el)),
      h("h3", {}, "OptFlds"), h("div", { class: "row" }, opt.map(x => x.el))), [
      { label: "Cancelar" },
      {
        label: "Criar", cls: "primary", click: async () => {
          const r = await send("POST", `/ieds/${id}/rcbs`, {
            ld: ld.value, ln: ln.value, nome: nome.value, datset: dsSel.value, buffered: tipo.value === "1", rptid: rptid.value,
            buf_time: +buf.value, intg_pd: +intg.value, max: +max.value, conf_rev: +conf.value,
            trgops: Object.fromEntries(TRG.map((t, i) => [t, trg[i].input.checked])), optflds: Object.fromEntries(OPT.map((t, i) => [t, opt[i].input.checked])),
          });
          if (r) { toast("RCB criado", "ok"); load(); }
          return r || false;
        },
      }]);
  }

  load();
  return {
    el,
    onEvent: m => {
      if (m.evento === "rcb.changed") { const r = rcbRows.get(m.dados.ref); if (r) r.update(m.dados); }
      if (m.evento === "rcb.write_denied") { const r = rcbRows.get(m.dados.rcb); if (r) r.update({ ...r.state, recusadas: m.dados.recusadas }); }
      if (m.evento === "ied.state") load();
    },
  };
}

// ---------------------------------------------------------------------------- reports e histórico
function hexDump(hex) {
  const lines = [];
  for (let i = 0; i < hex.length; i += 32) {
    const chunk = hex.slice(i, i + 32);
    lines.push(pad((i / 2).toString(16), 4) + "  " + chunk.match(/../g).join(" "));
  }
  return lines.join("\n");
}

function subReports(d) {
  const id = d.id;
  let view = store("rview") || "reports";
  let paused = false;
  let pduCache = new Map();
  const inner = h("div", { class: "tabs", style: { marginTop: 0 } });
  const tools = h("div", { class: "row" });
  const body = h("div");
  const el = h("div", { class: "stack" }, inner, tools, body);
  let tbody = null;
  const filt = h("input", { placeholder: "filtrar RCB / ponto", style: { width: "220px" } });
  const pauseChk = chk("pausar atualização ao vivo", false);
  pauseChk.input.addEventListener("change", () => { paused = pauseChk.input.checked; });

  function tabsR() {
    put(inner, ...[["reports", "Reports enviados"], ["mudancas", "Mudanças de ponto"], ["mms", "Serviços MMS"]].map(([k, l]) =>
      h("a", { href: "javascript:void 0", class: k === view ? "on" : "", onclick: () => { view = k; store("rview", k); render(); } }, l)));
  }

  function reportRow(r) {
    const tr = h("tr", { style: { cursor: "pointer" }, class: r.descartado ? "muted" : "" });
    const pts = (r.pontos || []).map(p => `${p.ref.split("/").pop()} = ${fmtVal(Array.isArray(p.valor) ? p.valor[0] : p.valor)} (${p.motivo.join(",")})`);
    tr.append(h("td", {}, tsEl(r.ts)), h("td", { class: "mono small" }, (r.rcb || "").split("/").pop()), h("td", { class: "mono small" }, r.rptid),
      h("td", {}, h("span", { class: "badge " + ({ evento: "info", gi: "warn", integridade: "neutral" }[r.tipo] || "neutral") }, r.tipo)),
      h("td", { class: "mono right" }, r.sqnum), h("td", { class: "mono small" }, r.entry_id || "—"), h("td", { class: "mono small" }, r.cliente),
      h("td", { class: "small" }, h("div", { class: "mono" }, pts.slice(0, 4).join("\n").split("\n").map(x => h("div", {}, x))), pts.length > 4 ? h("div", { class: "desc" }, `+${pts.length - 4}`) : null),
      h("td", { class: "small" }, r.descartado ? h("span", { class: "badge bad" }, "não enviado") : null, (r.notas || []).map(n => h("div", { class: "desc" }, n))));
    let open = null;
    tr.addEventListener("click", async () => {
      if (open) { open.remove(); open = null; return; }
      let pdu = r.pdu || pduCache.get(r.id);
      if (!pdu) {
        const rows = (await api("GET", `/ieds/${id}/reports?limit=2000`)).reports;
        for (const x of rows) pduCache.set(x.id, x.pdu);
        pdu = pduCache.get(r.id) || "";
      }
      open = h("tr", {}, h("td", { colspan: 9 }, h("div", { class: "small muted" }, `PDU MMS (${pdu.length / 2} bytes, informationReport a3 …). Para conferir com Wireshark exporte o PCAP.`), h("pre", { class: "hex" }, hexDump(pdu))));
      tr.after(open);
    });
    return tr;
  }

  async function render() {
    tabsR();
    const lim = 500;
    if (view === "reports") {
      put(tools, filt, pauseChk.el, h("span", { class: "spacer" }),
        h("a", { class: "btn", href: dl(`/ieds/${id}/reports?format=csv&limit=100000`) }, "⤓ CSV"),
        h("a", { class: "btn", href: dl(`/ieds/${id}/reports?format=pcap`) }, "⤓ PCAP sintético"),
        h("button", { class: "danger", onclick: clearLogs }, "Limpar histórico"));
      const rows = (await api("GET", `/ieds/${id}/reports?limit=${lim}${filt.value ? "&rcb=" + encodeURIComponent(filt.value) : ""}`)).reports;
      pduCache = new Map(rows.map(r => [r.id, r.pdu]));
      tbody = h("tbody", {}, rows.map(reportRow));
      put(body, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Hora", "RCB", "RptID", "Tipo", "SqNum", "EntryID", "Cliente", "Pontos (valor, motivo)", "Notas"].map(x => h("th", {}, x)))), tbody)),
        rows.length ? null : h("div", { class: "empty" }, "Nenhum report enviado ainda."));
    } else if (view === "mudancas") {
      put(tools, filt, pauseChk.el, h("span", { class: "spacer" }), h("a", { class: "btn", href: dl(`/ieds/${id}/history?format=csv&limit=100000`) }, "⤓ CSV"));
      const rows = (await api("GET", `/ieds/${id}/history?limit=${lim}${filt.value ? "&ponto=" + encodeURIComponent(filt.value) : ""}`)).mudancas;
      tbody = h("tbody", {}, rows.map(changeRow));
      put(body, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Hora", "Ponto", "Valor", "Qualidade", "t", "Origem", "Reports"].map(x => h("th", {}, x)))), tbody)),
        rows.length ? null : h("div", { class: "empty" }, "Nenhuma mudança registrada."));
    } else {
      put(tools, pauseChk.el);
      const rows = (await api("GET", `/ieds/${id}/mms-log?limit=${lim}`)).eventos;
      tbody = h("tbody", {}, rows.map(mmsRow));
      put(body, h("p", { class: "muted small" }, "Inclui a sequência do SAGE na associação (leitura dos DataSets, escrita de RptID, habilitação dos RCBs — RN-SAGE-01) e as integridades por GetDataValues (RN-SAGE-02)."),
        h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Hora", "Cliente", "Serviço", "Detalhe", "Resultado"].map(x => h("th", {}, x)))), tbody)));
    }
  }
  function changeRow(c) {
    return h("tr", {}, h("td", {}, tsEl(c.ts)), h("td", {}, h("div", { class: "path" }, (c.path || "").split("/").slice(1).join("/"))),
      h("td", { class: "mono" }, fmtVal(c.valor)), h("td", {}, qualBadge(c.qualidade)), h("td", {}, tsEl(c.t)),
      h("td", { class: "small" }, c.origem), h("td", { class: "mono small" }, (c.reports || []).join(", ") || "—"));
  }
  function mmsRow(m) {
    const bad = /erro|recus|reject|falha/i.test(m.resultado || "");
    return h("tr", {}, h("td", {}, tsEl(m.ts)), h("td", { class: "mono small" }, m.cliente), h("td", {}, m.servico),
      h("td", { class: "mono small", style: { wordBreak: "break-all" } }, m.detalhe), h("td", {}, h("span", { class: "badge " + (bad ? "bad" : "ok") }, m.resultado)));
  }
  async function clearLogs() {
    if (!await confirmDlg("Apagar reports, mudanças e registro MMS deste IED?", "Apagar", true)) return;
    if (await send("DELETE", `/ieds/${id}/logs`)) render();
  }
  const prepend = tr => {
    if (!tbody) return;
    tbody.prepend(tr);
    tr.classList.add("flash");
    while (tbody.children.length > 500) tbody.lastElementChild.remove();
    const empty = body.querySelector(".empty");
    if (empty) empty.remove();
  };
  filt.addEventListener("input", debounce(render, 300));
  render().catch(e => put(body, h("div", { class: "alert bad" }, e.message)));
  return {
    el,
    onEvent: m => {
      if (paused) return;
      const f = filt.value.toLowerCase();
      if (view === "reports" && m.evento === "report.sent" && (!f || (m.dados.rcb || "").toLowerCase().includes(f))) prepend(reportRow(m.dados));
      if (view === "mudancas" && m.evento === "point.changed" && m.dados.mudanca && (!f || m.dados.path.toLowerCase().includes(f))) {
        prepend(changeRow({ ts: Date.now() / 1000 + S.skew / 1000, path: m.dados.path, valor: m.dados.valor, qualidade: m.dados.qualidade, t: m.dados.t, origem: m.dados.origem, reports: [] }));
      }
      if (view === "mms" && m.evento === "mms") prepend(mmsRow(m.dados));
    },
  };
}

// ---------------------------------------------------------------------------- falhas (RN-FLT-01 a RN-FLT-08)
function subFalhas(d) {
  const id = d.id;
  const el = h("div", { class: "stack" });
  async function load() {
    const r = await api("GET", `/ieds/${id}/faults`);
    put(el, 
      h("p", { class: "muted small" }, "Ligadas e desligadas por IED. Toda falha ativa aparece no topo da tela e é anotada nos reports e no registro MMS, para não ser confundida com comportamento normal."),
      h("div", { class: "grid c3" }, Object.entries(r.falhas).map(([k, f]) => {
        const params = Object.entries(f).filter(([pk]) => !["regra", "titulo", "ativo"].includes(pk));
        const inputs = {};
        const on = chk("ativa", f.ativo);
        const paramEls = params.map(([pk, pv]) => {
          if (typeof pv === "boolean") {
            const c = chk(pk.replace(/_/g, " "), pv);
            inputs[pk] = c.input;
            return h("div", { class: "wide" }, c.el);
          }
          const i = h("input", { value: pv, type: typeof pv === "number" ? "number" : "text", class: typeof pv === "number" ? "num" : "mono" });
          inputs[pk] = i;
          return field(pk.replace(/_/g, " "), i);
        });
        return h("div", { class: "card", style: f.ativo ? { borderColor: "var(--bad)" } : {} },
          h("div", { class: "row between" }, h("b", {}, f.regra), f.ativo ? h("span", { class: "badge bad" }, "ATIVA") : h("span", { class: "badge neutral" }, "desligada")),
          h("div", { style: { margin: "6px 0 10px" } }, f.titulo),
          params.length ? h("div", { class: "form" }, paramEls) : null,
          k === "erro_escrita" ? h("p", { class: "muted small" }, "código 2 = temporarily-unavailable, 3 = object-access-denied; rcb vazio = todos") : null,
          k === "descarte" ? h("p", { class: "muted small" }, "ponto: descarta os reports que contêm esse ponto (vazio = um a cada N)") : null,
          k === "atraso" ? h("p", { class: "muted small" }, "fora de ordem: segura um report e entrega depois do seguinte") : null,
          h("div", { class: "row", style: { marginTop: "10px" } }, on.el, h("span", { class: "spacer" }), h("button", {
            class: "primary sm", onclick: async () => {
              const v = { ativo: on.input.checked };
              for (const [pk, i] of Object.entries(inputs)) v[pk] = i.type === "checkbox" ? i.checked : i.type === "number" ? +i.value : i.value;
              if (await send("PUT", `/ieds/${id}/faults`, { [k]: v })) { toast(`${f.regra}: ${v.ativo ? "ativada" : "desligada"}`, v.ativo ? "bad" : "ok"); load(); }
            },
          }, "Aplicar")));
      })));
  }
  load().catch(e => put(el, h("div", { class: "alert bad" }, e.message)));
  return { el };
}

// ---------------------------------------------------------------------------- rede
function pageRede() {
  const corpo = h("div");
  const el = h("div", { class: "stack" }, h("h1", {}, "Rede da máquina"));
  const NIVEL = { ok: "ok", info: "info", warn: "warn", bad: "bad" };
  const TIPO = { syn: "TCP SYN", arp: "ARP", ping: "ping", tcp: "TCP aceito", assoc: "associação", recusa: "recusa", fim: "encerrada", dominio: "LD errado" };
  const filtro = h("input", { placeholder: "filtrar por IP, IED ou texto", style: { minWidth: "220px" } });
  let ult = "", timer = null;
  el.append(h("div", { class: "card" }, h("div", { class: "row", style: { justifyContent: "space-between", alignItems: "center" } },
    h("h2", {}, "Quem está tentando conectar"),
    h("div", { class: "row" }, filtro,
      h("button", { class: "sm", onclick: () => { ult = ""; carregar(); } }, "↻ Atualizar"),
      h("button", { class: "sm", onclick: async () => { if (await send("DELETE", "/net/monitor")) { ult = ""; carregar(); } } }, "Limpar"))),
  corpo));
  const hora = ts => new Date(ts * 1000).toLocaleTimeString();
  const passa = (...xs) => { const f = filtro.value.trim().toLowerCase(); return !f || xs.join(" ").toLowerCase().includes(f); };

  function diagEscuta(e, d) {
    if (!e.ip) return ["warn", "IED sem IP: escolha o IP no painel do IED"];
    if (!e.ip_existe) return ["bad", `o IP ${e.ip} não existe nesta máquina: crie o IP abaixo`];
    if (e.estado !== "rodando") return ["warn", `IED ${e.estado}: a máquina recusa conexões em ${e.ip}:${e.porta}`];
    if (e.clientes.length) return ["ok", `associado: ${e.clientes.map(c => c.cliente).join(", ")}`];
    if (e.syns) return ["warn", `${e.syns} tentativa(s) de ${e.origens.join(", ")}, sem associação ativa: veja as tentativas e os eventos abaixo`];
    if (!d.ativo) return ["neutral", "sem captura de pacotes: só aparecem as conexões que o IED aceita"];
    return ["warn", `nenhum pedido de conexão chegou a ${e.ip}:${e.porta} desde ${hora(d.desde)}. O SAGE não está tentando este endereço: confira o host_mms, o PORTA= do CNF.CONFIG e se a ligação (LSC) está habilitada no SAGE`];
  }

  async function carregar() {
    let d;
    try { d = await api("GET", "/net/monitor"); } catch (e) { put(corpo, h("div", { class: "alert bad" }, e.message)); return; }
    const j = JSON.stringify(d) + filtro.value;
    if (j === ult) return;
    ult = j;
    const estado = d.ativo
      ? h("p", { class: "small" }, h("span", { class: "badge ok" }, "captura ativa"),
        ` desde ${hora(d.desde)}: pedidos ARP, ping e TCP SYN para as portas ${d.portas.join(", ")}, inclusive para IP ou porta sem IED.`)
      : h("div", { class: "alert warn" }, h("b", {}, "Captura de pacotes desligada: "), d.erro || "?",
        ". Sem ela só aparecem as conexões que um IED aceitou; tentativas para IP ou porta errada não aparecem.",
        d.como_ativar ? h("div", { class: "mono small", style: { marginTop: "6px" } }, d.como_ativar) : null);
    const escutas = h("table", {}, h("thead", {}, h("tr", {}, ["IED", "Endereço", "Estado", "Pedidos de conexão", "Diagnóstico"].map(x => h("th", {}, x)))),
      h("tbody", {}, d.escutas.filter(e => passa(e.ied, e.ip, e.porta)).map(e => {
        const [n, txt] = diagEscuta(e, d);
        return h("tr", {},
          h("td", {}, h("a", { href: `#/ied/${e.ied_id}/painel` }, h("b", {}, e.ied))),
          h("td", { class: "mono nowrap" }, `${e.ip || "—"}:${e.porta}`, e.ip_cid ? h("div", { class: "desc", title: "IP do relé real no CID" }, `real: ${e.ip_cid}`) : null),
          h("td", {}, stateBadge(e.estado)),
          h("td", { class: "small" }, e.syns ? [`${e.syns} SYN`, h("div", { class: "desc" }, `último ${hora(e.ultimo_syn)} · ${e.origens.join(", ")}`)] : h("span", { class: "muted" }, "nenhum")),
          h("td", {}, h("span", { class: "badge " + (NIVEL[n] || "neutral") }, { ok: "ok", warn: "atenção", bad: "problema", neutral: "—" }[n]), " ", h("span", { class: "small" }, txt)));
      })));
    const tent = d.tentativas.filter(t => passa(t.origem, t.destino, t.porta, t.ied, t.situacao));
    const tentativas = tent.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Última", "Tipo", "Origem", "Destino", "Interface", "Vezes", "Situação"].map(x => h("th", {}, x)))),
      h("tbody", {}, tent.map(t => h("tr", {},
        h("td", {}, tsEl(t.ultima)), h("td", { class: "nowrap" }, TIPO[t.tipo] || t.tipo),
        h("td", { class: "mono small" }, t.origem), h("td", { class: "mono small nowrap" }, t.porta ? `${t.destino}:${t.porta}` : t.destino),
        h("td", { class: "small" }, t.interface),
        h("td", { class: "num", title: t.repeticoes ? "retransmissões do mesmo SYN: o cliente não recebeu resposta" : "" }, String(t.vezes), t.repeticoes ? h("div", { class: "desc" }, `${t.repeticoes} retransm.`) : null),
        h("td", {}, h("span", { class: "badge " + (NIVEL[t.nivel] || "neutral") }, t.nivel), " ", h("span", { class: "small" }, t.situacao))))))
      : h("div", { class: "empty" }, d.ativo ? "Nenhum pedido capturado ainda." : "Captura desligada.");
    const ev = d.eventos.filter(e => passa(e.origem, e.destino, e.ied, e.texto)).slice(0, 200);
    const eventos = ev.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Hora", "Tipo", "Origem", "Destino", "IED", "O que aconteceu"].map(x => h("th", {}, x)))),
      h("tbody", {}, ev.map(e => h("tr", {},
        h("td", {}, tsEl(e.ts)), h("td", {}, h("span", { class: "badge " + (NIVEL[e.nivel] || "neutral") }, TIPO[e.tipo] || e.tipo)),
        h("td", { class: "mono small" }, e.origem), h("td", { class: "mono small" }, e.destino),
        h("td", { class: "small" }, e.ied_id ? h("a", { href: `#/ied/${e.ied_id}/reports` }, e.ied) : "—"),
        h("td", { class: "small", style: { wordBreak: "break-word" } }, e.texto)))))
      : h("div", { class: "empty" }, "Nenhum evento ainda.");
    put(corpo, estado,
      h("h3", {}, "IEDs e portas"), h("div", { class: "tablewrap" }, escutas),
      h("h3", {}, "Pedidos que chegaram à máquina"),
      h("p", { class: "muted small" }, "Agrupados por origem e destino. Retransmissões do mesmo SYN indicam que o cliente não recebeu resposta."),
      h("div", { class: "tablewrap", style: { maxHeight: "320px" } }, tentativas),
      h("h3", {}, "Eventos recentes"),
      h("p", { class: "muted small" }, "TCP aceito, associação, recusa, conexão fechada antes de associar e LD pedido que não é deste IED. O detalhe MMS de cada IED fica em Reports › Registro MMS."),
      h("div", { class: "tablewrap", style: { maxHeight: "420px" } }, eventos));
  }
  const recarregar = throttle(carregar, 1000);
  filtro.addEventListener("input", debounce(() => { ult = ""; carregar(); }, 250));
  carregar();
  timer = setInterval(carregar, 5000);
  (async () => {
    const picker = await ipPicker("", { uid: "rede" });
    const iface = h("select");
    const ip = h("input", { class: "mono", placeholder: "10.237.68.200" });
    const pre = h("input", { type: "number", class: "num", value: 24 });
    const out = h("div");
    try {
      const d = await api("GET", "/net/interfaces");
      put(iface, ...d.interfaces.filter(i => !i.loopback).map(i => h("option", { value: i.nome }, `${i.nome} (${i.estado})`)));
    } catch { /* ignore */ }
    el.append(h("div", { class: "card" }, h("h2", {}, "Interfaces (RN-NET-12)"), picker.el),
      h("div", { class: "card" }, h("h2", {}, "Criar IP secundário (RN-NET-02)"),
        h("p", { class: "muted small" }, "Um IP por IED, todos na porta 102. A aplicação recusa um IP que já responda na rede, para não conflitar com o relé real (RN-NET-03). ",
          "Sem permissão (root ou CAP_NET_ADMIN), mostra o comando para criar o IP manualmente."),
        h("div", { class: "row" }, field("Interface", iface), field("IP", ip), field("Prefixo", pre),
          h("button", {
            class: "primary", style: { alignSelf: "flex-end" }, onclick: async () => {
              put(out, h("span", { class: "muted" }, "verificando se o IP responde na rede…"));
              try {
                const r = await api("POST", "/net/alias", { interface: iface.value, ip: ip.value.trim(), prefixo: +pre.value });
                put(out, h("div", { class: "alert info" }, r.mensagem));
                picker.reload();
              } catch (e) { put(out, h("div", { class: "alert bad", style: { whiteSpace: "pre-wrap" } }, e.message)); }
            },
          }, "Criar")), out));
  })();
  return {
    el,
    onEvent: m => { if (m.evento === "rede.monitor" || m.evento.startsWith("session.") || m.evento === "ied.state") recarregar(); },
    destroy: () => clearInterval(timer),
  };
}

// ---------------------------------------------------------------------------- perfis (seção 9)
function pagePerfis() {
  const el = h("div", { class: "stack" });
  async function load() {
    const ps = await loadProfiles();
    put(el, h("h1", {}, "Perfis de IED"),
      h("p", { class: "muted small" }, "Um perfil agrupa as particularidades de um modelo de relé que o CID sozinho não descreve. Os fornecidos são somente leitura: crie uma cópia para alterar (RN-PRF-03). ",
        "Alterar o perfil de um IED parado vale na próxima partida; rodando, pede confirmação e derruba as associações (RN-PRF-04)."),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Perfil", "Versão", "Origem", "Edição", "Descrição", "Em uso por", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, ps.map(p => h("tr", {},
          h("td", {}, h("b", { class: "mono" }, p.id), h("div", { class: "desc" }, p.nome || "")),
          h("td", { class: "mono" }, p.versao),
          h("td", {}, p.fornecido ? h("span", { class: "badge neutral" }, "fornecido") : h("span", { class: "badge info" }, "usuário"), p.copiado_de ? h("div", { class: "desc" }, `cópia de ${p.copiado_de}`) : null),
          h("td", {}, String(p.edicao ?? "cid")),
          h("td", { class: "small", style: { maxWidth: "420px" } }, p.descricao || ""),
          h("td", { class: "small" }, S.ieds.filter(i => i.perfil === p.id).map(i => i.nome).join(", ") || "—"),
          h("td", { class: "nowrap right" },
            h("button", { class: "sm", onclick: () => modal(`Perfil ${p.id}`, h("pre", { class: "json" }, JSON.stringify(p, null, 2)), [{ label: "Fechar" }]) }, "Ver"), " ",
            h("button", {
              class: "sm", onclick: async () => {
                const r = await promptDlg(`Copiar ${p.id}`, [{ k: "id", label: "Id do novo perfil", value: `${p.id}-copia` }, { k: "nome", label: "Nome", value: `Cópia de ${p.nome || p.id}` }]);
                if (r && await send("POST", `/profiles/${encodeURIComponent(p.id)}/copy`, { novo_id: r.id, nome: r.nome })) { toast("Perfil copiado", "ok"); load(); }
              },
            }, "Copiar"), " ",
            p.fornecido ? null : h("button", { class: "sm", onclick: () => editProfile(p) }, "Editar"), " ",
            p.fornecido ? null : h("button", {
              class: "sm danger", onclick: async () => {
                if (await confirmDlg(`Excluir o perfil ${p.id}?`, "Excluir", true) && await send("DELETE", `/profiles/${encodeURIComponent(p.id)}`)) load();
              },
            }, "Excluir"))))))));
  }
  async function editProfile(p) {
    const clean = { ...p };
    delete clean.fornecido;
    const ta = h("textarea", { rows: 24 });
    ta.value = JSON.stringify(clean, null, 2);
    await modal(`Editar perfil ${p.id}`, h("div", { class: "stack" }, h("p", { class: "muted small" }, "JSON do perfil. Cada gravação incrementa a versão (RN-PRF-01). Valores \"cid\" são lidos do Services do CID (RN-CID-10)."), ta), [
      { label: "Cancelar" },
      {
        label: "Salvar", cls: "primary", click: async () => {
          let obj;
          try { obj = JSON.parse(ta.value); } catch (e) { toast("JSON inválido: " + e.message, "bad"); return false; }
          const r = await send("PUT", `/profiles/${encodeURIComponent(p.id)}`, obj);
          if (r) { toast(`Perfil salvo (versão ${r.versao})`, "ok"); load(); }
          return r || false;
        },
      }]);
  }
  load().catch(e => put(el, h("div", { class: "alert bad" }, e.message)));
  return { el };
}

// ---------------------------------------------------------------------------- cenários
function pageCenarios() {
  const el = h("div", { class: "stack" });
  const iedSel = h("select");
  async function load() {
    const [sc, ieds] = await Promise.all([api("GET", "/scenarios"), api("GET", "/ieds")]);
    S.ieds = ieds.ieds;
    const prev = iedSel.value || store("cen.ied");
    put(iedSel, ...S.ieds.filter(i => i.ied_scl).map(i => h("option", { value: i.id, selected: String(i.id) === prev }, `${i.nome} (${i.estado})`)));
    iedSel.onchange = () => store("cen.ied", iedSel.value);
    put(el, 
      h("div", { class: "row between" }, h("h1", {}, "Cenários"),
        h("div", { class: "row" }, h("button", { onclick: () => edit(null) }, "+ Novo cenário"), h("button", { onclick: fromIed }, "+ A partir do estado de um IED"))),
      h("p", { class: "muted small" }, "Conjuntos de valores digitais e analógicos e sequências com atraso entre passos. Passos sem atraso entram na mesma operação (e no mesmo report)."),
      h("div", { class: "row" }, field("Aplicar no IED", iedSel)),
      sc.cenarios.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Cenário", "Passos", "Duração", "Origem", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, sc.cenarios.map(s => h("tr", {},
          h("td", {}, h("b", {}, s.nome)), h("td", { class: "right" }, (s.passos || []).length),
          h("td", { class: "mono" }, `${(s.passos || []).reduce((a, p) => a + (+p.atraso_ms || 0), 0)} ms`),
          h("td", { class: "small" }, s.ied_origem || "—"),
          h("td", { class: "nowrap right" },
            h("button", {
              class: "sm primary", onclick: async () => {
                if (!iedSel.value) return toast("Escolha o IED", "bad");
                const r = await send("POST", `/scenarios/${s.id}/apply?ied=${iedSel.value}`);
                if (r) toast(`Aplicando “${s.nome}”: ${r.passos} passo(s) em ${r.grupos} grupo(s)`, "ok");
              },
            }, "▶ Aplicar"), " ",
            h("button", { class: "sm", onclick: () => edit(s) }, "Editar"), " ",
            h("button", { class: "sm danger", onclick: async () => { if (await confirmDlg(`Excluir o cenário “${s.nome}”?`, "Excluir", true) && await send("DELETE", `/scenarios/${s.id}`)) load(); } }, "Excluir"))))))) :
        h("div", { class: "empty card" }, "Nenhum cenário. Crie um vazio ou salve o estado atual de um IED."));
  }
  async function fromIed() {
    if (!iedSel.value) return toast("Nenhum IED", "bad");
    const r = await promptDlg("Cenário a partir do estado do IED", [{ k: "nome", label: "Nome", value: "estado " + new Date().toLocaleString("pt-BR") }]);
    if (r && await send("POST", "/scenarios", { de_ied: +iedSel.value, nome: r.nome })) load();
  }
  async function edit(s) {
    const nome = h("input", { value: s?.nome || "" });
    const steps = (s?.passos || []).map(p => ({ ...p }));
    const tb = h("tbody");
    const renderSteps = () => put(tb, ...steps.map((p, i) => {
      const ponto = h("input", { value: p.ponto || "", class: "mono", style: { width: "100%" }, oninput: e => { p.ponto = e.target.value; } });
      const val = h("input", { value: p.valor == null ? "" : JSON.stringify(p.valor), class: "mono", style: { width: "120px" }, oninput: e => { p.valor = parseVal(e.target.value); } });
      const q = sel([["", "—"], "good", "invalid", "questionable"], p.qualidade?.validity || "", { onchange: e => { p.qualidade = e.target.value ? { validity: e.target.value } : undefined; } });
      const at = h("input", { type: "number", value: p.atraso_ms || 0, class: "num", style: { width: "90px" }, oninput: e => { p.atraso_ms = +e.target.value; } });
      return h("tr", {}, h("td", { class: "mono muted" }, i + 1), h("td", {}, ponto), h("td", {}, val), h("td", {}, q), h("td", {}, at),
        h("td", {}, h("button", { class: "sm ghost", onclick: () => { steps.splice(i, 1); renderSteps(); } }, "✕")));
    }));
    renderSteps();
    await modal(s ? `Editar cenário` : "Novo cenário", h("div", { class: "stack" }, field("Nome", nome),
      h("p", { class: "muted small" }, "Valor em JSON: true/false, número, {\"mag\":1,\"ang\":0} para CMV. Atraso em ms antes do passo; ponto por caminho (ex.: sAutGGIO2$ST$Ind3)."),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["#", "Ponto", "Valor", "Qualidade", "Atraso (ms)", ""].map(x => h("th", {}, x)))), tb)),
      h("button", { onclick: () => { steps.push({ ponto: "", valor: true, atraso_ms: 0 }); renderSteps(); } }, "+ Passo")), [
      { label: "Cancelar" },
      {
        label: "Salvar", cls: "primary", click: async () => {
          const body = { ...(s || {}), nome: nome.value, passos: steps.map(p => { const x = { ponto: p.ponto, valor: p.valor, atraso_ms: +p.atraso_ms || 0 }; if (p.qualidade) x.qualidade = p.qualidade; return x; }) };
          delete body.id;
          const r = s ? await send("PUT", `/scenarios/${s.id}`, body) : await send("POST", "/scenarios", body);
          if (r) load();
          return r || false;
        },
      }]);
  }
  load().catch(e => put(el, h("div", { class: "alert bad" }, e.message)));
  return { el, onEvent: m => { if (m.evento === "scenario.done") toast("Cenário concluído", "ok"); if (m.evento === "scenario.error") toast(`Cenário: ${m.dados.erro}`, "bad"); } };
}

// ---------------------------------------------------------------------------- ensaios (seção 13)
const ENSAIOS = {
  T1: ["Importar o CID do i15 e iniciar o IED no IP da máquina", "SAGE físico associa, lê os DataSets e mostra os pontos do AQS.lst com valor atual"],
  T2: ["Ligar e desligar sAutGGIO2$ST$Ind3 (MOLA) com perfil ingeteam-ef-i15, URCB e OPMSK 10C830", "Evento no .alr e no .sde do SAGE, ou falha reproduzida com o report registrado"],
  T3: ["Repetir T2 com perfil referencia-UCDQSA1 e RCB por LN", "Evento registrado no SAGE; se funcionar, a diferença entre perfis é a causa"],
  T4: ["Três SCADAs associados ao mesmo IED", "Cada um habilita seu RCB e recebe o mesmo report; erro de reserva só quando exceder o máximo"],
  T5: ["Corrigir o relógio do IED (deslocamento 0 contra 3 h atrasado) e repetir T2", "Diferença no resultado do SAGE entre relógio correto e atrasado"],
  T6: ["Ponto em dois DataSets (N2 e SAGE)", "Mostra qual DataSet o SAGE usa para o ponto"],
  T7: ["Variação dos bits 5, 9, 10, 11, 14 e 20 do OPMSK", "Tabela de resultados por combinação, gravada em histórico"],
  T8: ["Espelhar um ponto do relé real e acioná-lo no campo", "O simulador replica valor e t e o SAGE mostra o mesmo evento nos dois"],
  T9: ["Falhas RN-FLT-01 a RN-FLT-08, uma por vez", "SAGE reage conforme esperado (reconexão, alarme de comunicação, evento perdido) e o registro mostra a causa"],
  T10: ["Dois IEDs com CIDs diferentes em IPs distintos", "Os dois operam ao mesmo tempo; parar um não afeta o outro"],
};
const ENS_STATUS = [["", "—"], ["aprovado", "Aprovado"], ["falha_reproduzida", "Falha reproduzida"], ["reprovado", "Reprovado"], ["inconclusivo", "Inconclusivo"]];

function pageEnsaios() {
  const el = h("div", { class: "stack" });
  async function load() {
    const [r, ieds] = await Promise.all([api("GET", "/ensaios"), api("GET", "/ieds")]);
    S.ieds = ieds.ieds;
    const rows = r.ensaios.sort((a, b) => (b.registrado_em || 0) - (a.registrado_em || 0));
    put(el, 
      h("div", { class: "row between" }, h("h1", {}, "Ensaios (seção 13)"), h("button", { class: "primary", onclick: () => edit(null) }, "+ Registrar ensaio")),
      h("div", { class: "alert warn" }, "Testes com o SAGE físico: eventos e alarmes injetados aparecem para o operador e entram nos arquivos .alr e .sde reais. ",
        "Use IED e pontos reservados para teste, combinados antes com a COT."),
      rows.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Quando", "Teste", "IED / perfil", "OPMSK", "Resultado", "Situação", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, rows.map(e => h("tr", {},
          h("td", {}, tsEl(e.registrado_em)), h("td", {}, h("b", {}, e.teste), h("div", { class: "desc" }, ENSAIOS[e.teste]?.[0] || "")),
          h("td", { class: "small" }, e.ied || "—", h("div", { class: "desc mono" }, e.perfil || "")), h("td", { class: "mono" }, e.opmsk || "—"),
          h("td", { class: "small", style: { whiteSpace: "pre-wrap", maxWidth: "420px" } }, e.resultado || ""),
          h("td", {}, e.status ? h("span", { class: "badge " + ({ aprovado: "ok", falha_reproduzida: "warn", reprovado: "bad" }[e.status] || "neutral") }, ENS_STATUS.find(x => x[0] === e.status)?.[1] || e.status) : "—"),
          h("td", { class: "nowrap right" }, h("button", { class: "sm", onclick: () => edit(e) }, "Editar"), " ",
            h("button", { class: "sm danger", onclick: async () => { if (await confirmDlg("Excluir este registro?", "Excluir", true) && await send("DELETE", `/ensaios/${e.id}`)) load(); } }, "Excluir"))))))) :
        h("div", { class: "empty card" }, "Nenhum ensaio registrado."),
      h("div", { class: "card" }, h("h2", {}, "OPMSK (T7)"), opmskWidget()));
  }
  async function edit(e) {
    const teste = sel(Object.keys(ENSAIOS).map(k => [k, `${k} — ${ENSAIOS[k][0]}`]), e?.teste || "T2");
    const esperado = h("div", { class: "alert info" });
    const fillEsp = () => { esperado.textContent = "Esperado: " + ENSAIOS[teste.value][1]; };
    teste.addEventListener("change", fillEsp);
    fillEsp();
    const ied = sel([["", "—"], ...S.ieds.map(i => [i.nome, i.nome])], e?.ied || "");
    const perfil = h("input", { value: e?.perfil || "", class: "mono" });
    ied.addEventListener("change", () => { const i = S.ieds.find(x => x.nome === ied.value); if (i) perfil.value = i.perfil; });
    const opmsk = h("input", { value: e?.opmsk || store("opmsk") || "10C830", class: "mono" });
    const status = sel(ENS_STATUS, e?.status || "");
    const res = h("textarea", { rows: 6 });
    res.value = e?.resultado || "";
    const obs = h("textarea", { rows: 3 });
    obs.value = e?.observacoes || "";
    await modal(e ? "Editar ensaio" : "Registrar ensaio", h("div", { class: "stack" },
      field("Teste", teste), esperado,
      h("div", { class: "form" }, field("IED", ied), field("Perfil", perfil), field("OPMSK", opmsk), field("Situação", status)),
      field("Resultado obtido (o que o SAGE gravou no .alr/.sde, reports, capturas)", res), field("Observações", obs)), [
      { label: "Cancelar" },
      {
        label: "Salvar", cls: "primary", click: async () => {
          const body = { ...(e || {}), teste: teste.value, ied: ied.value, perfil: perfil.value, opmsk: opmsk.value, status: status.value, resultado: res.value, observacoes: obs.value };
          delete body.id;
          const r = e ? await send("PUT", `/ensaios/${e.id}`, body) : await send("POST", "/ensaios", body);
          if (r) load();
          return r || false;
        },
      }]);
  }
  load().catch(e => put(el, h("div", { class: "alert bad" }, e.message)));
  return { el };
}

// ---------------------------------------------------------------------------- editor de CID
/* Três visões do mesmo rascunho: Modelo (IED → LD → LN → DO → DA, expandido dos tipos e combinado com as
 * instâncias), Tipos (DataTypeTemplates com os usos) e XML (o arquivo inteiro). A árvore carrega sob demanda. */
const CE_KIND = {
  ied: "IED", ld: "LD", ln: "LN", do: "DO", sdo: "SDO", da: "DA", bda: "BDA", ds: "DS", fcda: "FCDA", rcb: "RCB",
  goose: "GSE", log: "LOG", opt: "OPT", in: "IN", tpl: "TPL", lnt: "LNT", dot: "DOT", dat: "DAT", enum: "ENUM",
  val: "VAL", priv: "PRIV", com: "COM", ap: "AP", srv: "SRV", doc: "SCL", sub: "SUB", el: "XML",
};
const ceKind = t => h("span", { class: "ck ck-" + (CE_KIND[t] ? t : "el") }, CE_KIND[t] || "XML");
const CE_VISTAS = [["modelo", "Modelo"], ["tipos", "Tipos"], ["xml", "XML"]];
const CE_PREF = ["FCDA", "DataSet", "DOI", "DAI", "SDI", "Val", "DO", "DA", "SDO", "BDA", "EnumVal", "LN", "P", "ConnectedAP"];
const isAdmin = () => !S.eu || S.eu.papel === "administrador";
const fmtSize = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const ceUrl = (sid, p, q = {}) => `/cid-editor/${sid}${p}` + (Object.keys(q).length ? "?" + new URLSearchParams(q) : "");

function pageCidHome(mode = "edit") {
  const view = mode === "view";
  const base = view ? "#/cidview" : "#/cid";
  const el = h("div", { class: "stack" }, h("div", { class: "muted" }, "carregando…"));
  const file = h("input", { type: "file", accept: ".cid,.icd,.scd,.iid,.xml,.sed" });
  file.addEventListener("change", async () => {
    if (!file.files[0]) return;
    const fd = new FormData();
    fd.append("arquivo", file.files[0], file.files[0].name);
    try { const r = await api("POST", "/cid-editor/files", fd); location.hash = `${base}/${r.sessao}`; }
    catch (e) { toast("Arquivo recusado: " + e.message, "bad"); }
  });
  async function load() {
    let ieds, drafts;
    try {
      [ieds, drafts] = await Promise.all([api("GET", "/ieds").then(r => r.ieds), api("GET", "/cid-editor").then(r => r.rascunhos)]);
    } catch (e) { put(el, h("div", { class: "alert bad" }, e.message)); return; }
    const byIed = Object.fromEntries(drafts.filter(d => d.origem === "ied").map(d => [d.ied, d]));
    const avulsos = drafts.filter(d => d.origem === "arquivo");
    const orfaos = drafts.filter(d => d.origem === "ied" && !ieds.some(i => i.id === d.ied));
    const discard = async d => {
      if (!await confirmDlg(`Descartar o rascunho “${d.nome}”? As alterações não aplicadas se perdem.`, "Descartar", true)) return;
      if (await send("DELETE", `/cid-editor/${d.sessao}`)) load();
    };
    put(el,
      h("h1", {}, view ? "Visualizador de CID" : "Editor de CID"),
      view ? h("p", { class: "muted small" }, "Mostra o arquivo por assunto: resumo e histórico, comunicação (IPs, GOOSE), ",
        "blocos de controle (RCB, GOOSE, SMV, Log), DataSets com cada membro, assinaturas (ExtRef e clientes de report), ",
        "subestação, comparação com outro CID e exportação em CSV. Nada é alterado aqui.")
        : h("p", { class: "muted small" }, "Edita o CID de um IED num rascunho, com três visões: o modelo IEC 61850 instanciado ",
          "(IED → LD → LN → DO → DA, com FC, tipo e valor), os tipos (DataTypeTemplates, com quem usa cada um) e o XML. ",
          "O IED só muda quando o rascunho é aplicado (reimportação, que mantém o estado dos pontos)."),
      h("h3", {}, "CID dos IEDs"),
      ieds.length ? h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["IED", "CID", "Rascunho", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, ieds.map(i => {
          const d = byIed[i.id];
          return h("tr", {},
            h("td", {}, h("b", {}, i.nome), i.nome_ied ? h("div", { class: "desc mono" }, i.nome_ied) : null),
            h("td", { class: "small" }, i.cid || "—"),
            h("td", { class: "small" }, d ? [h("span", { class: "badge warn" }, `${d.alteracoes} alteração(ões)`), " ", tsEl(d.alterado_em)] : h("span", { class: "muted" }, "—")),
            h("td", { class: "right nowrap" }, h("a", { class: "btn", href: `${base}/ied-${i.id}` }, view ? (d ? "Ver rascunho" : "Ver") : d ? "Continuar" : "Abrir"),
              d && isAdmin() ? [" ", h("button", { class: "sm danger", onclick: () => discard(d) }, "Descartar")] : null));
        })))) : h("p", { class: "muted" }, "Nenhum IED."),
      h("h3", {}, "Arquivos avulsos"),
      avulsos.length || orfaos.length ? h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Arquivo", "Alterado", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, [...avulsos, ...orfaos].map(d => h("tr", {},
          h("td", {}, d.nome, d.origem === "ied" ? h("div", { class: "desc" }, `rascunho do IED ${d.ied}, que foi removido`) : null),
          h("td", {}, tsEl(d.alterado_em)),
          h("td", { class: "right nowrap" }, h("a", { class: "btn", href: `${base}/${d.sessao}` }, view ? "Ver" : "Abrir"),
            isAdmin() ? [" ", h("button", { class: "sm danger", onclick: () => discard(d) }, "Apagar")] : null)))))) :
        h("p", { class: "muted small" }, "Nenhum."),
      isAdmin() ? h("div", { class: "card stack" }, h("b", {}, view ? "Ver um arquivo SCL sem criar IED" : "Abrir um arquivo SCL sem criar IED"),
        h("p", { class: "muted small" }, "CID, ICD, SCD ou IID. Dá para editar, validar, baixar e depois criar um IED com ele."), file) : null);
  }
  load();
  return { el };
}

function pageCid(sid, abrirNo) {
  if (!sid) return pageCidHome();
  const admin = isAdmin();
  const head = h("div", { class: "stack" });
  const tabs = h("div", { class: "ce-tabs" });
  const q = h("input", { type: "search", placeholder: "Buscar nome, referência, descrição…  ( / )" });
  const results = h("div", { class: "ce-results", hidden: true });
  const treeBox = h("div", { class: "ce-treebox" });
  const left = h("div", { class: "ce-left" }, tabs, h("div", { class: "ce-search" }, q, results), treeBox);
  const gutter = h("div", { class: "ce-gutter", title: "Arraste para ajustar a largura" });
  const right = h("div", { class: "ce-right" }, h("div", { class: "empty" }, "Selecione um nó na árvore."));
  const split = h("div", { class: "ce-split" }, left, gutter, right);
  const el = h("div", { class: "stack" }, head, split);
  const trees = {};
  let info = null;
  const vista0 = store("ce.vista") || S.settings?.editor?.vista_inicial;
  let vista = CE_VISTAS.some(([k]) => k === vista0) ? vista0 : "modelo";
  let selId = null;
  const w0 = +store("ce.largura");
  if (w0) left.style.width = w0 + "px";

  // ------------------------------------------------------------ árvore
  function makeTree(v) {
    const box = h("div", { class: "ce-tree", tabindex: 0 });
    const nodes = new Map();
    let sel = null;
    const t = { v, box, stale: false };
    const fetchKids = (no, offset = 0) => api("GET", ceUrl(sid, "/children", { vista: v, no, offset }));
    function rowEl(d, depth) {
      const tog = h("span", { class: "tg" }, d.filhos ? "▸" : "");
      const row = h("div", { class: "tn", "data-id": d.id, style: { paddingLeft: (4 + depth * 14) + "px" } },
        tog, ceKind(d.tipo),
        v === "xml" && d.tag && d.tag !== d.rotulo ? h("span", { class: "tt" }, d.tag) : null,
        h("span", { class: "tl" }, d.rotulo),
        (d.marcas || []).filter(m => m.t).map(m => h("span", { class: "tm " + (m.c || "") }, m.t)),
        d.desc ? h("span", { class: "td", title: d.desc }, d.desc) : null);
      const kids = h("div", { class: "tk", hidden: true });
      nodes.set(d.id, { row, kids, tog, data: d, depth, open: false, loaded: false });
      tog.addEventListener("click", e => { e.stopPropagation(); toggle(d.id); });
      row.addEventListener("click", () => select(d.id));
      row.addEventListener("dblclick", () => toggle(d.id));
      return [row, kids];
    }
    function addRows(container, r, depth, parentId) {
      for (const d of r.filhos) add(container, rowEl(d, depth));
      const rest = r.total - r.offset - r.filhos.length;
      if (rest > 0) {
        const more = h("div", { class: "tn more", style: { paddingLeft: (22 + depth * 14) + "px" } }, `… mais ${rest}`);
        more.addEventListener("click", async () => {
          more.remove();
          addRows(container, await fetchKids(parentId, r.offset + r.filhos.length), depth, parentId);
        });
        container.append(more);
      }
    }
    t.load = async () => {
      nodes.clear();
      put(box, h("div", { class: "muted small", style: { padding: "8px" } }, "carregando…"));
      let r;
      try { r = await fetchKids(""); } catch (e) { put(box, h("div", { class: "alert bad" }, e.message)); return; }
      box.replaceChildren();
      addRows(box, r, 0, "");
      t.stale = false;
      if (r.filhos.length === 1 && r.filhos[0].filhos) await expand(r.filhos[0].id);  // raiz única: já aberta
    };
    async function expand(id) {
      const n = nodes.get(id);
      if (!n || !n.data.filhos) return false;
      if (!n.loaded) {
        n.loaded = true;
        put(n.kids, h("div", { class: "tn muted", style: { paddingLeft: (22 + (n.depth + 1) * 14) + "px" } }, "carregando…"));
        n.kids.hidden = false;
        let r;
        try { r = await fetchKids(id); } catch (e) { n.loaded = false; n.kids.replaceChildren(); toast(e.message, "bad"); return false; }
        n.kids.replaceChildren();
        addRows(n.kids, r, n.depth + 1, id);
      }
      n.open = true;
      n.kids.hidden = false;
      n.tog.textContent = "▾";
      return true;
    }
    function collapse(id) {
      const n = nodes.get(id);
      if (!n) return;
      n.open = false;
      n.kids.hidden = true;
      n.tog.textContent = n.data.filhos ? "▸" : "";
    }
    const toggle = id => (nodes.get(id)?.open ? collapse(id) : expand(id));
    function mark(id) {
      if (sel && nodes.get(sel)) nodes.get(sel).row.classList.remove("on");
      sel = id;
      const n = nodes.get(id);
      if (n) { n.row.classList.add("on"); n.row.scrollIntoView({ block: "nearest" }); }
    }
    function select(id) {
      mark(id);
      box.focus({ preventScroll: true });
      showDetail(id);
    }
    t.reveal = async path => {
      for (const id of path.slice(0, -1)) {
        if (!nodes.get(id) || !await expand(id)) break;
      }
      const last = path[path.length - 1];
      mark(last);
      showDetail(last);
    };
    t.refresh = async () => {
      const open = [...nodes.values()].filter(n => n.open).map(n => n.data.id);
      const keep = sel;
      await t.load();
      for (const id of open) if (nodes.get(id)) await expand(id);
      if (keep) mark(keep);
    };
    box.addEventListener("keydown", e => {
      if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Enter"].includes(e.key)) return;
      e.preventDefault();
      const vis = [...box.querySelectorAll(".tn[data-id]")].filter(r => r.offsetParent !== null);
      const i = vis.findIndex(r => r.dataset.id === sel);
      const cur = nodes.get(sel);
      if (e.key === "ArrowDown") { const r = vis[Math.min(vis.length - 1, i + 1)]; if (r) select(r.dataset.id); }
      else if (e.key === "ArrowUp") { const r = vis[Math.max(0, i - 1)]; if (r) select(r.dataset.id); }
      else if (e.key === "Enter" && cur) toggle(sel);
      else if (e.key === "ArrowRight" && cur) {
        if (!cur.open) expand(sel);
        else { const f = cur.kids.querySelector(".tn[data-id]"); if (f) select(f.dataset.id); }
      } else if (e.key === "ArrowLeft" && cur) {
        if (cur.open) collapse(sel);
        else { const p = cur.row.parentElement.previousElementSibling; if (p && p.dataset.id) select(p.dataset.id); }
      }
    });
    return t;
  }

  async function setVista(v) {
    vista = v;
    store("ce.vista", v);
    put(tabs, ...CE_VISTAS.map(([k, l]) => h("button", { class: k === v ? "on" : "", onclick: () => setVista(k) }, l)));
    if (!trees[v]) { trees[v] = makeTree(v); await trees[v].load(); }
    else if (trees[v].stale) await trees[v].refresh();
    put(treeBox, trees[v].box);
    q.placeholder = { modelo: "Buscar no modelo (ex.: Ind3, stVal, CSWI)…  ( / )", tipos: "Buscar nos tipos…  ( / )", xml: "Buscar no XML (tag, atributo, valor)…  ( / )" }[v];
    if (q.value.trim()) doSearch();
  }

  async function goTo(no, prefer) {
    let loc;
    try { loc = await api("GET", ceUrl(sid, "/locate", { no, vista: prefer || vista })); } catch (e) { toast(e.message, "bad"); return; }
    if (loc.vista !== vista) await setVista(loc.vista);
    await trees[vista].reveal(loc.caminho);
  }
  const link = (txt, no, prefer) => h("a", { href: "#", onclick: e => { e.preventDefault(); goTo(no, prefer); } }, txt);

  // ------------------------------------------------------------ alterações
  async function op(body, opt = {}) {
    const r = await send("POST", ceUrl(sid, "/ops"), body);
    if (!r) return null;
    info = r.sessao;
    renderHead();
    toast(r.alterado ? r.mensagem : "Nada mudou", r.alterado ? "ok" : "info", 3000);
    if (r.alterado) await afterChange(opt.keep || r.no || selId);
    return r;
  }
  async function afterChange(no) {
    for (const k in trees) if (k !== vista) trees[k].stale = true;
    await trees[vista].refresh();
    if (no) await goTo(no, vista);
    else if (selId) showDetail(selId);
  }
  async function undo(redo) {
    const r = await send("POST", ceUrl(sid, redo ? "/redo" : "/undo"));
    if (!r) return;
    info = r.sessao;
    renderHead();
    toast(r.mensagem, "ok", 3000);
    await afterChange(selId && selId.startsWith("m") ? selId : null);
  }

  // ------------------------------------------------------------ cabeçalho
  function renderHead() {
    const i = info;
    const c = i.contagem;
    put(head,
      h("div", { class: "row between" },
        h("div", { class: "row" }, h("a", { href: "#/cid" }, "Editor de CID"), h("span", { class: "muted" }, "›"), h("h1", {}, i.nome),
          i.origem === "ied"
            ? (i.ied && i.ied.nome ? h("a", { class: "badge info", href: `#/ied/${i.ied.id}/painel` }, `IED ${i.ied.nome}`) : h("span", { class: "badge bad" }, "IED removido"))
            : h("span", { class: "badge neutral" }, "arquivo avulso"),
          h("span", { class: "badge " + (i.alterado ? "warn" : "ok") }, i.alterado ? "rascunho com alterações" : "sem alterações"),
          h("span", { class: "muted small" }, `SCL ${i.versao_scl} · ${c.LDevice} LD · ${c.LN} LN · ${c.DataSet} DataSets · ${c.ReportControl} RCBs · ${fmtSize(i.tamanho)}`)),
        h("div", { class: "row" },
          admin ? [
            h("button", { class: "sm", disabled: !i.desfazer, title: i.proximo_desfazer ? "Desfazer: " + i.proximo_desfazer + " (Ctrl+Z)" : "", onclick: () => undo(false) }, "↶ Desfazer"),
            h("button", { class: "sm", disabled: !i.refazer, title: i.proximo_refazer ? "Refazer: " + i.proximo_refazer + " (Ctrl+Y)" : "", onclick: () => undo(true) }, "↷ Refazer")] : null,
          h("button", { class: "sm", onclick: validar }, "✓ Validar"),
          h("a", { class: "btn", href: `#/cidview/${sid}`, title: "Comunicação, blocos de controle, DataSets, assinaturas, comparação e CSV" }, "Visualizar"),
          admin ? h("button", { class: "sm", title: "Remove LNodeType, DOType, DAType e EnumType que nenhum elemento usa", onclick: limpar }, "Limpar tipos sem uso") : null,
          h("button", { class: "sm", disabled: !i.alterado, onclick: diferencas }, "Diferenças"),
          h("button", { class: "sm", disabled: !i.historico.length, onclick: historico }, "Histórico"),
          h("a", { class: "btn", href: dl(ceUrl(sid, "/download")) }, "⤓ Baixar"),
          admin && i.origem === "ied" && i.ied && i.ied.nome ? h("button", { class: "primary", disabled: !i.alterado, onclick: aplicar }, "Aplicar no IED") : null,
          admin && i.origem === "arquivo" ? h("button", { class: "primary", onclick: criarIed }, "Criar IED com este arquivo") : null,
          admin ? h("button", { class: "sm danger", disabled: i.origem === "ied" && !i.alterado, onclick: descartar }, i.origem === "ied" ? "Descartar rascunho" : "Apagar arquivo") : null)),
      i.base_mudou ? h("div", { class: "alert warn" }, "O CID do IED mudou depois que este rascunho foi aberto (reimportado em outra tela). Aplicar substitui aquela versão; para partir da atual, descarte o rascunho.") : null,
      i.origem === "ied" && i.alterado && i.ied && i.ied.nome ? h("div", { class: "alert warn row between ce-pend" },
        h("span", {}, h("b", {}, "Alterações ainda não estão no simulador. "),
          `O IED ${i.ied.nome} continua com o CID anterior até você aplicar o rascunho` + (i.historico.length ? ` (${i.historico.length} alteração(ões)).` : ".")),
        admin ? h("button", { class: "primary sm", onclick: () => aplicar() }, "Aplicar no IED agora") : null) : null,
      i.origem === "ied" && !i.alterado ? h("div", { class: "muted small" }, "Você está vendo o CID atual do IED. A primeira alteração cria um rascunho; o IED só muda ao aplicar. DataSets e RCBs extras de teste (tela RCBs) não fazem parte do CID.") : null,
      !admin ? h("div", { class: "alert info" }, "Seu perfil só consulta: a edição do CID é do perfil administrador.") : null);
  }

  // ------------------------------------------------------------ detalhes
  let detSeq = 0;
  async function showDetail(id) {
    selId = id;
    const my = ++detSeq;
    let d;
    try { d = await api("GET", ceUrl(sid, "/node", { no: id })); }
    catch (e) { if (my === detSeq) put(right, h("div", { class: "alert bad" }, e.message)); return; }
    if (my !== detSeq) return;
    const top = right.scrollTop;
    put(right, renderDetail(d));
    right.scrollTop = d.id === right.dataset.last ? top : 0;
    right.dataset.last = d.id;
  }
  const sect = (title, ...kids) => h("div", { class: "ce-sec" }, h("h3", {}, title), ...kids);
  const mono = x => h("span", { class: "mono" }, x);

  function renderDetail(d) {
    const crumbs = (d.caminho || []).length ? h("div", { class: "ce-crumbs" },
      d.caminho.map((c, i) => [i ? h("span", { class: "muted" }, " › ") : null, link(c.rotulo || "?", c.id)])) : null;
    const name = d.classe === "xml" ? [h("span", { class: "muted" }, `<${d.tag}>`), d.rotulo !== d.tag ? " " + d.rotulo : ""] : d.rotulo;
    const title = h("div", { class: "row" }, ceKind(d.tipo), h("h2", { class: "mono", style: { margin: 0 } }, name));
    if (d.classe === "grupo") {
      return h("div", { class: "stack" }, title, h("p", { class: "muted" }, `${d.total} ${d.rotulo}. Expanda o grupo para ver cada tipo; o detalhe de cada um mostra quem o usa e marca os que não são usados.`));
    }
    return h("div", { class: "stack" }, crumbs, title, d.classe === "modelo" ? modelDetail(d) : xmlDetail(d));
  }

  const childTable = list => h("div", { class: "tablewrap" }, h("table", { class: "ce-tbl" },
    h("thead", {}, h("tr", {}, ["Nome", "", "FC / tipo / valor", "Descrição"].map(x => h("th", {}, x)))),
    h("tbody", {}, list.map(r => h("tr", {},
      h("td", { class: "mono nowrap" }, link(r.rotulo, r.id, "modelo")), h("td", {}, ceKind(r.tipo)),
      h("td", {}, (r.marcas || []).filter(m => m.t).map(m => [h("span", { class: "tm " + (m.c || "") }, m.t), " "])),
      h("td", { class: "small muted" }, r.desc || ""))))));
  const attrsRO = list => h("dl", { class: "kv mono small" }, list.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));

  function modelDetail(d) {
    const out = [];
    const kv = h("dl", { class: "kv" });
    const row = (k, v) => { if (v != null && v !== "" && !(Array.isArray(v) && !v.length)) add(kv, [h("dt", {}, k), h("dd", {}, v)]); };
    row("Referência", mono(d.ref));
    if (d.fc) row("Nome MMS", mono(d.mms));
    row("FC", d.fc);
    row("CDC", d.cdc);
    row("bType", d.bType);
    if (d.tipo_ref) row("Tipo", d.tipo_no ? link(d.tipo_ref, d.tipo_no, "tipos") : h("span", { class: "badge bad" }, `${d.tipo_ref} não existe`));
    if (d.lnType != null) row("lnType", d.tipo_no ? link(d.lnType, d.tipo_no, "tipos") : h("span", { class: "badge bad" }, `${d.lnType || "(vazio)"} não existe`));
    if (d.fcs && !d.fc) row("FCs", d.fcs.join(", "));
    row("Gatilhos", (d.trg || []).join(", "));
    row("Descrição no tipo", d.desc_tipo);
    if (d.lds) row("LDs", d.lds.join(", "));
    out.push(kv);
    const inst = ["do", "sdo", "da", "bda"].includes(d.nivel);
    const desc = h("input", { value: d.desc || "", disabled: !admin, placeholder: inst ? "desc da instância (DOI/SDI/DAI)" : "" });
    const saveDesc = () => op({ op: "descricao", no: d.id, desc: desc.value });
    desc.addEventListener("keydown", e => { if (e.key === "Enter") saveDesc(); });
    out.push(h("div", { class: "row ce-inline" }, field(inst ? "Descrição (instância)" : "Descrição", desc, "grow"),
      admin ? h("button", { onclick: saveDesc }, "Salvar") : null));
    if (d.folha) out.push(valueEditor(d));
    if (d.atributos_modelo) out.push(sect(`Atributos (${d.atributos_modelo.length})`, childTable(d.atributos_modelo)));
    if (d.objetos) out.push(sect(`Objetos de dados (${d.objetos.length})`, childTable(d.objetos)));
    if (d.datasets) out.push(sect(`DataSets (${d.datasets.length})`, d.datasets.length
      ? h("ul", { class: "list" }, d.datasets.map(x => h("li", {}, link(x.rotulo, x.no), h("span", { class: "muted" }, ` — ${x.n} FCDA`)))) : h("p", { class: "muted small" }, "nenhum")));
    if (d.rcbs) out.push(sect(`ReportControls (${d.rcbs.length})`, d.rcbs.length
      ? h("ul", { class: "list" }, d.rcbs.map(x => h("li", {}, link(x.rotulo, x.no), h("span", { class: "muted" }, ` — DataSet ${x.datSet || "(nenhum)"}`)))) : h("p", { class: "muted small" }, "nenhum")));
    if (d.em_datasets) {
      out.push(sect("Em DataSets",
        d.em_datasets.length ? h("ul", { class: "list" }, d.em_datasets.map(x => h("li", {}, link(x.dataset_nome, x.dataset), h("span", { class: "muted" }, " — "), link(x.texto, x.fcda))))
          : h("p", { class: "muted small" }, "não está em nenhum DataSet"),
        admin && d.datasets_disponiveis.length && (d.fc || (d.fcs || []).length) ? fcdaAdder(d) : null));
    }
    const xl = [];
    if (d.xml_no) xl.push(link("elemento no XML", d.xml_no, "xml"));
    if (d.instancia_no) xl.push(link("instância (DOI/SDI/DAI) no XML", d.instancia_no, "xml"));
    if (d.template_no) xl.push(link("definição no tipo", d.template_no, "tipos"));
    if (xl.length) out.push(h("div", { class: "row small" }, xl.map((x, i) => [i ? h("span", { class: "muted" }, "·") : null, x])));
    if (d.atributos && d.atributos.length) out.push(sect("Atributos XML", attrsRO(d.atributos)));
    if (d.instancia_atributos && d.instancia_atributos.length) out.push(sect("Atributos da instância (DAI)", attrsRO(d.instancia_atributos)));
    return h("div", { class: "stack" }, out);
  }

  function valueEditor(d) {
    let inp;
    if (d.opcoes && d.opcoes.length) {
      const opts = d.enum ? d.enum.map(([o, t]) => [t, `${t}  (${o})`]) : d.opcoes.map(x => [x, x]);
      let cur = d.valor;
      if (d.enum && cur != null) { const m = d.enum.find(([o]) => o === cur); if (m) cur = m[1]; }
      if (cur != null && !opts.some(([v]) => v === cur)) opts.unshift([cur, `${cur}  (atual, fora da lista)`]);
      inp = sel([["", "— sem valor na instância —"], ...opts], cur ?? "", { disabled: !admin });
    } else {
      inp = h("input", { value: d.valor ?? "", class: "mono", disabled: !admin, placeholder: d.valor_tipo != null ? `do tipo: ${d.valor_tipo}` : "" });
      inp.addEventListener("keydown", e => { if (e.key === "Enter") save(); });
    }
    function save() {
      const v = inp.value;
      op({ op: "valor", no: d.id, valor: inp.tagName === "SELECT" && v === "" ? null : v });
    }
    const origem = { "instância": h("span", { class: "badge ok" }, "valor da instância (DAI)"), tipo: h("span", { class: "badge neutral" }, "valor padrão do tipo") }[d.valor_origem]
      || h("span", { class: "badge neutral" }, "sem valor no CID");
    return sect("Valor no CID",
      h("div", { class: "row" }, inp, admin && !d.array ? h("button", { class: "primary", onclick: save }, "Salvar valor") : null,
        admin && d.valor_origem === "instância" ? h("button", { onclick: () => op({ op: "valor", no: d.id, valor: null }) }, "Voltar ao valor do tipo") : null, origem),
      d.array ? h("div", { class: "alert warn" }, "Atributo em array: edite os elementos (DAI/SDI com ix) pela visão XML.") : null,
      d.valor_origem === "instância" && d.valor_tipo != null ? h("div", { class: "muted small" }, `Padrão do tipo: ${d.valor_tipo}`) : null,
      h("div", { class: "muted small" }, "É o valor de configuração gravado no CID (Val). O valor que o simulador publica em tempo real se ajusta nas telas de sinais."));
  }

  function fcdaAdder(d) {
    const ds = sel(d.datasets_disponiveis.map(x => [x.no, x.rotulo]), d.datasets_disponiveis[0].no);
    const fc = d.fc ? null : sel(d.fcs, d.fcs.includes("ST") ? "ST" : d.fcs[0]);
    return h("div", { class: "row" }, h("span", { class: "small muted" }, "Incluir no DataSet"), ds, fc,
      h("button", { onclick: () => op({ op: "fcda", no: d.id, dataset: ds.value, fc: fc ? fc.value : d.fc }) }, "+ FCDA"),
      h("span", { class: "small muted" }, "entra no fim (a ordem define a posição do membro no report)"));
  }

  function xmlDetail(d) {
    const out = [];
    const links = [];
    if (d.modelo) links.push(link("ver no modelo", d.modelo, "modelo"));
    for (const r of d.refs) links.push(r.no ? link("→ " + r.rotulo, r.no, "auto") : h("span", { class: "badge bad" }, `${r.rotulo}: ${r.erro}`));
    if (links.length) out.push(h("div", { class: "row small" }, links.map((x, i) => [i ? h("span", { class: "muted" }, "·") : null, x])));
    if (d.fcda) out.push(h("div", { class: "alert " + (d.fcda.ok ? "info" : "bad") }, d.fcda.ok ? "FCDA resolvida no modelo." : "FCDA não resolve: " + d.fcda.motivo));
    if (d.rcb) {
      out.push(sect("Report Control Block", rcbForm(d)));
      out.push(h("details", { class: "ce-xml" }, h("summary", {}, `Atributos XML (${d.atributos.length}) — edição livre`), attrEditor(d)));
    } else out.push(sect("Atributos", attrEditor(d)));
    if (d.texto != null || d.aceita_texto) out.push(sect("Conteúdo", textEditor(d)));
    if (d.fcdas) out.push(sect(`FCDAs (${d.fcdas.length})`, fcdaTable(d)));
    if (d.enum) out.push(sect(`Valores (${d.enum.length})`, h("dl", { class: "kv mono small" }, d.enum.map(([o, t]) => [h("dt", {}, o), h("dd", {}, t || h("span", { class: "muted" }, "(vazio)"))]))));
    if (d.usado_por) {
      const n = d.usado_total ?? d.usado_por.length;
      out.push(sect(`Usado por (${n})`, n ? h("ul", { class: "list" }, d.usado_por.map(u => h("li", {}, link(u.rotulo, u.no, "auto"))),
        n > d.usado_por.length ? h("li", { class: "muted" }, `… e mais ${n - d.usado_por.length}`) : null)
        : h("p", { class: "muted small" }, d.tag === "DataSet" ? "nenhum bloco de controle usa este DataSet" : "nenhum elemento usa este tipo")));
    }
    const fk = Object.entries(d.filhos);
    if (fk.length) out.push(h("div", { class: "small muted" }, "Filhos: " + fk.map(([k, n]) => `${n} ${k}`).join(", ")));
    if (admin) {
      out.push(h("div", { class: "row ce-actions" },
        h("button", { class: "sm", onclick: () => addChild(d) }, "+ Filho…"),
        d.pode_mover ? [
          h("button", { class: "sm", onclick: () => op({ op: "duplicar", no: d.id }) }, "Duplicar"),
          h("button", { class: "sm", title: "Mover para cima", onclick: () => op({ op: "mover", no: d.id, direcao: -1 }) }, "↑"),
          h("button", { class: "sm", title: "Mover para baixo", onclick: () => op({ op: "mover", no: d.id, direcao: 1 }) }, "↓")] : null,
        h("button", { class: "sm", disabled: d.xml_truncado, title: d.xml_truncado ? "Elemento grande demais para editar como texto; edite os filhos" : "", onclick: () => editXml(d) }, "Editar XML…"),
        d.pode_mover ? h("button", { class: "sm danger", onclick: () => excluir(d) }, "Excluir") : null));
    }
    out.push(h("details", { class: "ce-xml", open: d.xml_tamanho < 6000 },
      h("summary", {}, `XML (${fmtSize(d.xml_tamanho)})${d.xml_truncado ? " — mostrando o começo" : ""}`), h("pre", { class: "json" }, d.xml)));
    return h("div", { class: "stack" }, out);
  }

  // ------------------------------------------------------------ formulário do ReportControl
  const RCB_TRG_TXT = {
    dchg: ["Mudança de dado", "envia quando o valor de um membro muda (stVal, mag…)"],
    qchg: ["Mudança de qualidade", "envia quando a qualidade (q) de um membro muda"],
    dupd: ["Atualização de dado", "envia a cada atualização, mesmo sem mudança de valor"],
    period: ["Integridade (periódico)", "envia todos os membros a cada intgPd ms"],
    gi: ["Interrogação geral (GI)", "o cliente pode pedir todos os membros (o SAGE pede ao conectar)"],
  };
  const RCB_OPT_TXT = {
    seqNum: ["Número de sequência", "o cliente detecta report perdido ou repetido"],
    timeStamp: ["Hora do report", "hora em que o report foi gerado"],
    dataSet: ["Nome do DataSet", "referência do DataSet dentro do report"],
    reasonCode: ["Motivo da inclusão", "dchg, qchg, dupd, integridade ou GI, por membro"],
    dataRef: ["Referência dos dados", "referência MMS de cada membro (reports bem maiores)"],
    entryID: ["Identificação da entrada", "permite ao cliente retomar o buffer (só BRCB)"],
    configRef: ["Revisão da configuração", "o cliente percebe que o DataSet ou o RCB mudou"],
    bufOvfl: ["Estouro de buffer", "avisa que o buffer transbordou e houve perda (só BRCB)"],
    segmentation: ["Segmentação", "Só Ed. 1: report dividido em partes quando não cabe numa PDU"],
  };
  const RCB_CAP = { Fix: ["fixo", "", "Fixo no IED: não muda nem por configuração"], Conf: ["config.", "", "Muda pelo arquivo SCL (configuração)"], Dyn: ["online", "info", "O cliente pode mudar online, por MMS"] };

  function rcbForm(d) {
    const r = d.rcb, a = r.atributos;
    const rs = (r.servicos && r.servicos.report_settings) || {};
    const crc = (r.servicos && r.servicos.conf_report_control) || null;
    const dis = !admin;
    const lvl = {};
    for (const x of r.avisos) if (!lvl[x.campo] || x.nivel === "erro") lvl[x.campo] = x.nivel;
    const mark = (campo, node) => { if (lvl[campo] === "erro" || lvl[campo] === "aviso") node.classList.add("rcb-" + lvl[campo]); return node; };
    const cap = k => { const c = RCB_CAP[rs[k]]; return c ? h("span", { class: "tm " + c[1], title: `Services/ReportSettings ${k}="${rs[k]}": ${c[2]}` }, c[0]) : null; };
    const lab = (txt, k) => h("span", { class: "row rcb-lab" }, txt, k ? cap(k) : null);
    const inp = (v, extra = {}) => h("input", { value: v ?? "", class: "mono", disabled: dis, ...extra });

    // ---- Detalhes
    const fName = inp(a.name);
    const fDesc = h("input", { value: a.desc ?? "", disabled: dis });
    const dsOpts = [["", "— nenhum —"], ...r.datasets.map(x => [x.nome, `${x.nome}  (${x.n} membro${x.n === 1 ? "" : "s"})`])];
    if (a.datSet && !r.datasets.some(x => x.nome === a.datSet)) dsOpts.push([a.datSet, `${a.datSet}  (não existe neste LN)`]);
    const fDs = sel(dsOpts, a.datSet || "", { disabled: dis });
    const fRpt = inp(a.rptID);
    const fBuf = sel([["true", "Com buffer (BRCB)"], ["false", "Sem buffer (URCB)"]], r.buffered ? "true" : "false", { disabled: dis });
    const fIntg = inp(a.intgPd ?? "", { type: "number", min: 0, placeholder: "0" });
    const fBufT = inp(a.bufTime ?? "", { type: "number", min: 0, placeholder: "0" });
    const fConf = inp(a.confRev ?? "", { type: "number", min: 0, placeholder: "0" });
    const fIdx = sel([["", "padrão (true)"], ["true", "true"], ["false", "false"]], a.indexed ?? "", { disabled: dis });
    const fMax = inp(r.rptenabled.max, { type: "number", min: 0, max: 99 });
    const fRDesc = h("input", { value: r.rptenabled.desc || "", disabled: dis });
    const bump = chk("incrementar confRev ao salvar", false, { disabled: dis });
    const rptPrefix = r.rptid_sugerido.replace(/\.(BR|RP)\.[^.]*$/, "");
    const sugerido = () => `${rptPrefix}.${fBuf.value === "true" ? "BR" : "RP"}.${fName.value.trim()}`;
    const dsInfo = h("span", { class: "muted small" });
    const showDs = () => {
      const x = r.datasets.find(x => x.nome === fDs.value);
      put(dsInfo, x ? [link("abrir DataSet", x.no, "xml"), x.desc ? ` · ${x.desc}` : ""] : null);
      if (fDs.value !== (a.datSet || "")) bump.input.checked = true;
    };
    fDs.addEventListener("change", showDs);
    showDs();

    const detalhes = h("div", { class: "stack" },
      h("div", { class: "form" },
        mark("name", field(lab("Nome", "cbName"), fName)),
        mark("datSet", field(lab("DataSet", "datSet"), fDs)),
        mark("rptID", field(lab("RptID", "rptID"), h("div", { class: "row nowrap" }, h("span", { class: "grow" }, fRpt),
          admin ? h("button", { class: "sm", type: "button", title: "Usar LD/LN.BR|RP.nome", onclick: () => { fRpt.value = sugerido(); } }, "Padrão") : null), "wide")),
        field("Descrição", fDesc, "wide"),
        field(lab("Tipo"), fBuf),
        mark("intgPd", field(lab("IntgPd (ms)", "intgPd"), fIntg)),
        mark("bufTime", field(lab("BufTime (ms)", "bufTime"), fBufT)),
        mark("confRev", field(lab("ConfRev"), h("div", { class: "row nowrap" }, h("span", { class: "grow" }, fConf),
          admin ? h("button", { class: "sm", type: "button", title: "Somar 1", onclick: () => { fConf.value = String((parseInt(fConf.value, 10) || 0) + 1); bump.input.checked = false; } }, "+1") : null))),
        r.edicao >= 2 || a.indexed != null ? field(lab("Indexed"), fIdx) : null,
        mark("max", field(lab("Max enabled (instâncias)"), fMax))),
      h("div", { class: "row" }, dsInfo),
      admin ? bump.el : null,
      h("div", { class: "muted small" }, "Max enabled é quantos clientes usam o RCB ao mesmo tempo; no simulador cada um vira uma instância (sufixo 01, 02…). IntgPd e BufTime em milissegundos."));

    // ---- Gatilhos e campos opcionais
    const flagBox = (lista, txt, extra) => {
      const out = {};
      const rows = Object.entries(lista).map(([k, v]) => {
        const c = chk("", v.valor, { disabled: dis });
        out[k] = { input: c.input, orig: v.valor, explicito: v.explicito };
        const [t, dica] = txt[k] || [k, ""];
        return mark(k, h("label", { class: "rcb-flag", title: dica }, c.input,
          h("span", { class: "grow" }, h("b", {}, t), " ", h("span", { class: "mono muted small" }, k),
            h("div", { class: "muted small" }, dica, v.explicito ? "" : ` · não declarado no XML, vale o padrão (${v.padrao ? "ligado" : "desligado"})`)),
          extra ? extra(k) : null));
      });
      return { el: h("div", { class: "rcb-flags" }, rows), map: out };
    };
    const m = r.membros;
    const trg = flagBox(r.trgops, RCB_TRG_TXT, k => m && m.total && k in m
      ? h("span", { class: "tm " + (m[k] ? "info" : ""), title: "Membros do DataSet cujo tipo declara este gatilho" }, `${m[k]}/${m.total} membros`) : null);
    const opt = flagBox(r.optfields, RCB_OPT_TXT);
    const gatilhos = h("div", { class: "rcb-cols" },
      h("div", { class: "stack" }, h("h4", {}, "TrgOps — o que dispara o report ", cap("trgOps")), trg.el),
      h("div", { class: "stack" }, h("h4", {}, "OptFields — o que vai no report ", cap("optFields")), opt.el));

    // ---- Clientes (RptEnabled / ClientLN)
    const dlid = "rcb-ieds-" + Math.random().toString(36).slice(2);
    const cli = r.clientlns.map(c => ({ ...c }));
    const tb = h("tbody");
    const cols = [["iedName", "IED cliente"], ["apRef", "apRef"], ["ldInst", "LD"], ["prefix", "Prefixo"], ["lnClass", "Classe LN"], ["lnInst", "Inst"], ["desc", "Descrição"]];
    const renderCli = () => put(tb,
      cli.map((c, i) => h("tr", {}, cols.map(([k]) => h("td", {}, h("input", {
        value: c[k] || "", class: k === "desc" ? "" : "mono", disabled: dis, list: k === "iedName" ? dlid : null,
        placeholder: k === "lnClass" ? "ITCI" : "", oninput: e => { c[k] = e.target.value; },
      }))), admin ? h("td", {}, h("button", { class: "sm ghost", type: "button", title: "Remover", onclick: () => { cli.splice(i, 1); renderCli(); } }, "✕")) : null)),
      !cli.length ? h("tr", {}, h("td", { colspan: 8, class: "muted small" }, "Nenhum cliente reservado: qualquer cliente pode habilitar as instâncias livres.")) : null);
    renderCli();
    const clientes = h("div", { class: "stack" },
      h("div", { class: "form" }, field("Descrição do RptEnabled", fRDesc, "wide")),
      h("div", { class: "tablewrap" }, h("table", { class: "ce-tbl rcb-cli" }, h("thead", {}, h("tr", {}, cols.map(([, l]) => h("th", {}, l)), admin ? h("th", {}) : null)), tb)),
      h("datalist", { id: dlid }, r.ieds.map(x => h("option", { value: x }))),
      admin ? h("div", { class: "row" }, h("button", { class: "sm", type: "button", onclick: () => { cli.push({ iedName: "", ldInst: "", prefix: "", lnClass: "ITCI", lnInst: "1" }); renderCli(); } }, "+ Cliente")) : null,
      h("div", { class: "muted small" }, "ClientLN reserva uma instância do RCB para um cliente (o LN do SCADA/IHM). Cada ClientLN ocupa uma das instâncias de Max enabled."));

    // ---- abas
    const abas = [["det", "Detalhes", detalhes], ["trg", "TrgOps e OptFields", gatilhos], ["cli", `Clientes (${cli.length}/${r.rptenabled.max})`, clientes]];
    let aba = store("ce.rcbaba") || "det";
    if (!abas.some(x => x[0] === aba)) aba = "det";
    const tabBar = h("div", { class: "tabs", style: { margin: "4px 0 10px" } });
    const renderTabs = () => {
      put(tabBar, abas.map(([k, l]) => h("a", { href: "javascript:void 0", class: k === aba ? "on" : "", onclick: () => { aba = k; store("ce.rcbaba", k); renderTabs(); } }, l)));
      for (const [k, , el] of abas) el.hidden = k !== aba;
    };
    renderTabs();

    // ---- análise
    const ordem = { erro: 0, aviso: 1, info: 2 };
    const av = [...r.avisos].sort((x, y) => ordem[x.nivel] - ordem[y.nivel]);
    const nErr = av.filter(x => x.nivel === "erro").length, nAv = av.filter(x => x.nivel === "aviso").length;
    const analise = h("details", { class: "rcb-ana", open: nErr + nAv > 0 },
      h("summary", {}, h("b", {}, "Análise das opções "),
        nErr ? h("span", { class: "badge bad" }, `${nErr} erro${nErr > 1 ? "s" : ""}`) : null, " ",
        nAv ? h("span", { class: "badge warn" }, `${nAv} aviso${nAv > 1 ? "s" : ""}`) : null, " ",
        !nErr && !nAv ? h("span", { class: "badge ok" }, "sem problemas") : null,
        av.length - nErr - nAv ? h("span", { class: "muted small" }, ` · ${av.length - nErr - nAv} observação(ões)`) : null),
      h("ul", { class: "rcb-av" }, av.map(x => h("li", { class: "rcb-av-" + x.nivel }, h("span", { class: "mono small" }, x.campo), " ", x.msg))),
      r.servicos ? h("div", { class: "muted small" }, "IED declara em Services: ",
        rs && Object.keys(rs).length ? Object.entries(rs).map(([k, v]) => `${k}=${v}`).join(", ") : "sem ReportSettings",
        crc ? ` · ConfReportControl max=${crc.max ?? "?"} (o IED tem ${r.total_rcbs_ied} RCBs)` : "") : null);

    // ---- gravar
    function coletar() {
      const at = {};
      const muda = (k, v, orig) => { if ((v ?? "") !== (orig ?? "")) at[k] = v === "" && k !== "rptID" && k !== "desc" ? null : v; };
      muda("name", fName.value.trim(), a.name);
      muda("desc", fDesc.value, a.desc);
      muda("datSet", fDs.value, a.datSet);
      muda("rptID", fRpt.value.trim(), a.rptID);
      if ((fBuf.value === "true") !== r.buffered) at.buffered = fBuf.value;
      muda("intgPd", fIntg.value.trim(), a.intgPd);
      muda("bufTime", fBufT.value.trim(), a.bufTime);
      let conf = fConf.value.trim();
      if (bump.input.checked) conf = String((parseInt(conf, 10) || 0) + 1);
      muda("confRev", conf, a.confRev);
      muda("indexed", fIdx.value, a.indexed);
      const flags = mp => {
        const o = {};
        for (const [k, x] of Object.entries(mp)) if (x.input.checked !== x.orig) o[k] = x.input.checked;
        return Object.keys(o).length ? o : null;
      };
      const body = { op: "rcb", no: d.id, atributos: at };
      const t = flags(trg.map), o = flags(opt.map);
      if (t) body.trgops = t;
      if (o) body.optfields = o;
      if (fMax.value.trim() !== String(r.rptenabled.max) || fRDesc.value !== (r.rptenabled.desc || ""))
        body.rptenabled = { max: fMax.value.trim(), desc: fRDesc.value };
      const limpo = cli.map(c => Object.fromEntries(cols.map(([k]) => [k, (c[k] || "").trim()])));
      const origem = r.clientlns.map(c => Object.fromEntries(cols.map(([k]) => [k, (c[k] || "").trim()])));
      if (JSON.stringify(limpo) !== JSON.stringify(origem)) body.clientlns = limpo;
      return body;
    }
    async function salvar(aplicarDepois) {
      const body = coletar();
      const vazio = !Object.keys(body.atributos).length && !body.trgops && !body.optfields && !body.rptenabled && !body.clientlns;
      if (vazio && !aplicarDepois) { toast("Nada alterado no RCB"); return; }
      if (!vazio && !await op(body, { keep: d.id })) return;
      if (aplicarDepois) await aplicar();
    }
    const resumo = h("div", { class: "row small" },
      h("span", { class: "badge " + (r.buffered ? "info" : "neutral") }, r.buffered ? "BRCB" : "URCB"),
      h("span", { class: "muted" }, `SCL Ed. ${r.edicao}`),
      m ? h("span", { class: "muted" }, `· DataSet com ${m.total} membro${m.total === 1 ? "" : "s"}`) : null,
      h("span", { class: "muted" }, `· até ${r.rptenabled.max} cliente(s) simultâneo(s)`));
    return h("div", { class: "stack rcb-form" }, resumo, analise, tabBar, detalhes, gatilhos, clientes,
      admin ? h("div", { class: "row" }, h("button", { onclick: () => salvar(false) }, "Salvar no rascunho"),
        info.origem === "ied" && info.ied && info.ied.nome ? h("button", { class: "primary", onclick: () => salvar(true), title: "Grava no rascunho e aplica o CID no IED do simulador" }, "Salvar e aplicar no IED") : null,
        h("button", { onclick: () => showDetail(d.id) }, "Descartar mudanças"),
        h("span", { class: "muted small" }, info.origem === "ied" ? "O simulador só passa a usar o CID editado depois de aplicar no IED." : "Grava atributos, TrgOps, OptFields, RptEnabled e ClientLN de uma vez (desfaz com Ctrl+Z).")) : null);
  }

  function attrEditor(d) {
    const rows = d.atributos.map(([k, v]) => ({ k, v, inp: h("input", { value: v, class: "mono", disabled: !admin }), del: false }));
    const extra = [];
    const dlid = "ce-sug-" + Math.random().toString(36).slice(2);
    const tb = h("tbody");
    const render = () => put(tb,
      rows.map(r => h("tr", { class: r.del ? "ce-del" : "" }, h("td", { class: "mono" }, r.k), h("td", {}, r.inp),
        admin ? h("td", { class: "right" }, h("button", { class: "sm ghost", title: r.del ? "Manter" : "Remover o atributo", onclick: () => { r.del = !r.del; render(); } }, r.del ? "↺" : "✕")) : null)),
      extra.map(x => h("tr", {}, h("td", {}, x.k), h("td", {}, x.inp),
        h("td", { class: "right" }, h("button", { class: "sm ghost", onclick: () => { extra.splice(extra.indexOf(x), 1); render(); } }, "✕")))),
      !rows.length && !extra.length ? h("tr", {}, h("td", { colspan: 3, class: "muted small" }, "sem atributos")) : null);
    render();
    function save() {
      const m = {};
      for (const r of rows) { if (r.del) m[r.k] = null; else if (r.inp.value !== r.v) m[r.k] = r.inp.value; }
      for (const x of extra) if (x.k.value.trim()) m[x.k.value.trim()] = x.inp.value;
      if (!Object.keys(m).length) { toast("Nenhum atributo alterado"); return; }
      op({ op: "atributos", no: d.id, atributos: m });
    }
    tb.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.tagName === "INPUT") save(); });
    return h("div", { class: "stack" },
      h("div", { class: "tablewrap" }, h("table", { class: "ce-attrs" }, tb)),
      h("datalist", { id: dlid }, d.sugeridos.map(s => h("option", { value: s }))),
      admin ? h("div", { class: "row" },
        h("button", { class: "sm", onclick: () => { extra.push({ k: h("input", { class: "mono", placeholder: "nome", list: dlid }), inp: h("input", { class: "mono", placeholder: "valor" }) }); render(); extra[extra.length - 1].k.focus(); } }, "+ atributo"),
        h("button", { class: "sm primary", onclick: save }, "Salvar atributos")) : null);
  }

  function textEditor(d) {
    const long = (d.texto || "").length > 80 || (d.texto || "").includes("\n");
    const inp = long ? h("textarea", { rows: 4, disabled: !admin }, d.texto || "") : h("input", { value: d.texto || "", class: "mono", disabled: !admin, style: { flex: 1 } });
    const save = () => op({ op: "texto", no: d.id, texto: inp.value });
    if (!long) inp.addEventListener("keydown", e => { if (e.key === "Enter") save(); });
    return h("div", { class: "row" }, inp, admin ? h("button", { class: "sm primary", onclick: save }, "Salvar") : null);
  }

  function fcdaTable(d) {
    if (!d.fcdas.length) return h("p", { class: "muted small" }, "DataSet vazio. Inclua membros pela visão Modelo (botão + FCDA no DO ou DA) ou com + Filho.");
    return h("div", { class: "stack" },
      h("div", { class: "tablewrap" }, h("table", { class: "ce-tbl" },
        h("thead", {}, h("tr", {}, ["#", "FCDA", "", admin ? "" : null].filter(x => x != null).map(x => h("th", {}, x)))),
        h("tbody", {}, d.fcdas.map((f, i) => h("tr", {},
          h("td", { class: "mono muted right" }, i + 1),
          h("td", { class: "mono" }, link(f.texto, f.no, "xml"), f.modelo ? [" ", link("modelo", f.modelo, "modelo")] : null),
          h("td", {}, f.ok ? h("span", { class: "badge ok" }, "ok") : h("span", { class: "badge bad", title: f.motivo }, f.motivo)),
          admin ? h("td", { class: "nowrap right" },
            h("button", { class: "sm ghost", disabled: i === 0, title: "Subir", onclick: () => op({ op: "mover", no: f.no, direcao: -1 }, { keep: d.id }) }, "↑"),
            h("button", { class: "sm ghost", disabled: i === d.fcdas.length - 1, title: "Descer", onclick: () => op({ op: "mover", no: f.no, direcao: 1 }, { keep: d.id }) }, "↓"),
            h("button", { class: "sm ghost", title: "Tirar do DataSet", onclick: () => op({ op: "excluir", no: f.no }, { keep: d.id }) }, "✕")) : null))))),
      h("div", { class: "muted small" }, "A ordem das FCDAs é a ordem dos membros nos reports. Mudar o DataSet de um RCB em uso exige aumentar o confRev para o cliente perceber."));
  }

  async function addChild(d) {
    const tags = d.permitidos.filter(t => t !== "Text");
    const first = CE_PREF.find(t => tags.includes(t)) || tags[0] || "Private";
    const tagSel = sel(tags.length ? tags : ["Private"], first);
    const fields = h("div", { class: "form" });
    let inputs = {};
    const renderF = () => {
      inputs = {};
      const m = d.modelos[tagSel.value] || {};
      put(fields, Object.entries(m).map(([k, v]) => field(k === "#texto" ? "conteúdo (texto)" : k, inputs[k] = h("input", { value: v, class: "mono" }))));
      if (!Object.keys(m).length) add(fields, [h("p", { class: "muted small wide" }, "Sem atributos sugeridos; use o XML livre se precisar.")]);
    };
    tagSel.addEventListener("change", renderF);
    renderF();
    const xml = h("textarea", { rows: 8, placeholder: '<FCDA ldInst="CTRL" prefix="" lnClass="XCBR" lnInst="1" doName="Pos" fc="ST"/>' });
    const mode = sel([["campos", "Por campos"], ["xml", "XML livre"]], "campos");
    const boxCampos = h("div", { class: "stack" }, field("Elemento", tagSel), fields);
    const boxXml = h("div", { class: "stack", hidden: true }, xml, h("div", { class: "muted small" }, "Um elemento só (com filhos, se quiser). O namespace SCL e os prefixos do arquivo já valem."));
    mode.addEventListener("change", () => { boxCampos.hidden = mode.value !== "campos"; boxXml.hidden = mode.value !== "xml"; });
    await modal(`Adicionar filho em <${d.tag}> ${d.rotulo !== d.tag ? d.rotulo : ""}`, h("div", { class: "stack" },
      h("div", { class: "row" }, mode, h("span", { class: "muted small" }, `Previstos pelo esquema: ${tags.join(", ") || "nenhum"}`)),
      boxCampos, boxXml,
      d.tag === "LN0" || d.tag === "LN" ? h("div", { class: "muted small" }, "ReportControl novo já vem com TrgOps, OptFields e RptEnabled.") : null), [
      { label: "Cancelar", value: null },
      {
        label: "Adicionar", cls: "primary", click: async () => {
          let body;
          if (mode.value === "xml") body = { op: "adicionar", pai: d.id, xml: xml.value };
          else {
            const at = {};
            let texto = "";
            for (const [k, i] of Object.entries(inputs)) { if (k === "#texto") texto = i.value; else at[k] = i.value; }
            body = { op: "adicionar", pai: d.id, tag: tagSel.value, atributos: at, texto };
          }
          return (await op(body)) ? true : false;
        },
      }]);
  }

  async function editXml(d) {
    const ta = h("textarea", { rows: 24, spellcheck: false, style: { whiteSpace: "pre", fontSize: "12px" } }, d.xml);
    await modal(`Editar XML de <${d.tag}> ${d.rotulo !== d.tag ? d.rotulo : ""}`, h("div", { class: "stack" },
      h("div", { class: "muted small" }, "O elemento é trocado pelo texto abaixo. Um erro de sintaxe é recusado sem alterar nada; dá para desfazer."), ta), [
      { label: "Cancelar", value: null },
      { label: "Substituir", cls: "primary", click: async () => ((await op({ op: "substituir", no: d.id, xml: ta.value })) ? true : false) },
    ], { cls: "wide" });
  }

  async function excluir(d) {
    const n = Object.values(d.filhos).reduce((a, b) => a + b, 0);
    if (!await confirmDlg(`Excluir <${d.tag}> ${d.rotulo}${n ? ` e seus ${n} filho(s)` : ""}?\n\nDá para desfazer.`, "Excluir", true)) return;
    op({ op: "excluir", no: d.id });
  }

  // ------------------------------------------------------------ validar, diferenças, histórico, aplicar
  async function validar() {
    const dlg = modal("Validação do rascunho", h("div", { class: "muted" }, "verificando…"), [{ label: "Fechar", value: null }], { cls: "wide" });
    let v;
    try { v = await api("GET", ceUrl(sid, "/validate")); } catch (e) { put(dlg.dialog.querySelector(".db"), h("div", { class: "alert bad" }, e.message)); return; }
    const item = x => h("li", {}, x.no ? h("a", { href: "#", onclick: e => { e.preventDefault(); dlg.close(); goTo(x.no, "auto"); } }, x.msg) : x.msg);
    const lst = (arr, f) => arr.length ? h("ul", { class: "list" }, arr.map(f)) : h("p", { class: "muted small" }, "nenhum");
    const nErr = v.erros.length + v.carga.erros.length;
    put(dlg.dialog.querySelector(".db"), h("div", { class: "stack" },
      h("div", { class: "alert " + (nErr ? "bad" : "info") }, nErr ? `${nErr} erro(s) encontrados.` : "Nenhum erro encontrado.",
        v.avisos.length + v.carga.avisos.length ? ` ${v.avisos.length + v.carga.avisos.length} aviso(s).` : ""),
      sect(`Erros do modelo (${v.erros.length})`, lst(v.erros, item)),
      sect(`Avisos do modelo (${v.avisos.length})`, lst(v.avisos, item)),
      sect("Como o simulador carrega este arquivo",
        v.carga.ieds.map(i => h("div", { class: "small" }, h("b", { class: "mono" }, i.nome), ` — ${i.pontos} pontos (${i.digitais} digitais, ${i.analogicos} analógicos), ${i.datasets} DataSets, ${i.rcbs} RCBs`)),
        h("div", { class: "small muted" }, "Erros"), lst(v.carga.erros, x => h("li", {}, x)),
        h("div", { class: "small muted" }, "Avisos"), lst(v.carga.avisos, x => h("li", {}, x)))));
  }

  async function diferencas() {
    let r;
    try { r = await api("GET", ceUrl(sid, "/diff")); } catch (e) { toast(e.message, "bad"); return; }
    const pre = h("pre", { class: "json ce-diff" }, r.linhas.map(l => h("span", {
      class: l.startsWith("@@") ? "hk" : l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : "",
    }, l + "\n")));
    modal("Diferenças: CID original → rascunho", h("div", { class: "stack" },
      h("div", { class: "small" }, h("span", { class: "badge ok" }, `+${r.adicionadas}`), " ", h("span", { class: "badge bad" }, `−${r.removidas}`), " linhas",
        r.truncado ? h("span", { class: "muted" }, " (lista truncada)") : null), pre), [{ label: "Fechar", value: null }], { cls: "wide" });
  }

  function historico() {
    modal("Histórico do rascunho", h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Quando", "Usuário", "Alteração"].map(x => h("th", {}, x)))),
      h("tbody", {}, [...info.historico].reverse().map(x => h("tr", {}, h("td", {}, tsEl(x.ts)), h("td", { class: "small" }, x.usuario || "—"), h("td", { class: "small mono" }, x.acao)))))),
    [{ label: "Fechar", value: null }]);
  }

  async function aplicar() {
    let v = null;
    try { v = await api("GET", ceUrl(sid, "/validate")); } catch { /* segue sem a contagem */ }
    const nErr = v ? v.erros.length + v.carga.erros.length : 0;
    if (!await confirmDlg(`Aplicar o rascunho no IED “${info.ied.nome}”?\n\nO CID do IED passa a ser o rascunho (reimportação): os pontos que continuam existindo mantêm o estado.` +
      (nErr ? `\n\nA validação encontrou ${nErr} erro(s); o IED sobe só com o que é válido.` : ""), "Aplicar", nErr > 0)) return;
    const r = await send("POST", ceUrl(sid, "/apply"), {});
    if (!r) return;
    info = r.sessao;
    renderHead();
    modal("Rascunho aplicado no IED", h("div", { class: "stack" },
      h("p", {}, `${r.mantidos} ponto(s) mantidos, ${r.novos.length} novo(s), ${r.removidos.length} removido(s).`),
      r.removidos.length ? h("details", {}, h("summary", {}, "Removidos"), h("ul", { class: "list mono" }, r.removidos.map(x => h("li", {}, x)))) : null,
      r.novos.length ? h("details", {}, h("summary", {}, "Novos"), h("ul", { class: "list mono" }, r.novos.map(x => h("li", {}, x)))) : null,
      (r.erros || []).length ? h("div", { class: "alert bad" }, h("b", {}, "Erros da carga"), h("ul", { class: "list" }, r.erros.map(x => h("li", {}, x)))) : null),
    [{ label: "OK", cls: "primary", value: true }]);
  }

  async function limpar() {
    if (!await confirmDlg("Remover dos DataTypeTemplates os tipos que nenhum elemento usa (repetindo até não sobrar nenhum)?\n\nDá para desfazer.", "Limpar")) return;
    op({ op: "limpar" });
  }

  async function criarIed() {
    const up = await send("POST", ceUrl(sid, "/upload-token"));
    if (up) wizardNovo(up);
  }

  async function descartar() {
    const txt = info.origem === "ied"
      ? "Descartar o rascunho? As alterações não aplicadas se perdem e o editor volta ao CID atual do IED."
      : "Apagar este arquivo do editor? As alterações se perdem (baixe antes, se quiser guardar).";
    if (!await confirmDlg(txt, info.origem === "ied" ? "Descartar" : "Apagar", true)) return;
    if (!await send("DELETE", `/cid-editor/${sid}`)) return;
    toast(info.origem === "ied" ? "Rascunho descartado" : "Arquivo apagado", "ok");
    if (info.origem === "ied") route(); else location.hash = "#/cid";
  }

  // ------------------------------------------------------------ busca
  const doSearch = debounce(async () => {
    const t = q.value.trim();
    if (!t) { results.hidden = true; return; }
    let r;
    try { r = await api("GET", ceUrl(sid, "/search", { q: t, vista })); } catch { return; }
    if (q.value.trim() !== t) return;
    put(results,
      h("div", { class: "small muted ce-rh" }, r.total > r.resultados.length ? `${r.total} resultados (primeiros ${r.resultados.length})` : `${r.total} resultado(s)`),
      r.resultados.map(x => h("div", { class: "tn", "data-no": x.no, onclick: () => { results.hidden = true; goTo(x.no, vista); } },
        ceKind(x.tipo), h("span", { class: "tl" }, x.rotulo), x.desc ? h("span", { class: "td" }, x.desc) : null)));
    results.hidden = false;
  }, 250);
  q.addEventListener("input", doSearch);
  q.addEventListener("focus", () => { if (q.value.trim() && results.childElementCount) results.hidden = false; });
  q.addEventListener("keydown", e => {
    if (e.key === "Escape") { results.hidden = true; q.blur(); }
    if (e.key === "Enter") { const f = results.querySelector(".tn[data-no]"); if (f) f.click(); }
  });
  const outside = e => { if (!left.querySelector(".ce-search").contains(e.target)) results.hidden = true; };
  document.addEventListener("mousedown", outside);

  // ------------------------------------------------------------ largura do painel e atalhos
  gutter.addEventListener("pointerdown", e => {
    e.preventDefault();
    const x0 = e.clientX, w = left.getBoundingClientRect().width, max = split.getBoundingClientRect().width - 320;
    gutter.setPointerCapture(e.pointerId);
    document.body.classList.add("col-resizing");
    const move = ev => { left.style.width = Math.max(220, Math.min(max, w + ev.clientX - x0)) + "px"; };
    const up = () => {
      gutter.removeEventListener("pointermove", move);
      document.body.classList.remove("col-resizing");
      store("ce.largura", Math.round(left.getBoundingClientRect().width));
    };
    gutter.addEventListener("pointermove", move);
    gutter.addEventListener("pointerup", up, { once: true });
  });
  const keys = e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || document.querySelector("dialog[open]");
    if (typing || !info) return;
    if (e.key === "/") { e.preventDefault(); q.focus(); q.select(); }
    if (admin && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.shiftKey && info.desfazer) { e.preventDefault(); undo(false); }
    if (admin && (e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === "y" || (e.key.toLowerCase() === "z" && e.shiftKey)) && info.refazer) { e.preventDefault(); undo(true); }
  };
  document.addEventListener("keydown", keys);

  (async () => {
    try { info = await api("GET", `/cid-editor/${sid}`); }
    catch (e) { put(el, h("a", { href: "#/cid" }, "‹ Editor de CID"), h("div", { class: "alert bad" }, e.message)); return; }
    renderHead();
    await setVista(vista);
    if (abrirNo) goTo(abrirNo, "auto");
  })();

  return {
    el,
    onEvent: m => {
      if (info && info.origem === "ied" && info.ied && m.ied === info.ied.id && m.evento === "ied.deleted") renderHead();
    },
    destroy: () => { document.removeEventListener("keydown", keys); document.removeEventListener("mousedown", outside); },
  };
}

// ---------------------------------------------------------------------------- visualizador de CID
/* Leitura do arquivo por assunto, no espírito dos plug-ins do OpenSCD (Communication, Publisher, Subscriber,
 * Substation, Compare IED, Show SCL History). Usa as mesmas sessões do editor e não altera nada. */
const CV_ABAS = [["resumo", "Resumo"], ["comunicacao", "Comunicação"], ["controles", "Blocos de controle"], ["datasets", "DataSets"],
  ["assinaturas", "Assinaturas"], ["subestacao", "Subestação"], ["comparar", "Comparar"], ["exportar", "Exportar"]];
const CV_CB = { ReportControl: ["RCB", "rcb"], GSEControl: ["GOOSE", "goose"], SampledValueControl: ["SMV", "goose"], LogControl: ["LOG", "log"] };
const isOn = v => v === "true" || v === "1";

function pageCidView(sid, aba) {
  if (!sid) return pageCidHome("view");
  aba = CV_ABAS.some(([k]) => k === aba) ? aba : "resumo";
  const head = h("div", { class: "stack" });
  const tabs = h("div", { class: "tabs" });
  const body = h("div", { class: "stack" }, h("div", { class: "muted" }, "carregando…"));
  const el = h("div", { class: "stack" }, head, tabs, body);
  let info = null, ov = null;
  const edLink = (txt, no) => h("a", { href: `#/cid/${sid}/${encodeURIComponent(no)}`, title: "Abrir no editor" }, txt);
  const empty = txt => h("div", { class: "empty card" }, txt);
  const tbl = (cols, rows, cls = "") => h("div", { class: "tablewrap" }, h("table", { class: cls },
    h("thead", {}, h("tr", {}, cols.map(c => h("th", {}, c)))), h("tbody", {}, rows)));
  const chips = (obj, filtro = isOn) => Object.entries(obj || {}).filter(([, v]) => filtro(v)).map(([k]) => h("span", { class: "tm info" }, k));
  const filtroInput = (ph, fn) => { const i = h("input", { type: "search", placeholder: ph, style: { minWidth: "240px" } }); i.addEventListener("input", debounce(() => fn(i.value.trim().toLowerCase()), 200)); return i; };

  function renderHead() {
    put(head, h("div", { class: "row between" },
      h("div", { class: "row" }, h("a", { href: "#/cidview" }, "Visualizador de CID"), h("span", { class: "muted" }, "›"), h("h1", {}, info.nome),
        info.origem === "ied" && info.ied ? h("a", { class: "badge info", href: `#/ied/${info.ied.id}/painel` }, `IED ${info.ied.nome || info.ied.id}`) : h("span", { class: "badge neutral" }, "arquivo avulso"),
        info.alterado ? h("span", { class: "badge warn", title: "Você está vendo o rascunho do editor, não o CID aplicado no IED" }, "rascunho com alterações") : null,
        h("span", { class: "muted small" }, `SCL ${info.versao_scl} · ${fmtSize(info.tamanho)}`)),
      h("div", { class: "row" },
        h("button", { class: "sm", onclick: () => load(true) }, "↻ Recarregar"),
        h("a", { class: "btn", href: dl(ceUrl(sid, "/download")) }, "⤓ CID"),
        h("a", { class: "btn primary", href: `#/cid/${sid}` }, "Abrir no editor"))));
    put(tabs, ...CV_ABAS.map(([k, l]) => h("a", { href: `#/cidview/${sid}/${k}`, class: k === aba ? "on" : "" }, l, badgeAba(k))));
  }
  function badgeAba(k) {
    if (!ov) return null;
    const n = { comunicacao: ov.comunicacao.reduce((a, s) => a + s.aps.length, 0), controles: ov.controles.length, datasets: ov.datasets.length,
      assinaturas: ov.extrefs.length + ov.clientes.length, subestacao: ov.subestacao.length }[k];
    return n ? h("span", { class: "count neutral" }, n) : null;
  }

  async function load(force) {
    try {
      info = await api("GET", `/cid-editor/${sid}`);
      if (!ov || force) ov = await api("GET", ceUrl(sid, "/overview"));
    } catch (e) { put(body, h("a", { href: "#/cidview" }, "‹ Visualizador"), h("div", { class: "alert bad" }, e.message)); return; }
    renderHead();
    put(body, ({ resumo, comunicacao, controles, datasets, assinaturas, subestacao, comparar, exportar })[aba]());
  }

  // ------------------------------------------------------------ abas
  function resumo() {
    const c = info.contagem;
    const cab = ov.cabecalho;
    return h("div", { class: "stack" },
      h("div", { class: "grid c3" },
        h("div", { class: "card" }, h("h2", {}, "Arquivo"), h("dl", { class: "kv" },
          h("dt", {}, "Nome"), h("dd", {}, info.nome), h("dt", {}, "SCL"), h("dd", {}, ov.versao_scl),
          h("dt", {}, "Tamanho"), h("dd", {}, `${fmtSize(info.tamanho)} · ${c.elementos} elementos`),
          h("dt", {}, "Origem"), h("dd", {}, info.origem === "ied" ? `CID do IED ${info.ied?.nome || ""}` : "arquivo avulso"))),
        h("div", { class: "card" }, h("h2", {}, "Conteúdo"), h("div", { class: "cv-nums" },
          [["IED", ov.ieds.length], ["LD", c.LDevice], ["LN", c.LN], ["DataSets", c.DataSet], ["RCBs", c.ReportControl],
            ["GOOSE", c.GSEControl], ["LNodeType", c.LNodeType], ["DOType", c.DOType], ["DAType", c.DAType], ["EnumType", c.EnumType]]
            .map(([k, v]) => h("div", {}, h("b", {}, v), h("span", {}, k))))),
        h("div", { class: "card" }, h("h2", {}, "Cabeçalho (Header)"), cab ? h("dl", { class: "kv mono small" },
          Object.entries(cab.atributos).map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])) : h("p", { class: "muted" }, "sem Header"))),
      h("h3", {}, `IEDs (${ov.ieds.length})`),
      ov.ieds.map(i => h("div", { class: "card stack" },
        h("div", { class: "row between" }, h("div", { class: "row" }, ceKind("ied"), h("b", { class: "mono" }, i.nome), h("span", { class: "muted" }, i.desc)),
          h("span", { class: "muted small" }, [i.fabricante, i.tipo, i.configVersion && `configVersion ${i.configVersion}`].filter(Boolean).join(" · "))),
        h("div", { class: "small" }, `AccessPoints: ${i.aps.join(", ") || "—"} · LDs: ${i.lds.join(", ")} · ${i.lns} LNs`),
        i.servicos.length ? h("details", {}, h("summary", {}, `Serviços declarados (${i.servicos.length})`),
          h("div", { class: "pill-list", style: { marginTop: "6px" } }, i.servicos.map(([t, a]) => h("span", { class: "tm", title: Object.entries(a).map(([k, v]) => `${k}=${v}`).join(" ") },
            t + (Object.keys(a).length ? ` (${Object.entries(a).map(([k, v]) => `${k}=${v}`).join(", ")})` : ""))))) : null)),
      h("h3", {}, `Histórico do arquivo (${cab ? cab.historico.length : 0})`),
      cab && cab.historico.length ? tbl(["Versão", "Revisão", "Quando", "Quem", "O quê", "Por quê"], cab.historico.map(x => h("tr", {},
        h("td", { class: "mono" }, x.version || ""), h("td", { class: "mono" }, x.revision || ""), h("td", { class: "small" }, x.when || ""),
        h("td", { class: "small" }, x.who || ""), h("td", { class: "small" }, x.what || ""), h("td", { class: "small" }, x.why || ""))))
        : h("p", { class: "muted small" }, "O Header não tem History/Hitem."),
      h("div", { class: "grid c2" },
        h("div", { class: "card" }, h("h2", {}, "Namespaces"), ov.namespaces.length ? h("dl", { class: "kv small" },
          ov.namespaces.map(n => [h("dt", { class: "mono" }, n.prefixo), h("dd", { class: "mono" }, n.uri)])) : h("p", { class: "muted" }, "só o SCL")),
        h("div", { class: "card" }, h("h2", {}, "Dados privados (Private)"), ov.privados.length ? h("ul", { class: "list" },
          ov.privados.map(p => h("li", {}, h("span", { class: "mono" }, p.type), ` em ${p.em} (${p.n})`))) : h("p", { class: "muted" }, "nenhum"))));
  }

  function comunicacao() {
    if (!ov.comunicacao.length) return empty("O arquivo não tem a seção Communication.");
    const P = ["IP", "IP-SUBNET", "IP-GATEWAY"];
    return h("div", { class: "stack" }, ov.comunicacao.map(sn => h("div", { class: "card stack" },
      h("div", { class: "row" }, ceKind("com"), h("b", { class: "mono" }, sn.nome), sn.tipo ? h("span", { class: "tm info" }, sn.tipo) : null,
        sn.bitrate ? h("span", { class: "muted small" }, sn.bitrate) : null, h("span", { class: "muted small" }, sn.desc), h("span", { class: "spacer" }), edLink("no editor", sn.no)),
      sn.aps.length ? tbl(["IED / AccessPoint", "IP", "Máscara", "Gateway", "Outros parâmetros", "GOOSE / SMV"], sn.aps.map(a => h("tr", {},
        h("td", {}, h("b", { class: "mono" }, a.ied), a.ied_existe ? null : h("span", { class: "badge warn", title: "Nenhum IED com este nome no arquivo" }, "IED fora do arquivo"), h("div", { class: "desc mono" }, a.ap)),
        P.map(k => h("td", { class: "mono" }, a.endereco[k] || "—")),
        h("td", { class: "small mono" }, Object.entries(a.endereco).filter(([k]) => !P.includes(k)).map(([k, v]) => h("div", {}, `${k} = ${v}`))),
        h("td", { class: "small" }, a.gse.map(g => h("div", { class: "cv-gse" }, h("b", { class: "mono" }, `${g.tipo} ${g.ldInst}/${g.cbName}`),
          h("div", { class: "mono muted" }, [g.endereco["MAC-Address"], g.endereco.APPID && `APPID ${g.endereco.APPID}`, g.endereco["VLAN-ID"] && `VLAN ${g.endereco["VLAN-ID"]}`,
            g.endereco["VLAN-PRIORITY"] && `prio ${g.endereco["VLAN-PRIORITY"]}`, g.MinTime && `min ${g.MinTime}`, g.MaxTime && `max ${g.MaxTime}`].filter(Boolean).join(" · ")))))))) :
        h("p", { class: "muted small" }, "Sem ConnectedAP."))));
  }

  function controles() {
    if (!ov.controles.length) return empty("Nenhum bloco de controle (ReportControl, GSEControl, SampledValueControl, LogControl).");
    const tipos = [...new Set(ov.controles.map(c => c.tipo))];
    const fTipo = sel([["", "todos os tipos"], ...tipos.map(t => [t, t])], "");
    const lista = h("div");
    let q = "";
    const render = () => {
      const rows = ov.controles.filter(c => (!fTipo.value || c.tipo === fTipo.value) && (!q || `${c.ld}/${c.ln}.${c.nome} ${c.datSet} ${c.desc}`.toLowerCase().includes(q)));
      put(lista, tbl(["", "Bloco", "DataSet", "ID", "Tipo / tempos", "TrgOps", "OptFields", "Clientes / assinantes"], rows.map(c => {
        const a = c.atributos;
        const [lab, k] = CV_CB[c.tipo] || [c.tipo, "el"];
        const membros = h("tr", { hidden: true }, h("td", { colspan: 8 }, dsMembros(c)));
        const tr = h("tr", { style: { cursor: "pointer" }, title: "Clique para ver os membros do DataSet", onclick: () => { membros.hidden = !membros.hidden; } },
          h("td", {}, h("span", { class: "ck ck-" + k }, lab)),
          h("td", {}, h("b", { class: "mono" }, c.nome), h("div", { class: "desc mono" }, `${c.ied}${c.ld}/${c.ln}`), c.desc ? h("div", { class: "desc" }, c.desc) : null),
          h("td", { class: "mono small" }, c.datSet || h("span", { class: "badge warn" }, "sem DataSet"), c.dataset ? h("div", { class: "desc" }, `${c.dataset.membros} membros`) : c.datSet ? h("span", { class: "badge bad" }, "inexistente") : null),
          h("td", { class: "mono small" }, a.rptID ?? a.appID ?? a.smvID ?? "", c.comunicacao ? h("div", { class: "desc" }, [c.comunicacao.endereco["MAC-Address"], c.comunicacao.endereco.APPID && "APPID " + c.comunicacao.endereco.APPID].filter(Boolean).join(" · ")) : null),
          h("td", { class: "small" }, c.tipo === "ReportControl" ? [h("span", { class: "tm " + (isOn(a.buffered) ? "info" : "") }, isOn(a.buffered) ? "BRCB" : "URCB"), " ",
            `bufTime ${a.bufTime ?? "—"} · intgPd ${a.intgPd ?? "—"} · confRev ${a.confRev ?? "—"} · max ${c.max}`] :
            Object.entries(a).filter(([x]) => x !== "appID").map(([x, v]) => `${x}=${v}`).join(" · ")),
          h("td", {}, chips(c.trgops)), h("td", {}, chips(c.optflds)),
          h("td", { class: "small mono" }, [...(c.clientes || []), ...(c.assinantes || [])].join(", ") || h("span", { class: "muted" }, "—")));
        return [tr, membros];
      }).flat(), "cv-tbl"));
    };
    fTipo.addEventListener("change", render);
    render();
    return h("div", { class: "stack" }, h("div", { class: "row" }, fTipo, filtroInput("Filtrar por nome, DataSet ou descrição…", v => { q = v; render(); }),
      h("span", { class: "muted small" }, "Clique numa linha para ver o DataSet.")), lista);
  }

  function dsMembros(c) {
    const ds = ov.datasets.find(d => d.ied === c.ied && d.ld === c.ld && d.ln === c.ln && d.nome === c.datSet);
    if (!ds) return h("span", { class: "muted small" }, "sem DataSet");
    return membrosTable(ds.membros);
  }
  const membrosTable = ms => tbl(["#", "FCDA", "FC", "Referência MMS", "Descrição", ""], ms.map(m => h("tr", {},
    h("td", { class: "mono muted right" }, m.pos + 1), h("td", { class: "mono small" }, m.modelo ? edLink(m.texto, m.modelo) : m.texto),
    h("td", { class: "mono" }, m.fc), h("td", { class: "mono small" }, m.mms), h("td", { class: "small" }, m.desc),
    h("td", {}, m.ok ? null : h("span", { class: "badge bad", title: m.motivo }, "não resolve")))));

  function datasets() {
    if (!ov.datasets.length) return empty("Nenhum DataSet.");
    const lista = h("div", { class: "cv-dslist" });
    const det = h("div", { class: "stack", style: { minWidth: 0 } });
    let atual = store("cv.ds." + sid) || ov.datasets[0].no;
    let q = "";
    const show = d => {
      atual = d.no;
      store("cv.ds." + sid, d.no);
      for (const x of lista.children) x.classList.toggle("on", x.dataset.no === d.no);
      const ruins = d.membros.filter(m => !m.ok).length;
      put(det, h("div", { class: "row between" }, h("div", { class: "row" }, ceKind("ds"), h("h2", { class: "mono", style: { margin: 0 } }, d.nome),
        h("span", { class: "muted" }, d.desc)), edLink("abrir no editor", d.no)),
      h("div", { class: "small" }, `${d.ied}${d.ld}/${d.ln} · ${d.membros.length} membros · usado por: `, d.usado_por.length ? h("b", {}, d.usado_por.join(", ")) : h("span", { class: "badge warn" }, "nenhum bloco de controle")),
      ruins ? h("div", { class: "alert bad" }, `${ruins} membro(s) não resolvem no modelo.`) : null,
      filtroInput("Filtrar membros…", v => { q = v; put(mt, membrosTable(d.membros.filter(m => !q || `${m.texto} ${m.desc} ${m.mms}`.toLowerCase().includes(q)))); }),
      mt);
      const mt0 = membrosTable(d.membros);
      mt.replaceChildren(mt0);
    };
    const mt = h("div");
    put(lista, ov.datasets.map(d => h("div", { class: "tn", "data-no": d.no, onclick: () => show(d) }, ceKind("ds"),
      h("span", { class: "tl" }, d.nome), h("span", { class: "tm" }, d.membros.length), d.membros.some(m => !m.ok) ? h("span", { class: "tm bad" }, "!") : null,
      h("span", { class: "td" }, `${d.ld}/${d.ln}`))));
    show(ov.datasets.find(d => d.no === atual) || ov.datasets[0]);
    return h("div", { class: "cv-split" }, h("div", { class: "card", style: { padding: "6px 0" } }, lista), h("div", { class: "card" }, det));
  }

  function assinaturas() {
    return h("div", { class: "stack" },
      h("h3", {}, `Entradas (Inputs/ExtRef) — ${ov.extrefs.length}`),
      ov.extrefs.length ? tbl(["IED assinante", "LN", "Origem (publicador)", "Serviço", "Bloco", "intAddr", "Descrição", ""], ov.extrefs.map(e => h("tr", {},
        h("td", { class: "mono" }, e.ied), h("td", { class: "mono small" }, `${e.ld}/${e.ln}`), h("td", { class: "mono small" }, edLink(e.origem, e.no)),
        h("td", {}, e.servico), h("td", { class: "mono small" }, e.cb), h("td", { class: "mono small" }, e.intAddr), h("td", { class: "small" }, e.desc),
        h("td", {}, e.ok ? null : h("span", { class: "badge warn" }, "origem fora do arquivo"))))) :
        h("p", { class: "muted small" }, "Nenhum ExtRef. Num CID de um IED só, as assinaturas de GOOSE costumam aparecer só no SCD da subestação."),
      h("h3", {}, `Clientes de report (ClientLN) — ${ov.clientes.length}`),
      ov.clientes.length ? tbl(["RCB", "Cliente", ""], ov.clientes.map(c => h("tr", {}, h("td", { class: "mono small" }, c.rcb), h("td", { class: "mono small" }, edLink(c.cliente, c.no)),
        h("td", {}, c.existe ? null : h("span", { class: "badge neutral" }, "cliente fora do arquivo"))))) :
        h("p", { class: "muted small" }, "Nenhum RptEnabled/ClientLN. O SAGE não precisa estar listado: ele reserva um RCB livre ao conectar."),
      h("h3", {}, "GOOSE publicados"),
      (() => {
        const g = ov.controles.filter(c => c.tipo === "GSEControl");
        return g.length ? tbl(["GoCB", "DataSet", "appID", "MAC / APPID / VLAN", "Assinantes (IEDName)"], g.map(c => h("tr", {},
          h("td", { class: "mono" }, `${c.ld}/${c.ln}.${c.nome}`), h("td", { class: "mono small" }, c.datSet), h("td", { class: "mono small" }, c.atributos.appID || ""),
          h("td", { class: "mono small" }, c.comunicacao ? Object.values(c.comunicacao.endereco).join(" · ") : h("span", { class: "badge warn" }, "sem GSE na comunicação")),
          h("td", { class: "small" }, (c.assinantes || []).join(", ") || "—")))) : h("p", { class: "muted small" }, "Nenhum GSEControl.");
      })());
  }

  function subestacao() {
    if (!ov.subestacao.length) return empty("O arquivo não tem a seção Substation (comum em CIDs de um IED; aparece no SSD/SCD).");
    const node = (n, d = 0) => h("div", { class: "cv-sub", style: { marginLeft: d ? "18px" : 0 } },
      h("div", { class: "row" }, h("span", { class: "ck ck-sub" }, n.tag.replace("ConductingEquipment", "EQP").replace("VoltageLevel", "NT").slice(0, 4).toUpperCase()),
        h("b", { class: "mono" }, n.nome), n.tipo ? h("span", { class: "tm info" }, n.tipo) : null, h("span", { class: "muted small" }, n.desc),
        n.lnodes.map(l => h("span", { class: "tm " + (l.ok ? "val" : "warn"), title: l.ok ? "LN do IED" : "IED não está no arquivo" }, l.ref))),
      n.filhos.map(c => node(c, d + 1)));
    return h("div", { class: "card stack" }, ov.subestacao.map(s => node(s)));
  }

  function comparar() {
    const out = h("div", { class: "stack" });
    const alvo = h("select");
    const semNome = sel([["", "automático"], ["true", "ignorar o nome do IED"], ["false", "considerar o nome do IED"]], "");
    const file = h("input", { type: "file", accept: ".cid,.icd,.scd,.iid,.xml,.sed" });
    (async () => {
      let ieds = [], drafts = [];
      try { [ieds, drafts] = await Promise.all([api("GET", "/ieds").then(r => r.ieds), api("GET", "/cid-editor").then(r => r.rascunhos)]); } catch { /* lista parcial */ }
      put(alvo,
        info.alterado ? h("option", { value: "base" }, "o CID original deste rascunho") : null,
        h("optgroup", { label: "CID dos IEDs" }, ieds.filter(i => `ied-${i.id}` !== sid).map(i => h("option", { value: `ied-${i.id}` }, `${i.nome} — ${i.cid || ""}`))),
        h("optgroup", { label: "Arquivos avulsos" }, drafts.filter(d => d.origem === "arquivo" && d.sessao !== sid).map(d => h("option", { value: d.sessao }, d.nome))));
      if (!alvo.querySelector("option")) {
        alvo.append(h("option", { value: "" }, "— nenhum outro CID —"));
        put(out, h("p", { class: "muted small" }, "Não há outro CID cadastrado nem rascunho deste arquivo. Envie outro arquivo ao lado para comparar."));
      }
    })();
    file.addEventListener("change", async () => {
      if (!file.files[0]) return;
      const fd = new FormData();
      fd.append("arquivo", file.files[0], file.files[0].name);
      try {
        const r = await api("POST", "/cid-editor/files", fd);
        alvo.prepend(h("option", { value: r.sessao, selected: true }, `${r.nome} (enviado agora)`));
        alvo.value = r.sessao;
        go();
      } catch (e) { toast("Arquivo recusado: " + e.message, "bad"); }
    });
    async function go() {
      if (!alvo.value) { toast("Escolha com o que comparar", "bad"); return; }
      put(out, h("div", { class: "muted" }, "comparando…"));
      let r;
      try { r = await api("GET", ceUrl(sid, "/compare", { com: alvo.value, ...(semNome.value ? { sem_nome: semNome.value } : {}) })); }
      catch (e) { put(out, h("div", { class: "alert bad" }, e.message)); return; }
      renderCmp(r);
    }
    function renderCmp(r) {
      const cats = [...new Set(r.resumo.map(x => x.categoria))];
      const MUD = { adicionado: ["ok", "+ só neste"], removido: ["bad", "− só no outro"], alterado: ["warn", "≠ diferente"] };
      const fCat = sel([["", "todas as categorias"], ...cats.map(c => [c, c])], "");
      const fMud = sel([["", "todas as mudanças"], ...Object.entries(MUD).map(([k, [, l]]) => [k, l])], "");
      const lista = h("div", { class: "stack" });
      let q = "";
      const render = () => {
        const xs = r.diferencas.filter(d => (!fCat.value || d.categoria === fCat.value) && (!fMud.value || d.mudanca === fMud.value) &&
          (!q || (d.chave + " " + d.detalhes.join(" ")).toLowerCase().includes(q)));
        put(lista, xs.length ? tbl(["Categoria", "Item", "Mudança", "Detalhes"], xs.slice(0, 1500).map(d => h("tr", {},
          h("td", { class: "small" }, d.categoria), h("td", { class: "mono small" }, d.chave),
          h("td", {}, h("span", { class: "badge " + MUD[d.mudanca][0] }, MUD[d.mudanca][1])),
          h("td", { class: "small mono" }, d.detalhes.map(x => h("div", { class: x.startsWith("+") ? "cv-add" : x.startsWith("−") ? "cv-del" : "" }, x)))))) :
          h("p", { class: "muted" }, "Nada com esse filtro."), xs.length > 1500 ? h("p", { class: "muted small" }, `mostrando 1500 de ${xs.length}`) : null);
      };
      [fCat, fMud].forEach(x => x.addEventListener("change", render));
      put(out,
        h("div", { class: r.total ? "alert info" : "alert info" }, r.total ? `${r.total} diferença(s) entre este arquivo e ${r.com}.` : `Nenhuma diferença estrutural em relação a ${r.com}.`,
          r.sem_nome ? ` Nome do IED ignorado (${r.ieds[1].join(", ")} × ${r.ieds[0].join(", ")}).` : ""),
        h("div", { class: "cv-cmp" }, cats.map(c => h("div", { class: "card", style: { cursor: "pointer" }, onclick: () => { fCat.value = c; render(); } }, h("b", {}, c),
          h("div", { class: "row" }, r.resumo.filter(x => x.categoria === c).map(x => h("span", { class: "badge " + MUD[x.mudanca][0] }, `${MUD[x.mudanca][1].split(" ")[0]} ${x.n}`)))))),
        r.total ? h("div", { class: "row" }, fCat, fMud, filtroInput("Filtrar…", v => { q = v; render(); })) : null, lista);
      render();
    }
    return h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Compara IEDs, LNs, instâncias (DOI/DAI: descrição, valores e sAddr), DataSets (membros e ordem), blocos de controle, ",
        "comunicação e tipos. Útil para conferir duas versões do CID de um relé ou dois relés do mesmo modelo."),
      h("div", { class: "row" }, field("Comparar este arquivo com", alvo), field("Nome do IED", semNome),
        h("button", { class: "primary", style: { alignSelf: "flex-end" }, onclick: go }, "Comparar"),
        isAdmin() ? field("ou enviar outro arquivo", file) : null),
      out);
  }

  function exportar() {
    const X = [["pontos", "Pontos do modelo", "Todo atributo simples (DA/BDA) com FC, bType, CDC, referência MMS, descrição do DOI e valor no CID. Útil para montar a base do SAGE."],
      ["datasets", "Membros dos DataSets", "Cada FCDA com posição, FC, referência MMS, descrição e o bloco de controle que usa o DataSet."],
      ["controles", "Blocos de controle", "RCB, GOOSE, SMV e Log com DataSet, IDs, tempos, TrgOps, OptFields e clientes."],
      ["comunicacao", "Comunicação", "Endereços (IP, máscara, gateway, OSI) e parâmetros GOOSE/SMV por SubNetwork."]];
    return h("div", { class: "grid c2" }, X.map(([k, t, d]) => h("div", { class: "card stack" }, h("h2", {}, t), h("p", { class: "muted small" }, d),
      h("div", {}, h("a", { class: "btn", href: dl(ceUrl(sid, "/export", { tipo: k })) }, "⤓ CSV")))),
    h("div", { class: "card stack" }, h("h2", {}, "Arquivo SCL"), h("p", { class: "muted small" }, info.alterado ? "O rascunho, com as alterações do editor." : "O CID como está."),
      h("div", {}, h("a", { class: "btn", href: dl(ceUrl(sid, "/download")) }, "⤓ CID"))));
  }

  load();
  return { el };
}

// ---------------------------------------------------------------------------- OPMSK (SAGE)
/* Duas colunas: à esquerda o usuário liga os bits e o valor é gerado; à direita digita um valor e vê as opções.
 * As diferenças entre os dois lados aparecem marcadas nas duas colunas. */
const hex24 = s => { const t = String(s || "").trim().replace(/^0x/i, ""); if (!/^[0-9a-f]{1,6}$/i.test(t)) return null; return parseInt(t, 16); };
const fmtHex = n => n.toString(16).toUpperCase();

function pageOpmsk() {
  const el = h("div", { class: "stack" });
  let bits = [];
  let gen = hex24(store("opmsk.gerado")) ?? hex24(S.settings?.sage?.opmsk_padrao) ?? 0x10C830;
  let vis = hex24(store("opmsk")) ?? hex24(S.settings?.sage?.opmsk_padrao) ?? 0x10C830;
  const genOut = h("input", { class: "mono opm-val", readonly: true });
  const visIn = h("input", { class: "mono opm-val", value: fmtHex(vis), spellcheck: false });
  const genList = h("div", { class: "opm-list" });
  const visList = h("div", { class: "opm-list" });
  const cmp = h("div");
  const sug = h("div");
  const soLigados = chk("só os bits ligados", store("opmsk.so") === "1");
  soLigados.input.addEventListener("change", () => { store("opmsk.so", soLigados.input.checked ? "1" : "0"); render(); });

  function copy(txt) {
    const ok = () => toast(`${txt} copiado`, "ok", 2000);
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(txt).then(ok, () => toast("Não deu para copiar; selecione o valor", "bad"));
    else { genOut.select(); try { document.execCommand("copy"); ok(); } catch { toast("Selecione e copie o valor", "info"); } }
  }
  function render() {
    store("opmsk.gerado", fmtHex(gen));
    genOut.value = fmtHex(gen);
    const v = hex24(visIn.value);
    visIn.classList.toggle("bad", v == null);
    if (v != null) { vis = v; store("opmsk", fmtHex(v)); }
    const diff = gen ^ vis;
    put(genList, bits.map(b => {
      const ligado = !!(gen >> b.bit & 1);
      const box = h("input", { type: "checkbox", checked: ligado });
      box.addEventListener("change", () => { gen = box.checked ? gen | (1 << b.bit) : gen & ~(1 << b.bit); render(); });
      return h("label", { class: "opm-row" + (ligado ? " on" : "") + (diff >> b.bit & 1 ? " dif" : "") + (b.efeito === "Sem uso" ? " unused" : "") },
        box, h("span", { class: "opm-bit mono" }, b.bit, h("small", {}, b.opt)),
        h("span", { class: "opm-txt" }, h("b", {}, b.efeito), b.detalhe ? h("small", {}, b.detalhe) : null),
        h("span", { class: "opm-hex mono", title: "Valor do bit" }, b.valor));
    }));
    const lig = bits.filter(b => vis >> b.bit & 1);
    const mostrar = soLigados.input.checked ? lig : bits;
    put(visList, v == null ? h("div", { class: "alert bad" }, "Valor inválido: use até 6 dígitos hexadecimais (24 bits), ex.: 10C830.") :
      mostrar.length ? mostrar.map(b => {
        const ligado = !!(vis >> b.bit & 1);
        return h("div", { class: "opm-row" + (ligado ? " on" : " off") + (diff >> b.bit & 1 ? " dif" : "") },
          h("span", { class: "opm-st" }, ligado ? h("span", { class: "badge ok" }, "ligado") : h("span", { class: "muted small" }, "desligado")),
          h("span", { class: "opm-bit mono" }, b.bit, h("small", {}, b.opt)),
          h("span", { class: "opm-txt" }, h("b", {}, b.efeito), b.detalhe && ligado ? h("small", {}, b.detalhe) : null),
          h("span", { class: "opm-hex mono" }, b.valor));
      }) : h("p", { class: "muted" }, "Nenhum bit ligado: o SAGE usa o comportamento padrão em tudo."));
    const difs = bits.filter(b => diff >> b.bit & 1);
    put(cmp, v == null ? null : h("div", { class: "alert " + (difs.length ? "warn" : "info") },
      difs.length ? [h("b", {}, `${difs.length} bit(s) diferentes`), ` entre o gerado (${fmtHex(gen)}) e o informado (${fmtHex(vis)}): `,
        difs.map((b, i) => [i ? "; " : "", h("b", {}, `bit ${b.bit}`), ` ${b.efeito} — ${gen >> b.bit & 1 ? "só no gerado" : "só no informado"}`])]
        : `Os dois valores são iguais (${fmtHex(gen)}).`));
  }
  visIn.addEventListener("input", render);

  async function sugerir() {
    let ieds = [];
    try { ieds = (await api("GET", "/ieds")).ieds.filter(i => i.ied_scl); } catch { return; }
    if (!ieds.length) { toast("Nenhum IED cadastrado", "bad"); return; }
    const s = sel(ieds.map(i => [i.id, `${i.nome} — ${i.cid || ""}`]), ieds[0].id);
    const r = await modal("Sugerir bits a partir do CID de um IED", h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Ajusta só os bits que dependem do CID (DataSets dinâmicos e posição dos RCBs) e mostra observações sobre BRCB/URCB. Os demais ficam como estão."),
      field("IED", s)), [{ label: "Cancelar", value: null }, { label: "Sugerir", cls: "primary", click: () => api("GET", `/opmsk/sugestao?ied=${s.value}`).catch(e => { toast(e.message, "bad"); return false; }) }]);
    if (!r) return;
    for (const b of r.bits) if (b.ligar === true) gen |= 1 << b.bit; else if (b.ligar === false) gen &= ~(1 << b.bit);
    put(sug, h("div", { class: "card stack" }, h("div", { class: "row between" }, h("b", {}, `Sugestão para ${r.ied} (${r.ied_scl})`), h("button", { class: "sm ghost", onclick: () => put(sug) }, "✕")),
      h("div", { class: "small muted" }, `Edição ${r.edicao} · ${r.rcbs} RCB(s): ${r.brcb} BRCB, ${r.urcb} URCB · DataSets dinâmicos: ${r.dynamic ? "sim" : "não"}`),
      h("ul", { class: "list" }, r.bits.map(b => h("li", {}, h("b", {}, `bit ${b.bit} `),
        b.ligar === true ? h("span", { class: "badge ok" }, "ligado") : b.ligar === false ? h("span", { class: "badge neutral" }, "desligado") : h("span", { class: "badge warn" }, "avaliar"),
        " ", b.motivo)))));
    render();
  }

  (async () => {
    try { bits = (await api("GET", "/opmsk/bits")).bits; } catch (e) { put(el, h("div", { class: "alert bad" }, e.message)); return; }
    const padrao = hex24(S.settings?.sage?.opmsk_padrao) ?? 0x10C830;
    put(el,
      h("div", { class: "row between" }, h("h1", {}, "OPMSK do SAGE"),
        h("span", { class: "muted small" }, "Token OPMSK do atributo CONFIG da entidade CNF (Anexo 17 do Manual de Configuração, OPT0 a OPT23)")),
      cmp,
      h("div", { class: "opm-cols" },
        h("div", { class: "card stack" },
          h("div", { class: "row between" }, h("h2", { style: { margin: 0 } }, "Gerar"), h("span", { class: "muted small" }, "marque as opções")),
          h("div", { class: "row" }, genOut, h("button", { class: "primary", onclick: () => copy(fmtHex(gen)) }, "Copiar"),
            h("button", { class: "sm", onclick: () => { gen = 0; render(); } }, "Zerar"),
            h("button", { class: "sm", title: "Valor padrão definido em Configuração", onclick: () => { gen = padrao; render(); } }, `Padrão (${fmtHex(padrao)})`),
            h("button", { class: "sm", title: "Copia o valor informado à direita para o gerador", onclick: () => { gen = vis; render(); } }, "← Usar o da direita"),
            h("button", { class: "sm", onclick: sugerir }, "Sugerir pelo IED…")),
          sug, genList),
        h("div", { class: "card stack" },
          h("div", { class: "row between" }, h("h2", { style: { margin: 0 } }, "Visualizar"), h("span", { class: "muted small" }, "digite o valor do CNF")),
          h("div", { class: "row" }, visIn, h("button", { class: "sm", title: "Copia o valor gerado à esquerda para cá", onclick: () => { visIn.value = fmtHex(gen); render(); } }, "Usar o gerado →"), soLigados.el),
          visList)));
    render();
  })();
  return { el };
}

// ---------------------------------------------------------------------------- configuração
const TEMAS = {
  auto: ["Automático", "segue o sistema operacional", '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/></svg>'],
  claro: ["Claro", "fundo claro", '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'],
  escuro: ["Escuro", "fundo escuro", '<svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>'],
};
const temaAtual = () => (TEMAS[store("tema")] ? store("tema") : "auto");
function applyTheme() {
  const t = temaAtual();
  if (t === "claro" || t === "escuro") document.documentElement.dataset.theme = t === "claro" ? "light" : "dark";
  else delete document.documentElement.dataset.theme;
  const ordem = ["auto", "claro", "escuro"];
  const prox = ordem[(ordem.indexOf(t) + 1) % 3];
  for (const b of document.querySelectorAll(".js-tema")) {
    b.innerHTML = TEMAS[t][2];
    b.title = `Tema: ${TEMAS[t][0].toLowerCase()} (${TEMAS[t][1]}). Clique para ${TEMAS[prox][0].toLowerCase()}.`;
  }
  for (const b of document.querySelectorAll("[data-tema]")) b.classList.toggle("on", b.dataset.tema === t);
}
function setTema(t) { store("tema", t); applyTheme(); }
function cycleTema() { const o = ["auto", "claro", "escuro"]; setTema(o[(o.indexOf(temaAtual()) + 1) % 3]); }
const temaSeg = () => h("div", { class: "theme-seg" }, Object.entries(TEMAS).map(([k, [l]]) => {
  const b = h("button", { type: "button", "data-tema": k, class: k === temaAtual() ? "on" : "", onclick: () => setTema(k) }, l);
  b.insertAdjacentHTML("afterbegin", TEMAS[k][2]);
  return b;
}));

/* Menu do usuário no topo: quem está logado, papel, tema, configuração e sair. */
function renderUserMenu() {
  const um = $("#um");
  if (!S.eu) { um.classList.add("hidden"); return; }
  const nome = S.info?.autenticacao ? S.eu.usuario : "local";
  const papel = { administrador: "Administrador", operacao: "Operação", leitura: "Leitura" }[S.eu.papel] || S.eu.papel;
  const av = () => h("span", { class: "um-av" }, nome.slice(0, 2));
  const pop = h("div", { class: "um-pop", hidden: true },
    h("div", { class: "um-head" }, av(), h("div", {}, h("b", {}, nome), h("small", {}, papel + ({ administrador: " · altera toda a configuração",
      operacao: " · opera os IEDs", leitura: " · somente consulta" }[S.eu.papel] || "")))),
    h("a", { class: "um-item", href: "#/config" }, "Configuração e minha conta"),
    h("div", { class: "um-lbl" }, "Tema"), temaSeg(),
    h("div", { class: "um-sep" }),
    S.info?.autenticacao ? h("button", { class: "um-item danger", type: "button", onclick: sair }, "Sair do sistema")
      : h("div", { class: "um-note" }, "Sem login: a interface só escuta em 127.0.0.1 e todos entram como administrador."));
  const btn = h("button", { class: "um-btn", type: "button", "aria-haspopup": "menu", title: `${nome} · ${papel}` }, av(),
    h("span", { class: "um-txt" }, h("b", {}, nome), h("small", {}, papel)));
  btn.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>');
  btn.addEventListener("click", e => { e.stopPropagation(); pop.hidden = !pop.hidden; });
  pop.addEventListener("click", e => { if (e.target.closest("a, .um-item")) pop.hidden = true; });
  put(um, btn, pop);
  um.classList.remove("hidden");
  applyTheme();
}
document.addEventListener("click", e => { const p = $("#um .um-pop"); if (p && !p.hidden && !e.target.closest("#um")) p.hidden = true; });
document.addEventListener("keydown", e => { if (e.key === "Escape") { const p = $("#um .um-pop"); if (p) p.hidden = true; } });

async function sair() {
  if (!await confirmDlg("Sair do sistema? Os IEDs continuam no estado em que estão.", "Sair")) return;
  try { await api("POST", "/logout"); } catch { /* o token já pode ter expirado */ }
  try { sessionStorage.setItem("simied.saiu", "1"); } catch { /* sem sessionStorage */ }
  S.token = "";
  store("token", "");
  location.hash = "#/";
  location.reload();
}

function pageConfig() {
  const el = h("div", { class: "stack" }, h("h1", {}, "Configuração"));
  const admin = isAdmin();
  const sec = (titulo, desc, ...kids) => h("div", { class: "card stack" }, h("h2", {}, titulo), desc ? h("p", { class: "muted small", style: { margin: 0 } }, desc) : null, ...kids);
  const prefs = h("div"), srv = h("div"), conta = h("div"), users = h("div"), sys = h("div");
  add(el, [h("div", { class: "grid c2" }, prefs, srv), h("div", { class: "grid c2" }, conta, sys), users]);

  // ------------------------------------------------------------ preferências deste navegador
  const tema = temaSeg();
  const tz = sel([["local", "Local primeiro"], ["utc", "UTC primeiro"]], S.tz);
  tz.addEventListener("change", () => { S.tz = tz.value; store("tz", S.tz); $("#tzpref").value = S.tz; });
  const menu = chk("Menu lateral recolhido (só ícones)", document.body.classList.contains("side-min"));
  menu.input.addEventListener("change", () => { document.body.classList.toggle("side-min", menu.input.checked); store("side.min", menu.input.checked ? "1" : "0"); });
  put(prefs, sec("Preferências deste navegador", "Valem só para você, neste navegador.",
    h("div", { class: "form" }, h("div", { class: "f" }, h("span", { class: "small muted" }, "Tema"), tema), field("Horário principal nas tabelas", tz)), menu.el,
    h("button", { class: "sm", style: { alignSelf: "flex-start" }, onclick: () => {
      try { for (const k of Object.keys(localStorage)) if (k.startsWith("simied.cols.") || k.startsWith("simied.ce.") || k.startsWith("simied.cv.")) localStorage.removeItem(k); } catch { /* sem localStorage */ }
      toast("Larguras de colunas e posições do editor voltaram ao padrão", "ok");
    } }, "Restaurar larguras de colunas e painéis")));

  // ------------------------------------------------------------ padrões do servidor
  (async () => {
    let cfg;
    try { cfg = await api("GET", "/settings"); await loadProfiles(); } catch (e) { put(srv, sec("Padrões do simulador", null, h("div", { class: "alert bad" }, e.message))); return; }
    const perfil = sel([["auto", "Automático (pelo fabricante e edição do CID)"], ...S.profiles.map(p => [p.id, `${p.id} — ${p.nome || ""}`])], cfg.novo_ied.perfil, { disabled: !admin });
    const porta = h("input", { type: "number", class: "num", value: cfg.novo_ied.porta, disabled: !admin });
    const maxa = h("input", { type: "number", class: "num", value: cfg.novo_ied.max_associacoes, disabled: !admin });
    const opm = h("input", { class: "mono", value: cfg.sage.opmsk_padrao, disabled: !admin });
    const vista = sel([["modelo", "Modelo"], ["tipos", "Tipos"], ["xml", "XML"]], cfg.editor.vista_inicial, { disabled: !admin });
    put(srv, sec("Padrões do simulador", admin ? "Valem para todos os usuários." : "Só o perfil administrador altera.",
      h("h3", {}, "Novo IED"), h("div", { class: "form" }, field("Perfil", perfil, "wide"), field("Porta", porta), field("Máx. associações", maxa)),
      h("h3", {}, "SAGE e editor"), h("div", { class: "form" }, field("OPMSK padrão (hex)", opm), field("Visão inicial do editor de CID", vista)),
      admin ? h("div", {}, h("button", { class: "primary", onclick: async () => {
        const r = await send("PUT", "/settings", { novo_ied: { perfil: perfil.value, porta: +porta.value, max_associacoes: +maxa.value },
          sage: { opmsk_padrao: opm.value }, editor: { vista_inicial: vista.value } });
        if (r) { S.settings = r; opm.value = r.sage.opmsk_padrao; toast("Padrões gravados", "ok"); }
      } }, "Salvar padrões")) : null));
  })();

  // ------------------------------------------------------------ minha conta
  const atual = h("input", { type: "password", autocomplete: "current-password" });
  const nova = h("input", { type: "password", autocomplete: "new-password" });
  const nova2 = h("input", { type: "password", autocomplete: "new-password" });
  put(conta, sec("Minha conta", null,
    h("dl", { class: "kv" }, h("dt", {}, "Usuário"), h("dd", {}, S.eu?.usuario || "—"), h("dt", {}, "Papel"), h("dd", {}, S.eu?.papel || "—")),
    S.info?.autenticacao ? [h("h3", {}, "Trocar a senha"), h("div", { class: "form" }, field("Senha atual", atual), field("Nova senha (mín. 8)", nova), field("Repita", nova2)),
      h("div", {}, h("button", { onclick: async () => {
        if (nova.value !== nova2.value) { toast("As senhas novas não conferem", "bad"); return; }
        if (await send("POST", "/me/password", { atual: atual.value, nova: nova.value })) { toast("Senha trocada", "ok"); atual.value = nova.value = nova2.value = ""; }
      } }, "Trocar senha"))] :
      h("p", { class: "muted small" }, "A interface só escuta em 127.0.0.1, então não há login: todos entram como administrador.")));

  // ------------------------------------------------------------ usuários e sistema (administrador)
  if (!admin) {
    put(users);
    put(sys, sec("Sistema", null, h("p", { class: "muted small" }, "Informações do servidor, usuários e backup ficam com o perfil administrador.")));
    return { el };
  }
  async function loadUsers() {
    let r;
    try { r = await api("GET", "/users"); } catch (e) { put(users, sec("Usuários locais", null, h("div", { class: "alert bad" }, e.message))); return; }
    const nome = h("input", { placeholder: "usuário", autocomplete: "off" });
    const senha = h("input", { type: "password", placeholder: "senha (mín. 8)", autocomplete: "new-password" });
    const papel = sel([["operacao", "operação"], ["leitura", "leitura"], ["administrador", "administrador"]], "operacao");
    put(users, sec("Usuários locais", "Leitura só consulta; operação opera os IEDs (pontos, falhas, relógio, cenários, ensaios); administrador também altera a configuração. Mudar o papel ou a senha encerra os logins abertos daquele usuário.",
      r.usuarios.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Usuário", "Papel", "Nova senha", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, r.usuarios.map(u => {
          const p = sel([["leitura", "leitura"], ["operacao", "operação"], ["administrador", "administrador"]], u.papel);
          const pw = h("input", { type: "password", placeholder: "deixe vazio para manter", autocomplete: "new-password" });
          return h("tr", {}, h("td", {}, h("b", {}, u.usuario), u.usuario === r.eu ? h("span", { class: "badge info", style: { marginLeft: "6px" } }, "você") : null),
            h("td", {}, p), h("td", {}, pw),
            h("td", { class: "nowrap right" },
              h("button", { class: "sm", onclick: async () => {
                const body = { papel: p.value };
                if (pw.value) body.senha = pw.value;
                if (await send("PATCH", `/users/${encodeURIComponent(u.usuario)}`, body)) { toast(`${u.usuario} atualizado`, "ok"); loadUsers(); }
              } }, "Salvar"), " ",
              h("button", { class: "sm danger", disabled: u.usuario === r.eu, onclick: async () => {
                if (!await confirmDlg(`Remover o usuário ${u.usuario}? Os logins abertos dele são encerrados.`, "Remover", true)) return;
                if (await send("DELETE", `/users/${encodeURIComponent(u.usuario)}`)) { toast("Usuário removido", "ok"); loadUsers(); }
              } }, "Remover")));
        })))) : h("p", { class: "muted small" }, "Nenhum usuário local."),
      h("h3", {}, "Novo usuário"),
      h("div", { class: "row" }, nome, senha, papel, h("button", { class: "primary", onclick: async () => {
        if (await send("POST", "/users", { usuario: nome.value.trim(), senha: senha.value, papel: papel.value })) { toast("Usuário criado", "ok"); loadUsers(); }
      } }, "Criar"))));
  }
  async function loadSys() {
    let i;
    try { i = await api("GET", "/system"); } catch (e) { put(sys, sec("Sistema", null, h("div", { class: "alert bad" }, e.message))); return; }
    const d = i.disco;
    put(sys, sec("Sistema", null,
      h("dl", { class: "kv" },
        h("dt", {}, "Versão"), h("dd", {}, i.versao),
        h("dt", {}, "Interface"), h("dd", { class: "mono" }, i.admin),
        h("dt", {}, "Pasta de dados"), h("dd", { class: "mono small" }, i.dados),
        h("dt", {}, "IEDs"), h("dd", {}, `${i.ieds} (${i.rodando} rodando)`),
        h("dt", {}, "Login"), h("dd", {}, i.autenticacao ? `obrigatório · ${i.sessoes_abertas} sessão(ões) aberta(s)` : "desligado (só 127.0.0.1)"),
        h("dt", {}, "LDAP"), h("dd", {}, i.ldap ? h("span", { class: "mono small" }, `${i.ldap.url} · ${i.ldap.base}${i.ldap.tls ? "" : " · SEM TLS"}`) : "desligado"),
        h("dt", {}, "Teste local"), h("dd", {}, i.teste_local ? "ligado (IEDs em 127.0.0.1)" : "desligado"),
        h("dt", {}, "Disco"), h("dd", {}, `${fmtSize(d.total)} — banco ${fmtSize(d.banco)}, CIDs ${fmtSize(d.cids)}, rascunhos ${fmtSize(d.editor)}, perfis ${fmtSize(d.perfis)}`)),
      h("p", { class: "muted small" }, "Endereço, porta e LDAP vêm do arquivo simied.conf; depois de mudar, reinicie o serviço (sudo systemctl restart simied)."),
      h("h3", {}, "Backup"),
      h("p", { class: "muted small", style: { margin: 0 } }, "Banco (cópia consistente), CIDs, perfis, rascunhos do editor e config.json num ZIP. Para restaurar: pare o serviço e extraia na pasta de dados."),
      h("div", {}, h("a", { class: "btn primary", href: dl("/backup") }, "⤓ Baixar backup"))));
  }
  loadUsers();
  loadSys();
  return { el };
}

function setupSide() {
  const mobile = () => matchMedia("(max-width: 900px)").matches;
  if (store("side.min") === "1") document.body.classList.add("side-min");
  $("#menu-btn").addEventListener("click", () => {
    if (mobile()) document.body.classList.toggle("side-open");
    else store("side.min", document.body.classList.toggle("side-min") ? "1" : "0");
  });
  $("#side-backdrop").addEventListener("click", () => document.body.classList.remove("side-open"));
  $("#nav").addEventListener("click", e => { if (e.target.closest("a")) document.body.classList.remove("side-open"); });
  const fit = () => document.documentElement.style.setProperty("--simbar", $("#simbar").offsetHeight + "px");
  fit();
  window.addEventListener("resize", fit);
  if (window.ResizeObserver) new ResizeObserver(fit).observe($("#simbar"));
}

// ---------------------------------------------------------------------------- inicialização
function tickHeader() {
  const n = nowSrv();
  $("#clk-local").textContent = fmtMs(n, false);
  $("#clk-utc").textContent = fmtMs(n, true);
}

async function boot2() {
  try { S.ieds = (await api("GET", "/ieds")).ieds; } catch { return; }
  try { S.settings = await api("GET", "/settings"); } catch { S.settings = null; }
  try {
    S.eu = await api("GET", "/eu");
    renderUserMenu();
  } catch { /* versão antiga do servidor */ }
  updateSimbar();
  loadProfiles();
  route();
}

async function boot() {
  applyTheme();
  setupSide();
  $("#tzpref").value = S.tz;
  $("#tzpref").addEventListener("change", e => { S.tz = e.target.value; store("tz", S.tz); route(); });
  $("#tema-btn").classList.add("js-tema");
  $("#tema-btn").addEventListener("click", cycleTema);
  applyTheme();
  if (window.matchMedia) matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", applyTheme);
  $("#panic").addEventListener("click", async () => {
    if (!await confirmDlg("PARADA RÁPIDA\n\nPara todos os IEDs, fecha todas as associações com os SCADAs, cancela cenários em execução e devolve os pontos ao estado de antes da partida (RN-SEG-05).", "Parar tudo", true)) return;
    const r = await send("POST", "/panic");
    if (r) toast(`${r.parados.length} IED(s) parado(s): ` + r.parados.map(x => `${x.ied} (${x.pontos_restaurados} pontos)`).join(", "), "bad");
  });
  window.addEventListener("hashchange", route);
  setInterval(tickHeader, 200);
  try {
    const t0 = Date.now();
    S.info = await api("GET", "/info");
    S.skew = S.info.hora_utc * 1000 - (t0 + Date.now()) / 2;
  } catch (e) {
    put($("#app"), h("div", { class: "alert bad" }, "Simulador inacessível: " + e.message));
    return;
  }
  tickHeader();
  if (S.info.autenticacao && !S.token) { showLogin(); return; }
  wsConnect();
  boot2();
}

boot();
