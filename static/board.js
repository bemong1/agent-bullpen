'use strict';
// ---------- common ----------
const $ = (s, el = document) => el.querySelector(s);
const store = {
  get(k, d) { try { const v = localStorage.getItem('ab.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('ab.' + k, JSON.stringify(v)); } catch {} },
};
const qs = new URLSearchParams(location.search);
let SESSION = qs.get('session') || '';
let S = null, serverSkew = 0;
const OFFICE_OPEN_MIN = 768;   // the office card starts open from this window width (a tablet), folded below it (a phone), until the user picks: the pick is kept
// Screen state (ui): every field is declared here, once (properties are not added later)
const ui = {
  // Saved choices (localStorage)
  debate: store.get('debate', null), agentFilter: store.get('agentFilter', 'debate'),
  feedFilter: store.get('feedFilter', 'all'), tlWin: store.get('tlWin', 'debate'), tlEnd: store.get('tlEnd', null), tlFrom: store.get('tlFrom', null), tlTo: store.get('tlTo', null),
  gameOpen: store.get('gameOpen', innerWidth >= OFFICE_OPEN_MIN),
  // Drawer, feed and agent list
  drawer: null, dTab: 'overview', openEvents: new Set(), oldOpen: false, unlinkedOpen: false, tlAll: false, tlSig: '', tlPicked: '', tlToShown: '',
  // Open modal: 'file' (document) | 'talk' (conversation: one message opened by its event number, or the whole conversation) | 'diag' (the diagnostics list) | null
  modalKind: null, modalReturn: [],
  // User ↔ orchestrator conversation
  talk: [], talkMore: false, talkSig: '', talkLoaded: false, talkStick: true,
  // Agent talk (orchestrator ↔ agent, agent ↔ agent): atalkHtml = the list HTML as last drawn, atalkLast = the last visible event number, atalkErr = loading failed
  atalk: [], atalkMore: false, atalkHtml: '', atalkLast: -1, atalkLoaded: false, atalkErr: false, atalkStick: true, atalkFilter: ['all', 'orch', 'peer'].includes(store.get('atalkFilter', 'all')) ? store.get('atalkFilter', 'all') : 'all',
  // Alerts (demoAlert: the fake alert the demo raises)
  openAlerts: new Set(), alertsOpen: false, alertIds: '', demoAlert: null,
};
const STATUSES = ['running', 'stalled', 'done', 'failed', 'killed', 'ended', 'interrupted', 'unknown'];
const statusLabel = s => STATUSES.includes(s) ? t('status.' + s) : s;   // an unknown status shows as it came
// The state of an agent as the screen words it: its status, or status + reason where the dictionary has a word for the pair (interrupted + limit = "Limit reached")
const stateLabel = a => a.reason && I18N.has('status.' + a.status + '.' + a.reason) ? t('status.' + a.status + '.' + a.reason) : statusLabel(a.status);
// Why it is in that state, in a sentence (status.why.<status>[.<reason>]); '' where the dictionary has none (a working or finished agent needs no explanation)
const stateWhy = a => { const k = [a.reason ? 'status.why.' + a.status + '.' + a.reason : '', 'status.why.' + a.status].find(x => x && I18N.has(x)); return k ? t(k) : ''; };
const STATUS_ORDER = { running: 0, stalled: 1, interrupted: 2, unknown: 3, failed: 4, killed: 4, done: 5, ended: 6 };
const statusRank = st => st in STATUS_ORDER ? STATUS_ORDER[st] : 9;       // a status this page does not know goes last (never NaN in a sort)
const now = () => Date.now() / 1000 + serverSkew;
function ago(ts) {
  if (!ts) return '';
  const d = Math.max(0, now() - ts);
  if (d < 10) return t('time.now');
  if (d < 60) return t('time.ago.sec', { n: Math.floor(d) });
  if (d < 3600) return t('time.ago.min', { n: Math.floor(d / 60) });
  if (d < 86400) { const h = Math.floor(d / 3600), m = Math.floor(d / 60) % 60; return m ? t('time.ago.hourMin', { h, m }) : t('time.ago.hour', { h }); }
  return t('time.ago.day', { n: Math.floor(d / 86400) });
}
function dur(sec) {
  sec = Math.max(0, sec | 0);
  if (sec < 60) return t('time.dur.sec', { n: sec });
  if (sec < 3600) return t('time.dur.min', { n: Math.floor(sec / 60) });
  if (sec < 86400) return t('time.dur.hourMin', { h: Math.floor(sec / 3600), m: Math.floor(sec / 60) % 60 });
  return t('time.dur.dayHour', { d: Math.floor(sec / 86400), h: Math.floor(sec / 3600) % 24 });
}
const monthDay = ts => { const d = new Date(ts * 1000); return t('time.monthDay', { m: d.getMonth() + 1, d: d.getDate(), mon: I18N.date(ts, 'monthShort') }); };
const dayTime = ts => t('time.dayTime', { date: monthDay(ts), time: I18N.date(ts, 'time') });          // the date and the time of day, in the words of the language
function hm(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), n = new Date(now() * 1000);
  return d.toDateString() === n.toDateString() ? I18N.date(ts, 'time') : dayTime(ts);
}
const kfmt = n => n >= 1e9 ? (n / 1e9).toFixed(2) + 'B' : n >= 1e8 ? Math.round(n / 1e6) + 'M' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n || 0);
const TOK_KEYS = ['input', 'cache_write', 'cache_read', 'output', 'calls', 'adv_input', 'adv_output', 'adv_calls',
  'cost', 'cost_input', 'cost_write', 'cost_read', 'cost_output', 'adv_cost', 'unpriced', 'reasoning'];
const modelPretty = m => m.replace(/^opus-/, 'Opus ').replace(/^fable-/, 'Fable ').replace(/^sonnet-/, 'Sonnet ').replace(/^haiku-/, 'Haiku ').replace(/^mythos-/, 'Mythos ').replace(/(\d)-(\d)/g, '$1.$2');
const tokIn = tk => tk ? tk.input + tk.cache_write + tk.cache_read : 0;
// Cost per model of one token dict as [{ model, advisor, cost }]: the server's model_costs; an older server only has models, whose advisor entries carry a Korean key suffix
const ADV_SUFFIX = ' 조언';   // i18n-ok: the older server's key suffix, read here only
const modelCosts = tk => Array.isArray(tk && tk.model_costs) ? tk.model_costs
  : Object.entries((tk && tk.models) || {}).map(([k, cost]) => k.endsWith(ADV_SUFFIX) ? { model: k.slice(0, -ADV_SUFFIX.length), advisor: true, cost } : { model: k, advisor: false, cost });
const modelLabel = mc => mc.advisor ? t('board.model.advisor', { model: modelPretty(mc.model) }) : modelPretty(mc.model);
function tokSum(list) {
  const r = Object.fromEntries(TOK_KEYS.map(k => [k, 0])); r.ctx = 0; r.n = list.length;
  r.models = {};
  list.forEach(tk => { if (!tk) return; TOK_KEYS.forEach(k => r[k] += tk[k] || 0); r.ctx += tk.ctx || 0;
    modelCosts(tk).forEach(mc => { const k = (mc.advisor ? 'adv|' : '|') + mc.model; r.models[k] = { model: mc.model, advisor: !!mc.advisor, cost: (r.models[k] ? r.models[k].cost : 0) + mc.cost }; }); });
  return r;
}
const modelName = m => (m || '').replace(/^claude-/, '').replace(/-(\d)-(\d)/, ' $1.$2').replace(/-(\d+)$/, ' $1').replace(/\[1m\]/, '');
const agentById = id => S && S.agents.find(a => a.id === id);
// The summary of a tool call: the board's own wording comes as a dictionary key (words of the page's language); anything else is the agent's data as it was.
const toolText = x => x.text_i18n && I18N.has(x.text_i18n.key) ? t(x.text_i18n.key, i18nParams(x.text_i18n.params)) : x.text;
const isLive = a => a.status === 'running' || a.status === 'stalled';              // Working (suspected stalls included). The test for running only and the "within 45 s" test are used separately
const isHeld = a => a.status === 'interrupted' || a.status === 'unknown';          // Not working and not over: stopped by a limit or an error, or not known. They stay in the main list, not under "finished"
const under = (u, d) => u === d.root || u.startsWith(d.root + '/');
// An agent the judgment only guesses to work in a debate (an estimate, with no cell there) is shown as that: it is not counted with the ones that were seen working there
const guessedIn = (a, d) => !!a.placed && a.placed.sure === false && under(a.placed.unit || '', d) && !a.units.some(u => under(u, d));
const inDebate = (a, d) => a.units.some(u => under(u, d)) || ((a.work_units || []).some(u => under(u, d)) && !guessedIn(a, d)) ||   // Agents that worked in this debate's folder, or that the judgment ties to it (a tag, a request to save)
  d.topics.some(tp => tp.room === 'members' && tp.rows.some(r => r.agents.includes(a.id)));         // and those of a room of participants only (they hold no seat)
// A folder of people who work together (a room) is "work", not a debate: the words that name the unit follow it
const isRoom = d => !!d && d.topics.length > 0 && d.topics.every(tp => tp.room);
// A final is confirmed when the judgment says so (`confirmed`; `exists` is its alias for one release). Anything else is "closing not confirmed", with the documents that could be it
const finalOk = f => !!f && (f.confirmed !== undefined ? f.confirmed : f.exists);
// A topic or a room may be closed (the office closes its room, the stage reads "Done") only when the judgment says it can: a confirmed final and nobody working, a room that is not an estimate
const isClosable = tp => tp.closable !== undefined ? !!tp.closable : finalOk(tp.final);
// A topic that is closable though its own final is not confirmed is closed by the final of its bundle (J16)
const bundleClosed = tp => !tp.room && !finalOk(tp.final) && tp.closable === true;
// The name of whoever wrote or fixed a file: an agent, or the orchestrator
const whoName = id => id === 'orch' ? t('common.orchestrator.short') : agentName(id);
// Tab row: items = [[key, text], …], cur = the key that is on, onPick(key)
function tabBar(el, items, cur, onPick) {
  el.innerHTML = items.map(([k, n]) => `<button class="tab ${cur === k ? 'on' : ''}" data-k="${k}">${n}</button>`).join('');
  el.querySelectorAll('.tab').forEach(b => b.onclick = () => onPick(b.dataset.k));
}
// Provider (Claude / Codex). A session with only Claude adds no mark for either
const isCx = x => !!x && x.provider === 'codex';
const orchCx = () => !!S && isCx(S.orch);
const cxCls = x => isCx(x) ? ' cx' : '';                       // Status dot: diamond
const CXG = '<span class="cxg">◆</span>';
const cxMark = a => isCx(a) && !orchCx() ? CXG : '';            // Mark before the name: only when the provider differs from the orchestrator's
// Provider and model on one line (debate table, "Other work" card): Claude Code · Opus 5.5 · xhigh / ◆ Codex · gpt-6.1-sol · high
const prettyModel = m => { m = (m || '').replace(/\[1m\]/, ''); return /^claude-/.test(m) ? modelPretty(m.replace(/^claude-/, '').replace(/-\d{8}$/, '')) : m; };
const provLine = a => `<div class="prov-line" title="${esc(a.model || '')}${a.effort ? ' · ' + esc(a.effort) : ''}"><span class="pk">${isCx(a) ? CXG + ' Codex' : 'Claude Code'}</span>${a.model ? ' · ' + esc(prettyModel(a.model)) : ''}${a.effort ? ' · ' + esc(a.effort) : ''}</div>`;
const tsOf = v => typeof v === 'string' ? Date.parse(v) / 1000 : v;
const agentName = id => { const a = agentById(id); return a ? (a.tag || a.title) : (S && S.names[id]) || (id || '').slice(0, 8); };
// Name label shared by the message feed and the agent talk: user, orchestrator, agent (a click opens the drawer)
const whoBadge = id => id === 'user' ? `<span class="who-b user">${t('common.you')}</span>` : id === 'orch' ? `<span class="who-b orch">${t('common.orchestrator')}</span>`
  : `<span class="who-b agent" data-agent="${esc(id)}">${cxMark(agentById(id))}${esc(agentName(id))}</span>`;
// An agent the server linked to its session only by a guess (link.certain === false) gets a small "guess" mark; the tooltip says how the guess was made
// A `content_short` guess has three reasons, and the page names the one the server found: a short instruction, a text comparison that hit its size limit (incomplete), or a launching script that could not be read (assumed)
const ruleWhy = (rule, l) => rule === 'content_short' ? rule + (l.incomplete ? '.incomplete' : l.assumed ? '.assumed' : '') : rule;
const linkTip = l => { const r = ruleWhy(l.rule, l); return I18N.has('board.link.tip.' + r) ? t('board.link.tip.' + r) : t('board.link.tip.other'); };
// certain | guess | null: rule_class when the server sends it, else the older certain flag
const linkClass = l => !l ? null : l.rule_class === 'certain' || l.rule_class === 'guess' ? l.rule_class : l.certain === false ? 'guess' : l.certain === true ? 'certain' : null;
const guessMark = a => a && linkClass(a.link) === 'guess' ? `<span class="guess" title="${esc(linkTip(a.link))}">${t('board.link.guess')}</span>` : '';
// A title or text the server wrote: when it sent the new fields the screen builds it in its own language (the dictionary key must exist), else the old Korean text as it came
// The parameters that are epoch seconds (null = none, shown as '-') are worded here in the screen's own zone and language
const TIME_PARAMS = ['at', 'as_of', 'resets_at'];
const i18nParams = p => { const o = Object.assign({}, p); TIME_PARAMS.forEach(k => { if (k in o) o[k] = o[k] ? hm(o[k]) : '-'; }); return o; };
const evTitle = e => e.title_i18n && I18N.has(e.title_i18n.key) ? t(e.title_i18n.key, i18nParams(e.title_i18n.params)) : e.title;
const kindLabel = k => I18N.has('kind.' + k) ? t('kind.' + k) : k;
// A title the server made up as a stand-in (title_is_default) is left out when the kind word already says it ("Final report · Final report", "Agent message · Message"); one that tells more ("Assigned · Claude Code run") stays
const titleAdds = e => !e.title_is_default || !kindLabel(e.kind).toLowerCase().includes(String(evTitle(e)).toLowerCase());
// The text of an event. A question the orchestrator asked comes as structure (questions: header null = none given, description null = Codex, which has no descriptions) and is laid out here as the server did
function askText(qs) {
  const lines = [];
  qs.forEach(q => {
    lines.push(`**${q.header || t('event.ask.header')}** ${q.question || ''}`);
    (q.options || []).forEach(op => lines.push(op.description == null ? `- ${op.label || ''}` : `- ${op.label || ''} — ${op.description}`));
    lines.push('');
  });
  return lines.join('\n').trim();
}
const encryptedEvent = e => e.text_i18n && e.text_i18n.key === 'event.encrypted.text';
const evText = e => encryptedEvent(e) ? t('board.msg.encryptedDetail') : Array.isArray(e.questions) ? askText(e.questions) : e.text_i18n && I18N.has(e.text_i18n.key) ? t(e.text_i18n.key, i18nParams(e.text_i18n.params)) : e.text;       // a body the server wrote (not the agent) comes with its key
const encryptedNote = e => `<button type="button" class="encrypted-note" data-encrypted-idx="${esc(e.idx)}" title="${esc(evText(e))}">${esc(t('board.msg.encrypted'))}</button>`;
function wireEncryptedNotes(root, items) {
  root.querySelectorAll('.encrypted-note').forEach(b => b.onclick = ev => {
    ev.stopPropagation();
    const e = items.find(x => x.idx === +b.dataset.encryptedIdx);
    if (e) openEventModal(e, evTitle(e) || kindLabel(e.kind), b);
  });
}
const nameIds = s => String(s || '').replace(/\ba[0-9a-f]{16}\b/g, id => S && S.names[id] ? S.names[id] : id);
async function api(path, opt) {
  const sep = path.includes('?') ? '&' : '?';
  const r = await fetch(path.replace(/^\//, '') + (SESSION ? sep + 'session=' + encodeURIComponent(SESSION) : ''), opt);
  if (!r.ok) throw await httpError(r, path);       // carries the status, so the first screen can tell "no session" from "server refused or down"
  return r.json();
}
// For periodic calls: a request with no answer is cut after 30 s (a caller that waits for the previous request before sending the next would stop refreshing for ever if it were not cut)
async function apiGuarded(path) {
  const ctl = new AbortController(), guard = setTimeout(() => ctl.abort(), 30000);
  try { return await api(path, { signal: ctl.signal }); } finally { clearTimeout(guard); }
}
// Async response race: each kind (drawer, modal, talk, timeline) raises its request number; a late response is dropped if a newer request went out or the view was closed
const gens = {};
const nextGen = kind => (gens[kind] = (gens[kind] || 0) + 1);
const isLatest = (kind, g) => gens[kind] === g;

// ---------- markdown (tables, lists and code only) ----------
function inline(s) {
  s = esc(s);
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => (codes.push(c), '\u0000' + (codes.length - 1) + '\u0000'));
  s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => '<code>' + codes[+i] + '</code>');
}
function md(src) {
  const lines = String(src || '').replace(/\r/g, '').split('\n');
  let out = '', i = 0;
  const listStack = [];
  const closeLists = (lvl = -1) => { while (listStack.length && listStack[listStack.length - 1].ind > lvl) out += '</li></' + listStack.pop().tag + '>'; };
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*```/.test(l)) {
      closeLists(); let buf = []; i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      out += '<pre><code>' + esc(buf.join('\n')) + '</code></pre>'; i++; continue;
    }
    if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      closeLists();
      const row = x => x.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      out += '<table><thead><tr>' + row(l).map(c => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>';
      i += 2;
      while (i < lines.length && /^\s*\|/.test(lines[i])) out += '<tr>' + row(lines[i++]).map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>';
      out += '</tbody></table>'; continue;
    }
    let m;
    if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { closeLists(); const n = Math.min(4, m[1].length); out += `<h${n}>${inline(m[2])}</h${n}>`; i++; continue; }
    if (/^\s*(---|\*\*\*)\s*$/.test(l)) { closeLists(); out += '<hr>'; i++; continue; }
    if ((m = l.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
      const ind = m[1].length, tag = /\d/.test(m[2]) ? 'ol' : 'ul';
      const top = listStack[listStack.length - 1];
      if (!top || ind > top.ind) { out += '<' + tag + '><li>'; listStack.push({ ind, tag }); }
      else { closeLists(ind); out += '</li><li>'; }
      out += inline(m[3]); i++; continue;
    }
    if (/^\s*>/.test(l)) { closeLists(); let buf = []; while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, '')); out += '<blockquote>' + md(buf.join('\n')) + '</blockquote>'; continue; }
    if (!l.trim()) { closeLists(); i++; continue; }
    if (listStack.length && /^\s+\S/.test(l)) { out += ' ' + inline(l.trim()); i++; continue; }
    closeLists();
    let buf = [l]; i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*([-*+]|\d+[.)])\s|\s*```|\s*\||\s*>)/.test(lines[i])) buf.push(lines[i++]);
    out += '<p>' + buf.map(inline).join('<br>') + '</p>';
  }
  closeLists();
  return out;
}

// ---------- derived values ----------
function currentDebate() {
  if (!S || !S.debates.length) return null;
  return S.debates.find(d => d.root === ui.debate) || S.debates.find(d => d.current) || null;          // an estimated room is never the current one: with no current debate none is opened for the user
}
function topicKey(tp) { const m = (tp.title || '').match(/^(T\d+)\s+(.*)$/); return m ? [m[1], m[2]] : ['', tp.title]; }
const OPEN_CELLS = ['writing', 'draft', 'paused', 'unknown'];        // a cell of these states is a round that is not over: the agent is writing, has left a draft, was interrupted, or is not known
// A topic with no round folders is a room (of cells, or of participants only): one result per seat, so it has one result column and no "next round" to wait for.
// (game.js keeps its own copy of these two: the office does not load this file.)
const noRounds = tp => !!tp.room;
const roundKeys = tp => tp.room === 'members' ? [] : tp.room ? [1] : Array.from({ length: Math.max(2, ...tp.rounds) }, (_, i) => i + 1);      // a room has one round, a debate at least two rounds
const livePlaced = tp => (tp.placed || []).filter(p => p.live);              // the agents thought to work in a topic that hold no cell there
function topicStage(tp) {
  const cells = tp.rows.flatMap(r => r.cells);
  const assigned = cells.filter(c => c.agent);
  if (finalOk(tp.final)) return { cls: 's-final', text: t('board.stage.final'), step: 'final' };
  if (bundleClosed(tp)) return { cls: 's-final', text: t('board.stage.bundleFinal'), step: 'final' };
  if (tp.room === 'members') {                       // a room of participants only: no cell and no round, so only whether they still talk
    const live = tp.rows.some(r => r.agents.some(id => isLive(agentById(id))));
    return { cls: live ? 's-active' : 's-ready', text: t(live ? 'board.stage.room.members' : 'board.stage.room.membersEnded', { count: tp.rows.length }), step: 'brief' };
  }
  if (!assigned.length && !cells.some(c => c.state === 'done')) {
    const est = livePlaced(tp).length;               // nobody holds a cell, but somebody is thought to work here: not pending, and not done either
    if (est) return { cls: 's-active', text: t('board.stage.estimated', { count: est }), step: 'brief' };
    return { cls: 's-wait', text: tp.deps ? t('board.stage.waitDeps', { deps: tp.deps }) : t('board.stage.wait'), step: 'brief' };
  }
  if (noRounds(tp)) {                                // one result per seat: submitted, or still being written, or stopped; "Done" only when the judgment closes it (a confirmed final, nobody working)
    const rc = tp.rows.map(row => row.cells[0]).filter(Boolean);
    const done = rc.filter(c => c.state === 'done').length, step = roundKeys(tp)[0];
    if (rc.some(c => OPEN_CELLS.includes(c.state))) return { cls: 's-active', text: t('board.stage.room.active', { done, total: rc.length }), step };
    if (!done) return { cls: 's-wait', text: t('board.stage.wait'), step: 'brief' };
    if (done < rc.length) return { cls: 's-ready', text: t('board.stage.room.partial', { done, total: rc.length }), step };
    if (isClosable(tp)) return { cls: 's-final', text: t('board.stage.complete'), step, doneThrough: step };
    if (tp.rows.some(r => r.agents.some(id => isLive(agentById(id)))) || livePlaced(tp).length) return { cls: 's-ready', text: t('board.stage.room.ready', { total: rc.length }), step };
    return { cls: 's-ready', text: t('board.stage.room.unconfirmed', { total: rc.length }), step };        // everything is in and nobody works, but no document is confirmed as the end
  }
  const rounds = tp.rounds;
  let active = null;
  for (const r of rounds) {
    const rc = tp.rows.map(row => row.cells.find(c => c.round === r)).filter(Boolean);
    if (rc.some(c => OPEN_CELLS.includes(c.state))) { active = r; break; }
  }
  if (active != null) {
    const rc = tp.rows.map(row => row.cells.find(c => c.round === active)).filter(Boolean);
    const done = rc.filter(c => c.state === 'done').length;
    return { cls: 's-active', text: t('board.stage.active', { n: active, done, total: rc.length }), step: active };
  }
  const last = Math.max(...rounds.filter(r => tp.rows.some(row => row.cells.find(c => c.round === r && c.state === 'done'))), 0);
  if (last) {
    const rc = tp.rows.map(row => row.cells.find(c => c.round === last)).filter(Boolean);
    const all = rc.every(c => c.state === 'done');
    return all ? { cls: 's-ready', text: t('board.stage.ready', { n: last }), step: last, doneThrough: last }
               : { cls: 's-ready', text: t('board.stage.partial', { n: last }), step: last };
  }
  return { cls: 's-wait', text: t('board.stage.wait'), step: 'brief' };
}
// ---------- drawing ----------
function renderTop() {
  const sel = $('#sessionSel');
  const live = S.session.alive;
  $('#sessionLive').innerHTML = live == null
    ? `<span class="dot${orchCx() ? ' cx' : ''}"></span>${t('status.session.unknown')} <span class="faint mono">${esc(S.session.id.slice(0, 8))}</span>`
    : `<span class="dot ${live ? 'alive' : 'dead'}${orchCx() ? ' cx' : ''}"></span>${live ? t('status.session.running') : t('status.session.off')} <span class="faint mono">${esc(S.session.id.slice(0, 8))}</span>`;
  const cnt = { running: 0, stalled: 0, done: 0, failed: 0, interrupted: 0, unknown: 0 };
  const scope = scopeAgents();
  scope.forEach(a => { if (a.status in cnt) cnt[a.status]++; else if (a.status === 'killed') cnt.failed++; });
  const allCost = sessionCost(S), cxCost = codexCost(S);
  $('#counters').innerHTML =
    (S.alerts ? (() => { const al = activeAlerts(), dec = al.filter(a => a.level === 'decide').length;
      return al.length ? `<button class="chip" id="alertChip" style="cursor:pointer;color:${dec ? 'var(--red)' : 'var(--amber)'};border-color:currentColor">${dec ? t('board.top.alertsDecide', { n: `<b>${al.length}</b>`, decide: dec }) : t('board.top.alerts', { n: `<b>${al.length}</b>` })}</button>` : ''; })() : '') +
    `<span class="chip" title="${t('board.top.cost.title')}${hasCodex(S) ? `\n${t('board.top.cost.codex', { cost: usd(cxCost) })}` : ''}">${t('board.top.cost', { cost: `<b style="color:var(--green)">${usd(allCost)}</b>` })}</span>` +
    `<span class="chip"><span class="dot running active"></span>${t('status.running')} <b>${cnt.running}</b></span>` +
    (cnt.stalled ? `<span class="chip" style="color:var(--amber)"><span class="dot stalled"></span>${t('status.stalled')} <b>${cnt.stalled}</b></span>` : '') +
    (cnt.interrupted ? `<span class="chip" style="color:var(--amber)"><span class="dot interrupted"></span>${t('status.interrupted')} <b>${cnt.interrupted}</b></span>` : '') +
    (cnt.unknown ? `<span class="chip"><span class="dot unknown"></span>${t('status.unknown')} <b>${cnt.unknown}</b></span>` : '') +
    `<span class="chip"><span class="dot done"></span>${t('status.done')} <b>${cnt.done}</b></span>` +
    (cnt.failed ? `<span class="chip" style="color:var(--red)"><span class="dot failed"></span>${t('board.top.failed')} <b>${cnt.failed}</b></span>` : '') +
    diagChipHtml();
  const chip = $('#alertChip'); if (chip) chip.onclick = () => { ui.alertsOpen = !ui.alertsOpen; renderAlerts(); };
  const dchip = $('#diagChip'); if (dchip) dchip.onclick = () => openDiag(dchip);
  if (!sel.dataset.filled) fillSessions();
}
// Session selector: one representative session per project (working folder). The project's other sessions are in the menu of the orchestrator card.
// Claude conversations without subagents (solo) are grouped apart: a project whose representative is solo goes in the "Plain conversations" group, and its name is prefixed with "Chat"
let SESS = null, selHtml = '';
async function fillSessions() {
  const sel = $('#sessionSel');
  sel.dataset.filled = '1';
  try {
    SESS = await fetchSessions();
    if (!SESSION) SESSION = SESS.default || '';
    const byId = id => SESS.sessions.find(s => s.id === id);
    const me = byId(SESSION);
    const label = s => (s.provider === 'codex' ? '◆ ' : '') + (s.title || s.id.slice(0, 8));
    const opts = { orch: '', solo: '' };
    let hit = false;
    (SESS.projects || []).forEach(pr => {
      const cur = !!me && me.proj === pr.name, rep = byId(pr.rep);
      const show = cur ? me : rep;
      hit = hit || cur;
      opts[rep && rep.solo ? 'solo' : 'orch'] += `<option value="${esc(pr.rep)}" ${cur ? 'selected' : ''}>${show && show.solo ? t('board.sess.chat', { name: esc(pr.name) + (pr.n > 1 ? ` (${pr.n})` : ''), title: esc(show ? label(show) : pr.rep.slice(0, 8)) }) : `${esc(pr.name)}${pr.n > 1 ? ` (${pr.n})` : ''} · ${esc(show ? label(show) : pr.rep.slice(0, 8))}`}</option>`;
    });
    let html = opts.solo ? (opts.orch ? `<optgroup label="${t('board.sess.group.orch')}">${opts.orch}</optgroup>` : '') + `<optgroup label="${t('board.sess.group.solo')}">${opts.solo}</optgroup>` : opts.orch;
    // Show the current session as selected even when it was opened by address but is not in the list (old or deleted)
    if (SESSION && !hit) html = `<option value="${esc(SESSION)}" selected>${esc(SESSION.slice(0, 8))} · ${t('board.sess.unlisted')}</option>` + html;
    if (html !== selHtml) { sel.innerHTML = html; selHtml = html; }   // Not rewritten when unchanged (so an open dropdown does not close)
    renderPlanBar(PLANS);                                            // The bottom bar checks whether Claude is in use (sources), so draw it once more after the list arrives
  } catch {}
  sel.onchange = () => { const u = new URL(location); u.searchParams.set('session', sel.value); location = u; };
  if (S) renderOrch();
}
setInterval(() => { if (!document.hidden) fillSessions(); }, 30000);   // Every 30 s: representatives, working counts, new sessions
const soloNow = () => { const me = SESS && SESS.sessions.find(s => s.id === SESSION); return !!me && !!me.solo; };
function sameProject() {
  const me = SESS && SESS.sessions.find(s => s.id === SESSION);
  if (!me) return [];
  return SESS.sessions.filter(s => s.proj === me.proj && s.id !== SESSION);
}
function otherBtnHtml() {         // Other sessions of the same project: orchestrations and conversations without subagents are counted separately
  const o = sameProject(), solo = o.filter(s => s.solo).length, orch = o.length - solo;
  return o.length ? `<button class="chip other-btn" id="otherBtn" title="${t('board.sess.other.title')}">${[orch ? t('board.sess.other.orch', { n: orch }) : '', solo ? t(orch ? 'board.sess.other.chat' : 'board.sess.other.chatAlone', { count: solo }) : ''].filter(Boolean).join(' · ')} ▾</button>` : '';
}
function openOtherMenu(btn) {
  const old = $('#otherMenu'); if (old) { old.remove(); return; }
  const list = sameProject(), orch = list.filter(s => !s.solo), solo = list.filter(s => s.solo), r = btn.getBoundingClientRect();
  const el = document.createElement('div');
  el.id = 'otherMenu'; el.className = 'menu-pop';
  el.style.left = Math.max(8, Math.min(r.left, innerWidth - 380)) + 'px'; el.style.top = r.bottom + 6 + 'px';
  const item = s => `<a class="mp-item" href="?session=${encodeURIComponent(s.id)}"><span class="dot ${s.active ? 'running' : 'ended'}${s.provider === 'codex' ? ' cx' : ''}"></span>` +
    `<span class="mp-t">${s.rep ? `<b class="mp-rep">${t('board.sess.menu.rep')}</b> ` : ''}${esc(s.title || s.id.slice(0, 8))}</span>` +
    `<span class="mp-m">${s.solo ? t('board.sess.menu.chat') : s.provider === 'codex' ? esc({ tui: 'TUI', desktop: 'Desktop', exec: 'exec' }[s.origin] || 'Codex') : t('board.sess.menu.agents', { count: s.agents })}${s.active ? ' · ' + t('board.sess.menu.working', { n: s.active }) : ''} · ${ago(s.last)}</span></a>`;
  // Orchestrations first, then the conversations without subagents as a separate group
  el.innerHTML = `<div class="mp-head">${t('board.sess.menu.head', { count: list.length })}</div>` + orch.map(item).join('') +
    (solo.length ? `<div class="mp-head">${t('board.sess.menu.headSolo', { count: solo.length })}</div>` + solo.map(item).join('') : '');
  document.body.appendChild(el);
  setTimeout(() => document.addEventListener('click', function off(ev) { if (!el.contains(ev.target)) { el.remove(); document.removeEventListener('click', off); } }), 0);
}
// The sentence under the orchestrator's state while it waits on a usage limit: with or without the reset time, and with or without "continues by itself"
const limitKey = o => 'board.orch.limit' + (o.resets_at ? (o.auto ? '.auto' : '.at') : (o.auto ? '.noTime.auto' : '.noTime'));
function renderOrch() {
  const o = S.orch, running = S.agents.filter(isLive).length;
  const working = o.state === 'working', limitWait = o.state === 'limit_wait';       // limit_wait: its last turn ended on a usage limit and it waits for the reset (+ resets_at, auto)
  const lim = (o.tokens && o.tokens.ctx_limit) || 2e5, ctxPct = Math.min(100, (o.ctx || 0) / lim * 100);
  $('#orchCard').innerHTML = `
    <div class="label orch-label"><span class="nw">${t('common.orchestrator')}</span> <span class="faint" title="${esc(S.session.title || '')}">${esc(S.session.title || '')}</span>${otherBtnHtml()}</div>
    <div class="orch-state"><span class="dot ${limitWait ? 'interrupted' : working ? 'running active' : running ? 'done' : 'ended'}${cxCls(o)}"></span>${limitWait ? t('board.orch.limit') : working ? t('status.running') : running ? t('board.orch.waiting') : t('board.orch.idle')}</div>
    <div class="orch-sub">${limitWait ? t(limitKey(o), { time: o.resets_at ? hm(o.resets_at) : '' }) : running ? t('board.orch.sub.running', { agents: `<b>${t('unit.agent', { count: running })}</b>` }) : soloNow() ? t('board.orch.sub.solo') : t('board.orch.sub.none')} · ${esc(modelName(o.model))}${o.effort ? ' · ' + esc(o.effort) : ''}</div>
    <div class="orch-act" title="${esc(nameIds(o.last_action))}">${t('board.orch.last', { ago: ago(o.last_action_ts), action: esc(nameIds(o.last_action) || '-') })}</div>
    <div class="meter" title="${t('board.orch.ctx.title', { ctx: kfmt(o.ctx), limit: kfmt(lim) })}"><i style="width:${ctxPct.toFixed(1)}%;${ctxPct > 80 ? 'background:var(--amber)' : ''}"></i></div>
    <div class="orch-sub" style="font-size:11.5px;margin-top:4px">${t('board.orch.ctx', { ctx: kfmt(o.ctx), limit: kfmt(lim), pct: ctxPct.toFixed(0) })}</div>`;
  const ob = $('#otherBtn');
  if (ob) ob.onclick = ev => { ev.stopPropagation(); openOtherMenu(ob); };
  renderTokens();
  renderTalkTime();
  renderAtalkTime();
}
function renderTokens() {
  // Claude Code and Codex in the same frame: a summary tile per provider on top (same items, same size), the same rows per provider in the table, one total
  const o = S.orch.tokens || {}, d = currentDebate(), oProv = orchCx() ? 'codex' : 'claude';
  const provOf = a => isCx(a) ? 'codex' : 'claude';
  const PN = { claude: 'Claude Code', codex: CXG + ' Codex' };
  const provs = ['claude', 'codex'].filter(p => p === oProv || S.agents.some(a => provOf(a) === p));
  const mixed = provs.length > 1;
  const G = [o, ...S.agents.map(a => a.tokens)].reduce((g, tk) => { const x = tk && tk.guardian;
    if (x) { g.calls += x.calls || 0; g.input += (x.input || 0) + (x.cache_read || 0); g.output += x.output || 0; } return g; }, { calls: 0, input: 0, output: 0 });
  const cl = S.codex_limit, clOn = provs.includes('codex') && cl && !cl.stale && cl.used_percent != null && !(cl.resets_at && tsOf(cl.resets_at) <= now());
  const P = {};
  provs.forEach(p => {
    const ag = S.agents.filter(a => provOf(a) === p), live = ag.filter(isLive), ended = ag.filter(a => !isLive(a));
    P[p] = { ag, live, ended, L: tokSum(live.map(a => a.tokens)), E: tokSum(ended.map(a => a.tokens)),
             T: tokSum([p === oProv ? o : null, ...ag.map(a => a.tokens)]) };
  });
  const T = tokSum([o, ...S.agents.map(a => a.tokens)]);
  const dAgents = d ? S.agents.filter(a => inDebate(a, d)) : [];
  const dGuessed = d ? S.agents.filter(a => guessedIn(a, d)).length : 0;
  const D = tokSum(dAgents.map(a => a.tokens));
  const row = (name, sub, ctx, tk, cls = '') => `<tr class="${cls}"><td>${name}${sub ? ` <span class="sub">${sub}</span>` : ''}</td><td>${ctx}</td><td>${kfmt(tokIn(tk))}</td><td class="c-out">${kfmt(tk.output)}</td><td class="c-usd">${usd(tk.cost)}</td></tr>`;
  const rows = p => {
    const x = P[p];
    return (mixed ? `<tr class="grp"><td colspan="5">${PN[p]}</td></tr>` : '') +
      (p === oProv ? row(t('board.tok.row.orch'), '', `${kfmt(o.ctx)} <span class="sub">/ ${kfmt(o.ctx_limit)}</span>`, o) : '') +
      row(t('board.tok.row.live'), t('unit.agent', { count: x.L.n }), x.L.n ? `${kfmt(x.L.ctx)} <span class="sub">${t('board.tok.sum')}</span>` : '-', x.L) +
      row(t('board.tok.row.ended'), t('unit.agent', { count: x.E.n }), '-', x.E);
  };
  const tile = p => { const x = P[p];
    return `<div class="ptile"><div class="pn">${PN[p]}</div><div class="pv">${usd(x.T.cost)}</div>` +
      `<div class="pm">${t('board.tok.tile.io', { input: `<b>${kfmt(tokIn(x.T))}</b>`, output: `<b>${kfmt(x.T.output)}</b>` })}</div>` +
      `<div class="pm">${t(p === oProv ? 'board.tok.tile.agentsOrch' : 'board.tok.tile.agents', { orch: '<b>1</b>', live: `<b>${x.live.length}</b>`, ended: `<b>${x.ended.length}</b>` })}</div></div>`; };
  // Footer lines per provider, in the same order (cache and calls → per model → what only that provider has)
  const foot = p => { const x = P[p].T, pre = mixed ? `<span class="pf">${PN[p]}</span> ` : '';
    const rp = tokIn(x) ? Math.round(x.cache_read / tokIn(x) * 100) : 0;
    const only = p === 'claude'
      ? (x.adv_calls ? ' · ' + t('board.tok.foot.advisor', { calls: x.adv_calls, input: `<b>${kfmt(x.adv_input)}</b>`, output: `<b>${kfmt(x.adv_output)}</b>` }) : '')
      : (x.reasoning ? ' · ' + t('board.tok.foot.reasoning', { n: `<b>${kfmt(x.reasoning)}</b>` }) : '') + (G.calls ? ' · ' + t('board.tok.foot.review', { calls: `<b>${G.calls}</b>` }) : '') +
        (clOn ? ' · ' + t(cl.reached ? 'board.tok.foot.weeklyReached' : 'board.tok.foot.weekly', { pct: `<b${cl.used_percent >= 90 || cl.reached ? ' style="color:var(--red)"' : ''}>${Math.round(cl.used_percent)}%</b>`,
          scope: cl.resets_at ? t('board.tok.foot.scopeResets', { reset: hm(tsOf(cl.resets_at)) }) : t('board.tok.foot.scope') }) : '');
    return `${pre}${t('board.tok.foot.cache', { read: `<b>${rp}%</b>`, write: `<b>${kfmt(x.cache_write)}</b>`, input: `<b>${kfmt(x.input)}</b>`, calls: `<b>${I18N.num(x.calls)}</b>` })}<br>` +
      `${t('board.tok.foot.models', { list: Object.values(x.models).sort((a, b) => b.cost - a.cost).map(mc => `${esc(modelLabel(mc))} <b>${usd(mc.cost)}</b>`).join(' · ') || '-' })}${only}<br>`; };
  const all = T.cost || 1, pct = v => (v / all * 100).toFixed(2);
  const help = ['tiles', 'context', 'input', 'output', 'cost', 'bill'].concat(provs.includes('codex') ? ['codex'] : []).map(k => t('board.tok.help.' + k)).join('\n');
  $('#tokCard').innerHTML = `
    <div class="label">${t('board.tok.title')} <span class="help" title="${esc(help)}">?</span><span class="faint" style="text-transform:none;font-weight:400">${t('board.tok.since', { time: S.session.started ? hm(S.session.started) : '' })}</span></div>
    <div class="ptiles${mixed ? '' : ' one'}">${provs.map(tile).join('')}</div>
    <table class="tok"><thead><tr><th>${t('board.tok.th.kind')}</th><th>${t('board.tok.th.ctx')}</th><th>${t('board.tok.th.input')}</th><th class="c-out">${t('board.tok.th.output')}</th><th class="c-usd">${t('board.tok.th.cost')}</th></tr></thead><tbody>
      ${provs.map(rows).join('')}
      ${row(t('board.tok.row.total'), t('board.tok.row.totalSub', { count: S.agents.length }), '-', T, 'total')}
    </tbody></table>
    <div class="mix" title="${t('board.tok.mix.title', { read: pct(T.cost_read), write: pct(T.cost_write), input: pct(T.cost_input), output: pct(T.cost_output) })}">
      <i style="width:${pct(T.cost_read)}%;background:var(--blue)"></i><i style="width:${pct(T.cost_write)}%;background:var(--amber)"></i><i style="width:${pct(T.cost_input)}%;background:var(--purple)"></i><i style="width:${pct(T.cost_output)}%;background:var(--green)"></i></div>
    <div class="tok-foot">
      ${provs.map(foot).join('')}
      ${d && dAgents.length ? `${t(isRoom(d) ? 'board.tok.room' : 'board.tok.debate', { name: esc(d.name), count: dAgents.length, input: `<b>${kfmt(tokIn(D))}</b>`, output: `<b>${kfmt(D.output)}</b>`, cost: `<b style="color:var(--green)">${usd(D.cost)}</b>` })}${dGuessed ? ` <span class="faint">${t('board.tok.guessed', { count: dGuessed })}</span>` : ''}<br>` : ''}
      ${t(mixed ? 'board.tok.breakdownTotal' : 'board.tok.breakdown', { sr: '<span style="color:var(--blue)">■</span>', read: `<b>${usd(T.cost_read)}</b>`, sw: '<span style="color:var(--amber)">■</span>', write: `<b>${usd(T.cost_write)}</b>`,
        si: '<span style="color:var(--purple)">■</span>', input: `<b>${usd(T.cost_input)}</b>`, so: '<span style="color:var(--green)">■</span>', output: `<b>${usd(T.cost_output)}</b>` })}${T.adv_cost ? ' ' + t('board.tok.advIncl', { cost: usd(T.adv_cost) }) : ''}<br>
      <span class="faint">${t('board.tok.note')}${T.unpriced ? ' · ' + (G.calls ? `<span title="${t('board.tok.unpriced.title', { calls: G.calls, input: kfmt(G.input), output: kfmt(G.output) })}">${t('board.tok.unpriced', { count: T.unpriced })}</span>` : t('board.tok.unpriced', { count: T.unpriced })) : ''}</span>
    </div>`;
}
function renderSummary() {
  const d = currentDebate(), ul = $('#summary');
  if (!d) { ul.innerHTML = `<li class="muted">${t('board.sum.noDebate')}</li>`; return; }
  const items = [];
  const nexts = [];
  for (const tp of d.topics) {
    const [k, name] = topicKey(tp), st = topicStage(tp);
    if (st.cls === 's-wait') { nexts.push(`<b>${esc(k || name)}</b> ${esc(k ? name : '')}${tp.deps ? ' (' + t('board.sum.after', { deps: esc(tp.deps) }) + ')' : ''}`); continue; }
    let detail = '';
    if (st.cls === 's-active') {
      const r = st.step;
      const parts = tp.rows.map(row => { const c = row.cells.find(c => c.round === r); return c ? [row, c] : null; }).filter(Boolean);
      const w = parts.filter(([, c]) => c.state !== 'done').map(([row, c]) => `${row.p}${c.state === 'draft' ? '(' + t('board.sum.draft', { count: c.lines }) + ')' : c.state === 'writing' ? '(' + t('board.sum.writing') + ')' : c.state === 'paused' ? '(' + t('board.sum.paused') + ')' : c.state === 'missing' ? '(' + t('board.sum.missing') + ')' : c.state === 'previous' ? '(' + t('board.sum.previous') + ')' : ''}`);
      if (w.length) detail = ' — ' + t('board.sum.remaining', { list: w.join(', ') });
    }
    items.push(`<li><span class="k">${esc(k || '·')}</span><span><b>${esc(name)}</b> · ${esc(st.text)}${esc(detail)}</span></li>`);
  }
  if (nexts.length) items.push(`<li><span class="k">${t('board.sum.next')}</span><span class="next">${nexts.join(' · ')}</span></li>`);
  const stalled = S.agents.filter(a => a.status === 'stalled');
  if (stalled.length) items.push(`<li><span class="k" style="color:var(--amber)">${t('board.sum.warn')}</span><span>${t('board.sum.stalled', { names: stalled.map(a => esc(a.tag || a.title)).join(', '), min: Math.round((now() - Math.max(...stalled.map(a => a.last_ts))) / 60) })}</span></li>`);
  ul.innerHTML = items.join('') || `<li class="muted">${t('board.sum.none')}</li>`;
}
function renderDebates() {
  const tabs = $('#debateTabs'), d = currentDebate();
  // At most 8 tabs. A debate with a Codex participant shows even past that (a session with only Claude is unchanged)
  const cxDebate = x => x.topics.some(tp => tp.rows.some(r => r.agents.some(id => isCx(agentById(id)))));
  tabs.innerHTML = S.debates.filter((x, i) => i < 8 || cxDebate(x)).map(x =>
    `<button class="tab ${d && x.root === d.root ? 'on' : ''}" data-root="${esc(x.root)}"${x.copies ? ` title="${esc(t('board.debate.copies', { count: x.copies }))}"` : ''}>${esc(x.name)}${x.current ? `<span class="cur">${t('board.debate.current')}</span>` : ''}${x.sure === false ? `<span class="est" title="${esc(t('board.room.est.title.launch'))}">${t('board.room.est')}</span>` : ''}</button>`).join('');
  tabs.querySelectorAll('.tab').forEach(b => b.onclick = () => { ui.debate = b.dataset.root; store.set('debate', ui.debate); renderAll(); loadTimeline(); });
  if (!d) { $('#topics').innerHTML = `<div class="card empty">${soloNow() ? t('board.debate.solo') : S.debates.length ? t('board.debate.pick') : t('board.debate.none')}</div>` + renderOther(null); $('#debateMeta').innerHTML = ''; bindTopics(); return; }
  $('#debateMeta').innerHTML = `<span title="${esc(d.root + (d.copies ? ' — ' + t('board.debate.copies', { count: d.copies }) : ''))}">${esc(d.title)}</span><span class="faint mono">${esc(d.short)}</span>` +
    (d.sure === false ? `<span class="guess" title="${esc(t('board.room.est.title.launch'))}">${t('board.room.est')}</span>` : '') +
    (d.finals.length ? d.finals.map(f => `<span class="chip fchip" data-path="${esc(f.path)}">${t('board.debate.final', { name: esc(f.name), lines: `<span class="faint">${t('unit.line', { count: f.lines })}</span>` })}</span>`).join('') : '') +
    (d.final && (finalOk(d.final) || (d.final.why || []).length && !d.final.why.includes('no_report')) ? `<span class="bundle-final">${t('board.debate.bundleFinal')} ${finalHtml(d.final)}</span>` : '');
  $('#debateMeta').querySelectorAll('[data-path]').forEach(e => e.onclick = () => openFile(e.dataset.path, e));
  const rootPlaced = placedHtml(withoutWriters(d.placed, d, null));          // the agents thought to work in the bundle as a whole (no one topic): one line above its topics
  $('#topics').innerHTML = (rootPlaced ? `<div class="card topic root-placed">${rootPlaced}</div>` : '') + d.topics.map(tp => renderTopic(tp, d)).join('') + renderOther(d);
  bindTopics();
}
function bindTopics() {
  $('#topics').querySelectorAll('[data-path]').forEach(e => e.onclick = ev => { ev.stopPropagation(); openFile(e.dataset.path, e); });
  $('#topics').querySelectorAll('[data-agent]').forEach(e => e.onclick = ev => { ev.stopPropagation(); openDrawer(e.dataset.agent); });
}
// The agents of this debate that hold no cell but are thought to work in it (the lines under its topics and above them: `placed`), by id
function placedIds(d) {
  const ids = new Set((d && d.placed || []).map(p => p.agent));
  (d ? d.topics : []).forEach(tp => (tp.placed || []).forEach(p => ids.add(p.agent)));
  return ids;
}
// Other work: agents that are working but sit in no cell of this debate and are not thought to work in it either (same rule as the "Other work" room in the office); the ones that were launched together are one group
function renderOther(d) {
  const seated = placedIds(d);
  (d ? d.topics : []).forEach(tp => tp.rows.forEach(r => r.agents.length && seated.add(r.agents[r.agents.length - 1])));
  const list = S.agents.filter(a => isLive(a) && !seated.has(a.id))
    .sort((a, b) => (a.spawn_ts || 0) - (b.spawn_ts || 0));
  if (!list.length) return '';
  const row = a => {
    const live = a.status === 'running' && now() - (a.last_ts || 0) < 45;
    const lt = a.last_tool;
    return `<tr><td><div class="who"><span class="plet">${esc(a.tag || '·')}</span><div style="min-width:0"><div class="role" title="${esc(a.description)}">${esc(a.title)}</div>` +
      `<div class="ag" data-agent="${esc(a.id)}"><span class="dot ${a.status} ${live ? 'active' : ''}${cxCls(a)}"></span><span class="t">${esc(stateLabel(a))} · ${ago(a.last_ts)} · ${t('board.elapsed', { dur: dur(now() - (a.spawn_ts || a.first_ts || now())) })}</span></div>` + provLine(a) +
      (lt ? `<div class="now-line" title="${esc(lt.text)}">${esc(lt.name)} ${esc(lt.text)}</div>` : '') + `</div></div></td></tr>`;
  };
  const groups = [], at = new Map();                                    // the agents launched together (the same `launch`) stand together, at the place of the first of them; the rest stand alone
  list.forEach(a => {
    if (a.launch && at.has(a.launch)) { at.get(a.launch).push(a); return; }
    const g = [a]; groups.push(g);
    if (a.launch) at.set(a.launch, g);
  });
  const rows = groups.map(g => (g.length > 1 ? `<tr class="grp"><td>${t('board.work.together', { count: g.length })}</td></tr>` : '') + g.map(row).join('')).join('');
  return `<div class="card topic active">
    <div class="topic-head"><span class="tkey">${t('board.work.key')}</span><h3>${t('board.work.title')}</h3><span class="stage s-active">${t('board.work.count', { count: list.length })}</span></div>
    <table class="mx"><thead><tr><th>${t('board.work.th')}</th></tr></thead><tbody>${rows}</tbody></table>
  </div>`;
}
// ---------- the agents thought to work in a topic (no cell), the end of a topic ----------
const PLACED_ORDER = ['tag', 'launch_call', 'launch_peer', 'guide_read'];
const placedWhy = why => I18N.has('board.placed.why.' + why) ? t('board.placed.why.' + why) : why;
function placedChip(p) {
  const a = agentById(p.agent), name = a ? agentName(p.agent) : String(p.agent || '').slice(0, 6);
  const live = !a ? '' : p.live ? ` <span class="faint">${t('board.elapsed', { dur: dur(now() - (a.spawn_ts || a.first_ts || now())) })}</span>` : ` <span class="faint">${esc(stateLabel(a))}</span>`;       // working: for how long; over: how it ended (done, interrupted …)
  return `<span class="who-b agent" data-agent="${esc(p.agent)}" title="${esc(t('board.placed.title', { whys: (p.whys && p.whys.length ? p.whys : [p.why]).map(placedWhy).join(', ') }))}">${cxMark(a)}${esc(name)}</span>${live}`;
}
// "Working · estimated (launched together)" for the agents that hold no cell but are thought to work here, one line for each reason (a room tag is a fact, so it says no "estimated"), and a grey "Ended · no file" line for those that are over
function placedHtml(items) {
  if (!items || !items.length) return '';
  const whys = PLACED_ORDER.concat([...new Set(items.map(p => p.why))].filter(w => !PLACED_ORDER.includes(w)));
  const lines = whys.map(why => {
    const ps = items.filter(p => p.live && p.why === why);
    return ps.length ? `<div class="placed${why === 'tag' ? ' sure' : ''}"><span class="pl-key">${t(why === 'tag' ? 'board.placed.working.tag' : 'board.placed.working', { why: placedWhy(why) })}</span> ${ps.map(placedChip).join(' ')}</div>` : '';
  });
  const gone = items.filter(p => !p.live);
  if (gone.length) lines.push(`<div class="placed ended"><span class="pl-key">${t('board.placed.ended')}</span> ${gone.map(placedChip).join(' ')}</div>`);
  return lines.join('');
}
const FINAL_CANDIDATES = 5;
// The end of a topic, a room or a bundle: the confirmed final (open it), or "closing not confirmed" with the reasons and the documents that could be it. `table`: the file the brief table names, as
// { rel, file } (`file`: the document of the bundle's final/ folder that is that file, when there is one). The table is read for display only: it settles nothing
function finalHtml(f, table) {
  if (!f) return '';
  const named = table ? ' ' + t('board.foot.table', { rel: `<span class="mono">${esc(table.rel)}</span>`, state: table.file ? `<span class="chip fchip" data-path="${esc(table.file.path)}">${t('board.foot.open', { count: table.file.lines })}</span>` : `<span class="faint">${t('board.foot.none')}</span>` }) : '';
  if (finalOk(f)) return (f.rel ? t('board.foot.final', { rel: `<span class="mono">${esc(f.rel)}</span>`, state: `<span class="chip fchip" data-path="${esc(f.path)}">${t('board.foot.open', { count: f.lines })}</span>` }) : '')
    + (f.by ? ` <span class="faint">${t('board.foot.by', { name: esc(whoName(f.by)) })}</span>` : '') + named;
  const why = f.why || [];
  if (!why.length || why.includes('no_report')) return named.trim();                                    // nothing was handed in: there is nothing to close
  const reasons = why.map(w => I18N.has('board.final.why.' + w) ? t('board.final.why.' + w) : w);
  const cands = f.candidates || [];
  return `<span class="chip unconf" title="${esc(reasons.join(' · '))}">${t('board.final.unconfirmed')}</span> <span class="faint">${esc(reasons.join(' · '))}</span>`
    + (cands.length ? ` <span class="faint">${t('board.final.candidates')}</span> ` + cands.slice(0, FINAL_CANDIDATES).map(c => `<span class="chip fchip" data-path="${esc(c.path)}" title="${esc(c.rel)}">${t('board.final.candidate', { name: esc(c.rel.replace(/^final\//, '')), lines: `<span class="faint">${t('unit.line', { count: c.lines })}</span>` })}</span>`).join(' ')
      + (cands.length > FINAL_CANDIDATES ? ` <span class="faint">${t('board.final.more', { count: cands.length - FINAL_CANDIDATES })}</span>` : '') : '')
    + named;
}
// How the owner's first write of a cell was seen (a tool, the shell, a request to save, a room tag): a small badge at the end of the state line
const evBadge = c => c.evidence && I18N.has('board.cell.ev.' + c.evidence) ? `<span class="cev" title="${esc(t('board.cell.ev.' + c.evidence + '.title'))}">${t('board.cell.ev.' + c.evidence)}</span>` : '';
// What a cell tells besides its state: who owns it when that is not the agent of the row, who fixed it, and a guess from the time (marked as one)
function cellMeta(c, agentId) {
  const out = [];
  if (c.owner && c.owner !== agentId) out.push(t('board.cell.owner', { name: esc(whoName(c.owner)) }));
  if (c.editors && c.editors.length) out.push(t('board.cell.editors', { names: c.editors.map(id => esc(whoName(id))).join('·') }));
  if (c.hint && c.hint.agent) out.push(`<span class="guess" title="${esc(t('board.cell.hint.title'))}">${t('board.link.guess')}</span> ${t('board.cell.hint', { name: esc(whoName(c.hint.agent)) })}`);
  return out.length ? `<div class="sub meta">${out.join(' · ')}</div>` : '';
}
// The agents that wrote a confirmed final (of the topic, or of the bundle) are not "working · estimated" or "ended · no file": what they did is the end of the topic
function withoutWriters(items, d, tp) {
  const by = new Set([tp && tp.final, d && d.final].filter(finalOk).map(f => f.by).filter(Boolean));
  (d && !tp ? d.topics : []).forEach(x => { if (finalOk(x.final) && x.final.by) by.add(x.final.by); });
  return (items || []).filter(p => !by.has(p.agent));
}
const shortLetter = text => { const c = Array.from(text || ''); return c.length <= 3 ? c.join('') : c[0].toUpperCase(); };
function renderTopic(tp, d) {
  const [k, name] = topicKey(tp), st = topicStage(tp);
  const fin = finalOk(tp.final), byBundle = bundleClosed(tp) && d && finalOk(d.final) ? d.final : null, closed = fin || isClosable(tp);
  // A topic that is closed shows no round in which nobody wrote anything (a column of "Pending" beside a final is no wait); one with nothing at all keeps its columns
  let rounds = roundKeys(tp);
  if (closed && !noRounds(tp)) { const used = rounds.filter(r => tp.rows.some(row => { const c = row.cells.find(c => c.round === r); return c && c.state !== 'waiting'; })); if (used.length) rounds = used; }
  // Step marks: brief → round 1 → round 2 … → final
  const roundDone = r => tp.rows.length && tp.rows.every(row => (row.cells.find(c => c.round === r) || {}).state === 'done');
  const roundName = r => noRounds(tp) ? t('board.room.col') : t('board.round', { n: r });
  const finRel = fin ? tp.final.rel : byBundle ? byBundle.rel : null;
  const steps = [{ n: t('board.step.brief'), s: tp.brief ? 'done' : '' }]
    .concat(rounds.map(r => ({ n: roundName(r), s: roundDone(r) ? 'done' : st.step === r ? 'active' : '' })))
    .concat([{ n: finRel ? t('board.step.finalNamed', { name: finRel.replace(/^final\//, '') }) : t('board.step.final'), s: fin || byBundle || bundleClosed(tp) ? 'done' : '' }]);
  const stepper = steps.map((s, i) => (i ? `<span class="bar ${s.s === 'done' || (s.s === 'active' && steps[i - 1].s === 'done') ? 'done' : ''}"></span>` : '') +
    `<span class="step ${s.s}"><span class="b">${s.s === 'done' ? '✓' : i === 0 ? '·' : i === steps.length - 1 ? '★' : i}</span>${esc(s.n)}</span>`).join('');
  const rows = tp.rows.map(row => {
    const a = row.agents.length ? agentById(row.agents[row.agents.length - 1]) : null;
    const act = a && isLive(a);
    const agentLine = a ? `<div class="ag" data-agent="${esc(a.id)}" title="${esc(a.description)}"><span class="dot ${a.status} ${act && now() - a.last_ts < 45 ? 'active' : ''}${cxCls(a)}"></span><span class="t">${esc(stateLabel(a))} · ${ago(a.last_ts)}</span></div>` + provLine(a) +
      (act && a.last_tool ? `<div class="now-line" title="${esc(toolText(a.last_tool))}">${esc(a.last_tool.name)} ${esc(toolText(a.last_tool))}</div>` : '')
      : `<div class="ag"><span class="t faint">${t('board.row.unassigned')}</span></div>`;
    const cells = rounds.map(r => {
      const c = row.cells.find(c => c.round === r);
      if (!c || (closed && c.state === 'waiting' && !c.agent && !c.previous)) return closed ? `<td><div class="cell c-none" title="${esc(t('board.cell.none.title'))}"><div class="st">—</div></div></td>` : `<td><div class="cell c-waiting"><div class="st">${t('board.cell.pending')}</div></div></td>`;
      const rd = c.readers.length ? `<div class="sub">${t('board.cell.readBy', { names: c.readers.map(esc).join('·') })}</div>` : '';
      const meta = cellMeta(c, a && a.id), ev = evBadge(c), openable = c.previous && c.lines != null;
      const prev = c.previous && c.state !== 'previous' ? `<div class="sub prev">${t('board.cell.prevFile', { count: c.lines, time: hm(c.mtime) })}</div>` : '';      // a file from before this run, kept for what it is
      const p = c.state === 'draft' || c.state === 'done' || c.state === 'previous' || openable ? `data-path="${esc(c.path)}"` : '';
      if (c.state === 'done') return `<td><div class="cell c-done" ${p} title="${esc(c.path)}"><div class="st">${t('board.cell.done')}</div><div class="sub l">${t('unit.line', { count: c.lines })} · ${hm(c.mtime)}${ev}</div>${meta}${rd}</div></td>`;
      if (c.state === 'previous') return `<td><div class="cell c-previous" ${p} title="${esc(c.path)}"><div class="st">${t('board.cell.previous')}</div><div class="sub l">${t('unit.line', { count: c.lines })} · ${hm(c.mtime)}${ev}</div>${meta}${rd}</div></td>`;
      const ca = (c.agent && agentById(c.agent)) || a, held = !!(ca && isHeld(ca));      // the agent of the cell is stopped or not known: nothing is being typed
      if (c.state === 'draft' && held) return `<td><div class="cell c-draft c-held" ${p} title="${t('board.cell.draftHeld.title.' + ca.status)}"><div class="st">${t('board.cell.draftHeld', { state: statusLabel(ca.status) })}</div><div class="sub l">${t('unit.line', { count: c.lines })} · ${ago(c.mtime)}${ev}</div>${meta}${rd}</div></td>`;
      if (c.state === 'draft') return `<td><div class="cell c-draft" ${p} title="${t('board.cell.draft.title')}"><div class="st">${t('board.cell.draft')} <span class="typing"><i></i><i></i><i></i></span></div><div class="sub l">${t('unit.line', { count: c.lines })} · ${ago(c.mtime)}${ev}</div>${meta}${rd}</div></td>`;
      if (c.state === 'writing') return `<td><div class="cell c-writing" ${p}><div class="st">${t('board.cell.writing')}${held ? '' : ' <span class="typing"><i></i><i></i><i></i></span>'}</div><div class="sub l">${c.planned ? t('board.cell.willSave') : t('board.cell.noFile')}${ev}</div>${prev}${meta}</div></td>`;
      if (c.state === 'paused') return `<td><div class="cell c-paused" ${p} title="${t('board.cell.paused.title')}"><div class="st">${t('board.cell.paused')}</div><div class="sub l">${t('board.cell.paused.sub')}${ev}</div>${prev}${meta}</div></td>`;
      if (c.state === 'missing') return `<td><div class="cell c-missing" ${p}><div class="st">${t('board.cell.missing')}</div><div class="sub l">${t('board.cell.agentEnded')}${ev}</div>${prev}${meta}</div></td>`;
      return `<td><div class="cell c-waiting"><div class="st">${t('board.cell.pending')}</div>${ev ? `<div class="sub l">${ev}</div>` : ''}${meta}</div></td>`;
    }).join('');
    const file = row.cells.length ? row.p + '.md' : '';                  // a row is a file: its name is the file's name (no role is read from a text)
    return `<tr><td><div class="who"><span class="plet">${esc(shortLetter(row.p))}</span><div style="min-width:0"><div class="role"${file ? ` title="${esc(file)}"` : ''}>${esc(file || row.p)}</div>${agentLine}</div></div></td>${cells}</tr>`;
  }).join('');
  const w = rounds.length > 2 ? '40%' : '46%';
  const tp0 = tp.final && tp.final.table_path;
  const table = tp0 && d && tp0.startsWith(d.root + '/') ? { rel: tp0.slice(d.root.length + 1), file: (d.finals || []).find(f => f.path === tp0) || null } : null;       // what the brief table names as the final
  const foot = [
    byBundle ? t('board.foot.bundle', { rel: `<span class="mono">${esc(byBundle.rel)}</span>`, state: `<span class="chip fchip" data-path="${esc(byBundle.path)}">${t('board.foot.open', { count: byBundle.lines })}</span>` }) : bundleClosed(tp) ? t('board.stage.bundleFinal') : finalHtml(tp.final, table),
    tp.deps ? t('board.foot.after', { deps: esc(tp.deps) }) : '',
  ].filter(Boolean).join(' · ') + (tp.docs || []).map(f => ` <span class="chip fchip" data-path="${esc(f.path)}">${t(f.name === 'brief.md' || f.path === tp.guide ? 'board.foot.brief' : 'board.foot.doc', { name: esc(f.name) })}</span>`).join('');
  const room = tp.room ? (tp.room_sure === false ? `<span class="guess" title="${esc(t('board.room.est.title.' + (tp.room_why || 'launch')))}">${t('board.room.est')}</span>` : tp.room_why === 'tag' ? `<span class="chip sure" title="${esc(t('board.room.tag.title'))}">${t('board.room.tag')}</span>` : '') : '';
  return `<div class="card topic ${st.cls === 's-wait' ? 'idle' : st.cls === 's-active' ? 'active' : ''}">
    <div class="topic-head">${k ? `<span class="tkey">${esc(k)}</span>` : tp.room ? `<span class="tkey">${t('board.room.key')}</span>` : ''}<h3 title="${esc(tp.dir)}">${esc(name)}</h3>${room}<span class="stage ${st.cls}">${esc(st.text)}</span></div>
    ${tp.room === 'members' ? '' : `<div class="stepper">${stepper}</div>`}
    <table class="mx"><colgroup><col style="width:${w}">${rounds.map(() => '<col>').join('')}</colgroup>
      <thead><tr><th>${t('board.topic.th.agent')}</th>${rounds.map(r => `<th>${esc(roundName(r))}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table>
    ${placedHtml(withoutWriters(tp.placed, d, tp))}
    ${foot ? `<div class="topic-foot">${foot}</div>` : ''}
  </div>`;
}
function scopeAgents() {
  const d = currentDebate();
  if (ui.agentFilter === 'all' || !d) return S.agents;
  return S.agents.filter(a => inDebate(a, d) || isLive(a) || isHeld(a));
}
function sparkSvg(v) {
  const w = 90, h = 18, mx = Math.max(3, ...v);
  const pts = v.map((x, i) => `${(i / (v.length - 1) * w).toFixed(1)},${(h - 1 - x / mx * (h - 3)).toFixed(1)}`).join(' ');
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="var(--blue)" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
}
// Sessions that started next to one of this session's commands but could not be tied to any (S.unlinked, at most 20): a folded box under the agents, nothing at all when there are none
const unlinkedReason = r => I18N.has('board.unlinked.reason.' + r) ? t('board.unlinked.reason.' + r) : r;
function unlinkedHtml() {
  const list = S.unlinked || [];
  if (!list.length) return '';
  return `<div class="group-label" id="unlinkedToggle" title="${esc(t('board.unlinked.note'))}">${ui.unlinkedOpen ? '▾' : '▸'} ${t('board.unlinked.title', { count: list.length })}</div>` + (!ui.unlinkedOpen ? '' :
    `<div class="faint ul-note">${t('board.unlinked.note')}</div>` + list.map(x => `<div class="card agent unlinked"><div class="a-head"><span class="a-tag">${x.provider === 'codex' ? CXG + ' Codex' : 'Claude Code'}</span><span class="a-title" title="${esc(x.cwd)}">${esc(x.cwd || '-')}</span></div>` +
      `<div class="a-meta"><span>${t('board.drawer.started', { time: hm(x.started) })}</span><span class="mono faint">${esc(String(x.id || '').slice(0, 8))}</span></div>` +
      `<div class="ul-why">${esc(unlinkedReason(x.reason))}</div></div>`).join(''));
}
// What the list says about an agent that stopped but is not over: when a usage limit resets, else the sentence for its state
const heldNote = a => a.reason === 'limit' ? (a.resets_at ? t('board.agents.limitAt', { time: hm(a.resets_at) }) : t('board.agents.limitNoTime')) : stateWhy(a);
// Agents with a parent (a `claude -p` run launched by a sub-agent or by another run) follow that parent, one step further in per level; an agent whose parent is not in this list stands on its own.
// The order within a level is the list's own. -> [[{ a, depth }, ...]]: one list per tree, the launcher first
function trees(list) {
  const ids = new Set(list.map(a => a.id)), kids = new Map(), out = [], seen = new Set();
  const hung = a => !!a.parent && a.parent !== a.id && ids.has(a.parent);
  list.forEach(a => { if (hung(a)) { if (!kids.has(a.parent)) kids.set(a.parent, []); kids.get(a.parent).push(a); } });
  const walk = (a, depth, tree) => { if (seen.has(a.id)) return; seen.add(a.id); tree.push({ a, depth }); (kids.get(a.id) || []).forEach(k => walk(k, depth + 1, tree)); };
  list.forEach(a => { if (!hung(a)) { const tr = []; walk(a, 0, tr); out.push(tr); } });
  list.forEach(a => { if (!seen.has(a.id)) { const tr = []; walk(a, 0, tr); out.push(tr); } });      // a loop of parents (never expected) still shows every agent
  return out;
}
function renderAgents() {
  tabBar($('#agentFilter'), [['debate', t(isRoom(currentDebate()) ? 'board.agents.tab.room' : 'board.agents.tab.debate')], ['all', t('board.agents.tab.all')]], ui.agentFilter, k => { ui.agentFilter = k; store.set('agentFilter', k); renderAll(); });
  const list = scopeAgents();
  const sorted = [...list].sort((a, b) => (statusRank(a.status) - statusRank(b.status)) || ((b.last_ts || 0) - (a.last_ts || 0)));
  // A tree stays together: it is in the main list when any of it is working or held, so a finished run is never cut off from the launcher it hangs under; the trees are ordered by their best member
  const showNow = a => isLive(a) || isHeld(a);
  const grown = trees(sorted).map((tr, i) => ({ tr, i, rank: Math.min(...tr.map(x => statusRank(x.a.status))), last: Math.max(...tr.map(x => x.a.last_ts || 0)), on: tr.some(x => showNow(x.a)) }))
    .sort((p, q) => (p.rank - q.rank) || (q.last - p.last) || (p.i - q.i));
  const active = grown.filter(g => g.on).flatMap(g => g.tr), rest = grown.filter(g => !g.on).flatMap(g => g.tr);
  const card = ({ a, depth }) => {
    const live = a.status === 'running' && now() - (a.last_ts || 0) < 45;
    const cur = isLive(a) ? a.current : null;
    const pend = a.pending && now() - a.pending.ts > 60 ? a.pending : null;
    const nowLine = pend ? `<span class="tn" style="color:var(--amber);border-color:var(--amber)">${t('board.agents.running', { name: esc(pend.name), dur: dur(now() - pend.ts) })}</span><span class="tx" title="${esc(toolText(pend))}">${esc(toolText(pend))}</span>`
      : cur ? (cur.kind === 'tool'
      ? `<span class="tn">${esc(cur.name)}</span><span class="tx" title="${esc(nameIds(toolText(cur)))}">${esc(nameIds(toolText(cur)))}</span>`
      : `<span class="tn">${cur.kind === 'msg' ? t('board.agents.msg') : t('board.agents.thought')}</span><span class="tx" title="${esc(cur.text)}">${esc(cur.text)}</span>`)
      : isHeld(a) ? `<span class="tn">${esc(stateLabel(a))}</span><span class="tx" title="${esc(stateWhy(a))}">${esc(heldNote(a))}</span>`
      : a.notification ? `<span class="tn">${t('board.agents.result')}</span><span class="tx">${esc(a.notification.summary || '')}</span>` : `<span class="tx faint">-</span>`;
    const el = isLive(a) ? now() - (a.spawn_ts || a.first_ts) : (a.last_ts || 0) - (a.spawn_ts || a.first_ts || 0);
    return `<div class="card agent ${ui.drawer === a.id ? 'sel-on' : ''}${depth ? ' child' : ''}" data-agent="${esc(a.id)}"${depth ? ` style="--depth:${depth}"` : ''}>
      <div class="a-head"><span class="dot ${a.status} ${live ? 'active' : ''}${cxCls(a)}"></span>${a.tag ? `<span class="a-tag">${esc(a.tag)}</span>` : ''}<span class="a-title" title="${esc(a.description)}">${esc(a.title)}</span>${guessMark(a)}<span class="pill ${a.status}" title="${esc(stateWhy(a))}">${esc(stateLabel(a))}</span></div>
      ${a.parent && !depth ? `<div class="a-under">${t('board.agents.launchedBy', { name: esc(agentName(a.parent)) })}</div>` : ''}
      <div class="a-now">${nowLine}<span class="ago">${ago(a.last_ts)}</span></div>
      <div class="a-meta">${sparkSvg(a.spark)}<span>${t('board.agents.tools', { n: a.tool_count })}${a.errors ? ` · <span style="color:var(--red)">${t('board.agents.errors', { n: a.errors })}</span>` : ''}</span><span title="${t('board.agents.metaTitle')}">${t('board.agents.metaLine', { ctx: kfmt(a.ctx), input: kfmt(tokIn(a.tokens)), output: kfmt(a.tokens ? a.tokens.output : 0), cost: `<b style="color:var(--green)">${usd(a.tokens ? a.tokens.cost : 0)}</b>` })}</span><span>${t(isLive(a) ? 'board.elapsed' : 'board.took', { dur: dur(el) })}</span><span class="faint">${esc(modelName(a.model))}${a.effort ? ' · ' + esc(a.effort) : ''}</span></div>
    </div>`;
  };
  let html = active.length ? active.map(card).join('') : `<div class="card empty">${t('board.agents.none')}</div>`;
  if (rest.length) {
    html += `<div class="group-label" id="restToggle">${ui.oldOpen ? '▾' : '▸'} ${t('board.agents.finished', { count: rest.length })}</div>`;
    if (ui.oldOpen) html += rest.map(card).join('');
  }
  html += unlinkedHtml();
  const keepAgents = $('#agentList').scrollTop;
  $('#agentList').innerHTML = html;
  $('#agentList').scrollTop = keepAgents;
  $('#agentList').querySelectorAll('[data-agent]').forEach(e => e.onclick = () => openDrawer(e.dataset.agent));
  const rt = $('#restToggle'); if (rt) rt.onclick = () => { ui.oldOpen = !ui.oldOpen; renderAgents(); };
  const ut = $('#unlinkedToggle'); if (ut) ut.onclick = () => { ui.unlinkedOpen = !ui.unlinkedOpen; renderAgents(); };
}
const feedFilters = () => ['all', 'talk', 'peer', 'user', 'orch', 'status'].map(k => [k, t('board.feed.tab.' + k)]);
function renderFeed() {
  const keepTop = $('#feedItems').scrollTop;
  tabBar($('#feedFilter'), feedFilters(), ui.feedFilter, k => { ui.feedFilter = k; store.set('feedFilter', k); renderFeed(); $('#feedItems').scrollTop = 0; });
  const d = currentDebate();
  const debateAgents = new Set(d ? S.agents.filter(a => inDebate(a, d)).map(a => a.id) : []);
  const since = d && debateAgents.size ? Math.min(...S.agents.filter(a => debateAgents.has(a.id)).map(a => a.spawn_ts || a.first_ts || Infinity)) - 1800 : 0;
  const want = e => {
    const f = ui.feedFilter;
    if (f === 'talk') return ['spawn', 'orch_msg', 'handback', 'peer', 'agent_msg'].includes(e.kind);
    if (f === 'peer') return ['agent_msg', 'xread'].includes(e.kind);
    if (f === 'user') return e.kind === 'user_say' || e.kind === 'user_answer';
    if (f === 'orch') return e.kind === 'orch_say' || e.kind === 'orch_ask';
    if (f === 'status') return ['notify', 'stop', 'spawn', 'sys'].includes(e.kind);
    return true;
  };
  const items = S.feed.filter(e => (e.ts || 0) >= since && want(e)).slice(-160).reverse();
  $('#feedItems').innerHTML = items.map(e => {
    if (e.kind === 'sys') return `<div class="ev k-sys" data-idx="${e.idx}"><div class="t" title="${esc(I18N.date(e.ts, 'dateTime'))}">${hm(e.ts)}</div><div><div class="h"><span class="kind">${esc(kindLabel('sys'))} · ${esc(evTitle(e))}</span></div></div></div>`;   // the board's own news (a limit, an API error): no sender, no receiver, dimmed
    const open = ui.openEvents.has(e.idx);
    const title = e.kind === 'notify' ? `<span class="kind">${esc(evTitle(e))}</span>` : `<span class="kind">${esc(kindLabel(e.kind))}${e.title && titleAdds(e) && !['orch_say', 'user_say'].includes(e.kind) ? ' · ' + esc(evTitle(e)) : ''}</span>`;
    const long = !encryptedEvent(e) && (e.full_len || 0) > 240;
    return `<div class="ev k-${e.kind} ${open ? 'open' : ''}" data-idx="${e.idx}"><div class="t" title="${esc(I18N.date(e.ts, 'dateTime'))}">${hm(e.ts)}</div><div>
      <div class="h">${e.kind === 'xread' ? `${whoBadge(e.to)}<span class="kind">${t('board.feed.xread', { author: esc(agentName(e.from)), title: esc(e.title) })}</span>` : `${whoBadge(e.from)}<span class="faint">→</span>${whoBadge(e.to)}${title}`}</div>
      <div class="body">${encryptedEvent(e) ? encryptedNote(e) : esc(evText(e))}</div>${long ? `<div class="more">${open ? t('board.feed.less') : t('board.feed.more')}${e.full_len > 1500 ? ' · ' + t('board.feed.fullLen', { count: e.full_len }) : ''}</div>` : ''}</div></div>`;
  }).join('') || `<div class="empty">${t('board.feed.empty')}</div>`;
  $('#feedItems').scrollTop = keepTop;
  wireEncryptedNotes($('#feedItems'), items);
  $('#feedItems').querySelectorAll('.who-b.agent').forEach(b => b.onclick = ev => { ev.stopPropagation(); openDrawer(b.dataset.agent); });
  $('#feedItems').querySelectorAll('.more').forEach(m => m.onclick = async ev => {
    const el = m.closest('.ev'), idx = +el.dataset.idx;
    if (ui.openEvents.has(idx)) { ui.openEvents.delete(idx); renderFeed(); return; }
    ui.openEvents.add(idx);
    const e = S.feed.find(x => x.idx === idx);
    if (e && e.full_len > 1500) { try { const full = await api('/api/event?idx=' + idx); e.text = full.text; } catch {} }
    renderFeed();
  });
}

// ---------- timeline ----------
let TL = null;
// key, seconds (0 = decided when it is asked for: this debate, all of the session, a range that was picked). The first TL_TABS are always tabs; the others are tabs on a wide page and a box on a narrow one
const TL_WINS = [['30m', 1800], ['2h', 7200], ['debate', 0], ['12h', 43200], ['24h', 86400], ['3d', 259200], ['7d', 604800], ['all', 0], ['custom', 0]];
const TL_TABS = 4, TL_NARROW = 760;
const TL_MAX = 31 * 86400;                   // the longest range that is picked (the longest the server answers for a range that has an end)
const TL_ROWS = 60;                          // a long range shows this many rows (the most recently active) until "show all"
const TL_BIN_SPAN = 13 * 3600;               // the server sends a range longer than this binned, and (this debate) with the lanes of all the agents: the page picks the debate's own
const TL_LATE = 600;                         // a range that ended less than this ago is asked for again by the refresh: records arrive late (a command is written when it ends)
const TL_EARLIEST = 946684800, TL_LATEST = 4102444800;      // a time that was kept is believed between 2000 and 2100: Date shows more, but nothing here happened then
const tlWinOf = k => TL_WINS.find(w => w[0] === k) || TL_WINS[2];
const tlTime = v => typeof v === 'number' && isFinite(v) && v >= TL_EARLIEST && v <= TL_LATEST && !isNaN(new Date(v * 1000).getTime()) ? v : null;
// A picked range: it starts and ends in that order, an open end (null) is "up to now", and it is at most TL_MAX long
const tlOk = (from, to, n) => from != null && from < (to == null ? n : to) && (to == null ? n : to) - from <= TL_MAX;
// What was kept in the browser is only believed when it makes sense: a window this page knows, times that Date can show, a picked range that runs forward and is not too long
(function tlInit() {
  if (!TL_WINS.some(w => w[0] === ui.tlWin)) ui.tlWin = 'debate';
  ui.tlEnd = tlTime(ui.tlEnd); ui.tlFrom = tlTime(ui.tlFrom); ui.tlTo = tlTime(ui.tlTo);
  if (!tlOk(ui.tlFrom, ui.tlTo, now())) ui.tlFrom = ui.tlTo = null;
  if (ui.tlWin === 'custom' && ui.tlFrom == null) ui.tlWin = 'debate';
  if (!tlWinOf(ui.tlWin)[1] || (ui.tlEnd != null && ui.tlEnd - tlWinOf(ui.tlWin)[1] < TL_EARLIEST)) ui.tlEnd = null;     // a step back is of a window that has a length
})();
const tlSave = () => { store.set('tlWin', ui.tlWin); store.set('tlEnd', ui.tlEnd); store.set('tlFrom', ui.tlFrom); store.set('tlTo', ui.tlTo); };
// The range on show: since, and until (null = up to now, an open end: it is not a time, so the range goes on with the clock). A window ends at now unless a step moved it into the past
// (ui.tlEnd); a picked range ends where it was picked, or is open. `n` is now: one moment for all that one action works out
function tlRaw(n) {
  const k = ui.tlWin, w = tlWinOf(k);
  if (k === 'custom') return { since: ui.tlFrom, until: ui.tlTo != null && ui.tlTo < n ? ui.tlTo : null };
  if (w[1]) { const end = ui.tlEnd != null && ui.tlEnd < n ? ui.tlEnd : null; return { since: (end || n) - w[1], until: end }; }
  if (k === 'all') return { since: ((S && S.session.started) || n - 7200) - 60, until: null };
  const d = currentDebate();
  const ids = d ? S.agents.filter(a => inDebate(a, d)) : [];
  const t0 = ids.length ? Math.min(...ids.map(a => a.spawn_ts || a.first_ts || n)) : n - 7200;
  return { since: t0 - 120, until: null };
}
const tlSane = r => r.since != null && tlTime(r.since) != null && (r.until == null || (tlTime(r.until) != null && r.since < r.until));
function tlRange(n = now()) {
  let r = tlRaw(n);
  if (!tlSane(r) && ui.tlWin !== 'debate') { ui.tlWin = 'debate'; ui.tlEnd = ui.tlFrom = ui.tlTo = null; tlSave(); r = tlRaw(n); }       // values that do not fit together: the default window
  return tlSane(r) ? r : { since: n - 7200, until: null };
}
const tlSince = () => tlRange().since;
// One step: a range of its own length into the past (dir -1) or toward now (+1, which stops at now: a step that lands within a minute of it, or a thousandth of the length, ends open).
// The window of a debate becomes a picked range when it is stepped; all of a session has nothing before it
function tlStep(dir) {
  const n = now(), r = tlRange(n), k = ui.tlWin;
  if (k === 'all' || !S) return;
  const len = Math.max(60, Math.min((r.until || n) - r.since, TL_MAX));
  let from, to;
  if (dir < 0) { to = r.since; from = to - len; if (from < TL_EARLIEST) return; }
  else if (r.until == null) return;
  else { from = r.since + len; to = r.until + len; if (to >= n - Math.max(60, len / 1000)) { to = null; from = n - len; } }
  if (tlWinOf(k)[1]) ui.tlEnd = to;
  else { ui.tlWin = 'custom'; ui.tlFrom = from; ui.tlTo = to; ui.tlEnd = null; }
  tlSave(); tlControls(); loadTimeline();
}
function tlPick(k) {
  const n = now();
  ui.tlWin = k; ui.tlEnd = null; ui.tlAll = false;
  if (k === 'custom' && !tlOk(ui.tlFrom, ui.tlTo, n)) { ui.tlTo = null; ui.tlFrom = n - 86400; }          // the last 24 hours, and still going
  tlSave(); tlControls(); loadTimeline();
}
// The picked range, as the date-time inputs hold it (the local time, to the minute)
const p2 = n => String(n).padStart(2, '0');
const tlInputOf = ts => { const d = new Date(ts * 1000); return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate()) + 'T' + p2(d.getHours()) + ':' + p2(d.getMinutes()); };
const tlTimeOf = v => { const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(v || ''); return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]).getTime() / 1000 : null; };
function tlCustom() {
  const n = now(), nm = Math.floor(n / 60) * 60;
  const from = tlTimeOf($('#tlFrom').value);
  let to = tlTimeOf($('#tlTo').value);
  if (ui.tlTo == null && $('#tlTo').value === ui.tlToShown) to = null;       // an end that is open and was not touched stays open (the input shows the minute it was set)
  else if (to != null && to >= nm) to = null;                                  // this minute or later is "up to now", never a time that is past in a few seconds
  const bad = tlTime(from) == null || (to != null && tlTime(to) == null) || !tlOk(from, to, n);
  ['#tlFrom', '#tlTo'].forEach(q => { const el = $(q); if (el.setAttribute) el.setAttribute('aria-invalid', bad ? 'true' : 'false'); });
  $('#tlHint').textContent = bad ? t('board.tl.custom.bad') : '';
  if (bad) return;                                                             // only a range that is one is kept and asked for
  ui.tlFrom = from; ui.tlTo = to; tlSave(); tlControls(); loadTimeline();
}
// The window picker: tabs (all of the windows on a wide page; the first four and a box for the rest on a narrow one), the step buttons, the picked range's inputs, and the range on show when it is not "up to now".
// Drawn again only when what it shows changed, so a box that is open is not closed by the 15-second refresh
function tlControls() {
  const narrow = innerWidth < TL_NARROW, room = isRoom(currentDebate()), r = tlRange(), custom = ui.tlWin === 'custom';
  const label = k => t('board.tl.win.' + (k === 'debate' && room ? 'room' : k));
  const sig = [narrow, ui.tlWin, room, t('board.tl.win.30m')].join('|');
  if (sig !== ui.tlSig) {
    ui.tlSig = sig;
    tabBar($('#tlWin'), TL_WINS.filter((w, i) => !narrow || i < TL_TABS).map(([k]) => [k, label(k)]), ui.tlWin, tlPick);
    if (narrow) {
      const longer = TL_WINS.slice(TL_TABS).map(([k]) => k), on = longer.includes(ui.tlWin);
      $('#tlWin').insertAdjacentHTML('beforeend', `<select id="tlMore" class="sel tl-more${on ? ' on' : ''}" aria-label="${esc(t('board.tl.more'))}"><option value="" disabled${on ? '' : ' selected'}>${esc(t('board.tl.more'))}</option>`
        + longer.map(k => `<option value="${k}"${k === ui.tlWin ? ' selected' : ''}>${esc(label(k))}</option>`).join('') + '</select>');
      const box = $('#tlMore');
      if (box) box.onchange = () => { if (box.value) tlPick(box.value); };
    }
  }
  $('#tlCustom').hidden = !custom;
  const picked = custom ? ui.tlFrom + '|' + ui.tlTo : '';
  if (custom && picked !== ui.tlPicked) {                     // the inputs are set when the picked range changed (a step, a pick), never under the hand of someone who is typing in them
    ui.tlPicked = picked;
    $('#tlFrom').value = tlInputOf(ui.tlFrom); $('#tlTo').value = ui.tlToShown = tlInputOf(ui.tlTo != null ? ui.tlTo : now());       // an open end shows this minute
    $('#tlHint').textContent = ''; ['#tlFrom', '#tlTo'].forEach(q => { const el = $(q); if (el.setAttribute) el.setAttribute('aria-invalid', 'false'); });
  }
  if (custom) $('#tlTo').max = tlInputOf(now());
  $('#tlPrev').disabled = ui.tlWin === 'all';
  $('#tlNext').disabled = ui.tlWin === 'all' || r.until == null;
  $('#tlRange').textContent = r.until != null || custom ? dayTime(r.since) + ' – ' + dayTime(r.until || now()) : '';
}
(function tlBind() {
  const on = (q, ev, f) => { const el = $(q); if (el) el.addEventListener(ev, f); };
  on('#tlPrev', 'click', () => tlStep(-1)); on('#tlNext', 'click', () => tlStep(1));
  on('#tlFrom', 'change', tlCustom); on('#tlTo', 'change', tlCustom);
})();
let tlReq = null;                            // Timeline request in flight { key, g }
const tlHasAll = () => !!TL && (TL.lanes_total == null || TL.lanes.length >= TL.lanes_total);      // the answer has every lane (a long range comes with the most recently active ones unless all were asked for)
// Whether the request asks for the lanes of all the agents: when it was asked for, and for a long window of a debate (the page keeps the debate's own, and the server's most recently active
// ones may be none of them)
const tlWantAll = r => ui.tlAll || (ui.tlWin === 'debate' && (r.until || now()) - r.since > TL_BIN_SPAN);
async function loadTimeline() {
  if (!S) return;
  tlControls();
  // The 15 s refresh waits if a request for the same window has not finished (so a response slower than the period is not pushed aside and discarded every time).
  // After a change of window or debate it is a different request, so a new one is sent and the late response of the earlier one is dropped.
  // A range that ended some time ago does not change: it is asked for again only when it is another range (the live refresh does not move it, and has nothing new to bring);
  // one that ended a few minutes ago is asked for again, for the records that come late
  const n = now(), d = currentDebate(), r = tlRange(n), since = Math.round(r.since), until = r.until != null ? Math.round(r.until) : null, all = tlWantAll(r);
  const key = ui.tlWin + '|' + (d ? d.root : '') + '|' + (until || '') + '|' + (ui.tlWin === 'custom' ? since : '') + '|' + (all ? 'all' : '');
  if (until != null && n - until >= TL_LATE && TL && TL.since === since && TL.until === until && (!all || tlHasAll())) { renderTimeline(); return; }
  if (tlReq && tlReq.key === key && isLatest('timeline', tlReq.g)) return;
  const g = nextGen('timeline'), req = tlReq = { key, g };
  let T = null;
  try { T = await apiGuarded('/api/timeline?since=' + since + (until != null ? '&until=' + until : '') + (all ? '&all=1' : '')); } catch {}
  if (tlReq === req) tlReq = null;
  if (!T || !isLatest('timeline', g)) return;
  TL = T;
  renderTimeline();
}
// The ticks of half a day or more: local midnight and noon (12 h), or local midnight of the days that count in whole steps (the day number is a multiple of the step). They are counted on the
// calendar: a day is not always 86400 s (the clocks change), and a tick at midnight must not be on 23:00 of the day before
function tlLocalTicks(t0, t1, step) {
  const out = [], days = step / 86400, d = new Date(t0 * 1000);
  d.setHours(0, 0, 0, 0);
  for (let i = 0; i < 5000; i++, d.setDate(d.getDate() + 1), d.setHours(0, 0, 0, 0)) {
    const mid = d.getTime() / 1000;
    if (mid > t1 + 86400) break;
    if (days < 1) [0, 12].forEach(h => { const x = new Date(d); x.setHours(h, 0, 0, 0); out.push(x.getTime() / 1000); });
    else if (Math.round(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86400000) % days === 0) out.push(mid);
  }
  return out.filter(tt => tt >= t0 && tt <= t1);
}
function renderTimeline() {
  if (!TL) return;
  const box = $('#tlBox'), W = Math.max(300, box.clientWidth || 700), labelW = W < 560 ? 92 : 128, rowH = 24, pad = 22;
  const past = TL.until != null;                      // a range that ends before now: no "now" line, and what is live now was not live then
  const t0 = TL.since, t1 = past ? TL.until : now(), span = Math.max(60, t1 - t0);
  const x0 = ts => labelW + (ts - t0) / span * (W - labelW - 10);
  const x = TL.binned ? ts => Math.round(x0(ts) * 10) / 10 : x0, tsAttr = TL.binned ? Math.round : ts => ts;     // thousands of marks: a tenth of a pixel and a second are enough, and the drawing is half the size
  const d = currentDebate();
  let lanes = TL.lanes.map(l => ({ ...l, a: agentById(l.id) })).filter(l => l.a)
    .filter(l => ui.tlWin !== 'debate' || !d || inDebate(l.a, d) || l.a.status === 'running');
  // A long range has a lane for every agent that was at work in it, which can be hundreds: the answer holds the most recently active ones (TL_ROWS) until "show all", which asks for the rest.
  // The lanes of a binned answer come in the order of the work in the range (the latest first), which the server works out from the ticks themselves: a tick of a long range is the first of its
  // column, and cannot say who worked last. The ones kept are the first TL_ROWS of what is left after the page took out those of other work (this debate) and of agents it does not know, in
  // that order; the order of the rows on the page (by name) is made after the cut. The count is of the rows that can be shown: the server's own count when it cut the lanes (then none was
  // taken out here), else the lanes that are left
  const have = tlHasAll(), expanded = ui.tlAll && have, count = have ? lanes.length : TL.lanes_total;
  if (TL.binned && !expanded && lanes.length > TL_ROWS) lanes = lanes.slice(0, TL_ROWS);
  const capped = !!TL.binned && !expanded && count > lanes.length;
  lanes.sort((p, q) => (p.a.tag || p.a.title).localeCompare(q.a.tag || q.a.title));
  const H = pad + (lanes.length + 1) * rowH + 6;
  const COL = { read: 'var(--blue)', write: 'var(--green)', bash: 'var(--amber)', web: 'var(--purple)', msg: 'var(--orange)', recv: 'var(--orange)', error: 'var(--red)', other: 'var(--faint)' };
  let g = '';
  // Time ticks
  // Over a day the labels carry the date (so does a day that goes across midnight), so fewer fit: the number is by the width of the card, a label of a date and a time taking more than one of a
  // date only. From half a day on the ticks are at the local hours of the day (midnight, noon) counted on the calendar, not at multiples of the time since 1970 in UTC
  const stepCands = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 1209600, 2592000];
  const dated = span > 90000 || (span > 46800 && new Date(t0 * 1000).toDateString() !== new Date(t1 * 1000).toDateString()), axisW = W - labelW - 10;
  const fits = s => span / s <= (dated ? Math.max(2, Math.min(8, Math.floor(axisW / (s >= 86400 ? 44 : 84)))) : 8);
  const step = stepCands.find(fits) || stepCands[stepCands.length - 1];
  const tickText = tt => !dated ? hm(tt) : step >= 86400 ? monthDay(tt) : dayTime(tt);
  const ticks = step >= 43200 ? tlLocalTicks(t0, t1, step) : [];
  if (step < 43200) for (let tt = Math.ceil(t0 / step) * step; tt <= t1; tt += step) ticks.push(tt);
  ticks.forEach(tt => {
    g += `<line x1="${x(tt)}" x2="${x(tt)}" y1="${pad - 6}" y2="${H}" stroke="var(--line)" /><text x="${x(tt) + 3}" y="${pad - 9}" font-size="10.5" fill="var(--muted)">${tickText(tt)}</text>`;
  });
  const row = (i, name, st, cx) => {
    const y = pad + i * rowH, fill = st === 'running' ? 'var(--green)' : st === 'stalled' || st === 'interrupted' ? 'var(--amber)' : st === 'orch' ? 'var(--orange)' : 'var(--faint)';
    return `<rect x="0" y="${y}" width="${W}" height="${rowH}" fill="${i % 2 ? 'transparent' : 'var(--panel2)'}" opacity=".6"/>` +
      (cx ? `<path d="M8 ${y + rowH / 2 - 4.2} l4.2 4.2 -4.2 4.2 -4.2 -4.2z" fill="${fill}"/>`     // Codex: diamond
        : `<circle cx="8" cy="${y + rowH / 2}" r="3.5" fill="${fill}"/>`) +
      `<text x="18" y="${y + rowH / 2 + 4}" font-size="11.5" fill="var(--text)">${esc(name.length > 16 ? name.slice(0, 15) + '…' : name)}</text>`;
  };
  // Orchestrator row
  g += row(0, t('common.orchestrator'), 'orch', orchCx());
  TL.orch.forEach(([ts, k]) => {
    const y = pad + rowH / 2, cx = x(ts);
    const col = k === 'user_say' || k === 'user_answer' ? 'var(--purple)' : k === 'handback' ? 'var(--teal)' : 'var(--orange)';
    g += `<rect x="${cx - 2.5}" y="${y - 6}" width="5" height="12" rx="1.5" fill="${col}" data-ts="${tsAttr(ts)}" data-k="ev:${esc(k)}"/>`;
  });
  lanes.forEach((l, i) => {
    const y = pad + (i + 1) * rowH, mid = y + rowH / 2;
    g += row(i + 1, (l.a.tag ? l.a.tag + ' ' : '') + l.a.title, l.a.status, isCx(l.a));
    const s = Math.max(t0, l.spawn_ts || t0), e = isLive(l.a) && !past ? t1 : (l.a.last_ts || t1);
    if (e > t0 && s <= t1) g += `<line x1="${x(s)}" x2="${x(Math.min(e, t1))}" y1="${mid}" y2="${mid}" stroke="var(--line2)" stroke-width="2"/>`;
    l.ticks.forEach(([ts, c, n]) => { const more = n > 1 ? ` data-n="${n}"` : ''; if (ts >= t0) g += c === 'recv'     // n: how many ticks of that kind the server put into this one (a long range)
      ? `<path d="M${x(ts)} ${mid - 6} l5 6 -5 6 -5 -6z" fill="var(--orange)" data-ts="${tsAttr(ts)}" data-k="recv"${more}/>`
      : `<line x1="${x(ts)}" x2="${x(ts)}" y1="${mid - 6}" y2="${mid + 6}" stroke="${COL[c] || COL.other}" stroke-width="1.6" data-ts="${tsAttr(ts)}" data-k="${esc(c)}"${more}/>`; });
    l.writes.forEach(([ts, p]) => { if (/\/r\d+\/[^/]+\.md$/.test(p)) g += `<circle cx="${x(ts)}" cy="${mid}" r="4.5" fill="var(--green)" stroke="var(--panel)" stroke-width="1.5" data-ts="${tsAttr(ts)}" data-k="saved" data-p="${esc(p)}"/>`; });
    l.handbacks.forEach(ts => g += `<path d="M${x(ts) - 5} ${mid - 7} h10 l-5 7z" fill="var(--teal)" data-ts="${tsAttr(ts)}" data-k="handback"/>`);
  });
  if (!past) g += `<line x1="${x(t1)}" x2="${x(t1)}" y1="${pad - 6}" y2="${H}" stroke="var(--red)" stroke-dasharray="3 3" opacity=".7"/>`;
  const rows = TL.binned && count > TL_ROWS ? `<div class="tl-rows"><span>${capped ? t('board.tl.capped', { shown: lanes.length, count }) : ''}</span><button type="button" id="tlAll">${capped ? t('board.tl.showAll', { count }) : t('board.tl.showFewer', { shown: TL_ROWS })}</button></div>` : '';
  box.innerHTML = lanes.length || TL.orch.length ? `<svg viewBox="0 0 ${W} ${H}" height="${H}">${g}</svg>${rows}` : `<div class="empty">${t('board.tl.empty')}</div>`;
  const allBtn = $('#tlAll', box);
  if (allBtn) allBtn.onclick = () => { ui.tlAll = !ui.tlAll; if (ui.tlAll && !tlHasAll()) loadTimeline(); else renderTimeline(); };
  // Legend: the ◆ entry is added only when there is a Codex row
  const cxLegend = $('#tlCx'), wantCx = orchCx() || lanes.some(l => isCx(l.a));
  if (wantCx && !cxLegend) $('.tl .legend').insertAdjacentHTML('beforeend', `<span id="tlCx">${CXG.replace('class="cxg"', 'class="cxg" style="margin:0"')}Codex</span>`);
  else if (!wantCx && cxLegend) cxLegend.remove();
}
// The tip of a mark is worked out when the pointer is over it, from what the mark carries (its time, its kind, how many it stands for), and the handlers are one set on the box: a long range
// has thousands of marks, and a text and three listeners for each was most of the cost of drawing one
const tlTipText = d => {
  const k = d.k || '', what = k === 'recv' ? t('board.tl.recv') : k === 'saved' ? t('board.tl.saved', { path: d.p }) : k === 'handback' ? t('board.tl.handback')
    : k.startsWith('ev:') ? (I18N.has('board.tl.ev.' + k.slice(3)) ? t('board.tl.ev.' + k.slice(3)) : k.slice(3)) : k;
  return hm(+d.ts) + ' · ' + what + (d.n ? ' ×' + d.n : '');
};
(function tlTip() {
  const box = $('#tlBox'), tip = $('#tip');
  if (!box || !tip) return;
  const mark = ev => ev.target && ev.target.closest ? ev.target.closest('[data-ts]') : null;
  box.addEventListener('mouseover', ev => { const m = mark(ev); if (m) { tip.textContent = tlTipText(m.dataset); tip.style.display = 'block'; } });
  box.addEventListener('mousemove', ev => { if (mark(ev)) { tip.style.left = Math.min(ev.clientX + 12, innerWidth - 390) + 'px'; tip.style.top = ev.clientY + 12 + 'px'; } });
  box.addEventListener('mouseout', ev => { const m = mark(ev); if (m && !(ev.relatedTarget && m.contains && m.contains(ev.relatedTarget))) tip.style.display = 'none'; });
})();

// ---------- agent drawer ----------
let DETAIL = null, DETAIL_ID = null;         // DETAIL is the detail of the agent DETAIL_ID (kept together so the drawer never draws a mix while it switches to another agent)
async function openDrawer(id) {
  ui.drawer = id; ui.dTab = ui.dTab || 'overview';
  $('#drawer').classList.add('open');
  renderAgents();
  await refreshDrawer();
}
function closeDrawer() { ui.drawer = null; DETAIL = null; DETAIL_ID = null; nextGen('drawer'); $('#drawer').classList.remove('open'); renderAgents(); }
let drawerReq = null;                        // Drawer request in flight { id, g }
async function refreshDrawer() {
  const id = ui.drawer;
  if (!id) return;
  // The 5 s refresh waits if a request for the same agent has not finished (so a response slower than the period is not pushed aside and discarded every time).
  // If another agent was opened, or the drawer was closed and reopened, it is a different request, so a new one is sent and the late response of the earlier one is dropped
  if (drawerReq && drawerReq.id === id && isLatest('drawer', drawerReq.g)) return;
  const g = nextGen('drawer'), req = drawerReq = { id, g };
  let D = null;
  try { D = await apiGuarded('/api/agent?id=' + id); } catch {}
  if (drawerReq === req) drawerReq = null;
  if (!D || !isLatest('drawer', g) || ui.drawer !== id) return;   // Meanwhile another agent was opened, or the drawer was closed
  DETAIL = D; DETAIL_ID = id;
  renderDrawer();
}
const linkRule = rule => I18N.has('board.link.' + rule) ? t('board.link.' + rule) : rule || '?';
const bytes = n => n >= 1e9 ? (n / 1e9).toFixed(1) + ' GB' : Math.round(n / 1e6) + ' MB';
// Who handed a run its instruction, by = [session id of the tree, node]: a node is an agent, no node is the main session of that tree (the orchestrator, or a child run of its own)
const byName = by => !by ? '' : by[1] ? agentName(by[1]) : S && by[0] === S.session.id ? t('common.orchestrator') : agentById(by[0]) ? agentName(by[0]) : String(by[0]).slice(0, 8);
function linkText(D, a) {                    // A Codex agent or a `claude -p` run: how it was linked and how sure that is, and whether a large Codex transcript was read from its tail only
  if (!isCx(D) && !isCx(a) && a.origin !== 'cli') return '';
  const l = D.link || a.link, lk = a.link || l, sk = D.partial && D.partial.skipped_bytes, cls = linkClass(lk);
  const rule = l ? linkRule(ruleWhy(l.rule, lk)) : '';
  return (l ? `<span title="${esc(linkTip(lk))}">${t('board.link.line', { rule: esc(rule) })}${cls ? ' · ' + t('board.link.class.' + cls) : ''}${l.bash_ts ? ' · ' + (l.parent_kind === 'codex' ? 'exec' : 'Bash') + ' ' + hm(l.bash_ts) : ''}${l.dt != null ? ' · ' + t('board.link.dt', { n: (+l.dt).toFixed(1) }) : ''}</span>` : '')
    + (sk ? `<span>${t('board.link.partial', { size: bytes(sk) })}</span>` : '');
}
// Where an agent hangs (launched by …) and how many runs its record holds with who handed over the last one
function lineageText(a) {
  const runs = a.runs && a.runs.length > 1 ? `<span>${t('board.drawer.runs', { count: a.runs.length })}${a.by ? ' · ' + t('board.drawer.run.by', { who: esc(byName(a.by)) }) : ''}</span>` : '';
  const base = p => String(p || '').split('/').filter(Boolean).pop() || '';
  const together = a.launch && S ? S.agents.filter(x => x.id !== a.id && x.launch === a.launch).length : 0;                  // the ones started by the same call
  const placed = a.placed ? `<span title="${esc(t('board.placed.title', { whys: (a.placed.whys && a.placed.whys.length ? a.placed.whys : [a.placed.why]).map(placedWhy).join(', ') }))}">${t(a.placed.sure ? 'board.drawer.placed.sure' : 'board.drawer.placed', { unit: esc(base(a.placed.topic || a.placed.unit)), why: esc(placedWhy(a.placed.why)) })}</span>` : '';
  const tag = a.room_tag ? `<span>${t(a.room_tag.seat ? 'board.drawer.roomTag.seat' : 'board.drawer.roomTag', { room: esc(a.room_tag.room), seat: esc(a.room_tag.seat || '') })}</span>` : '';
  return (a.parent ? `<span>${t('board.agents.launchedBy', { name: esc(agentName(a.parent)) })}</span>` : '') + runs + (together ? `<span>${t('board.drawer.together', { count: together })}</span>` : '') + placed + tag;
}
function renderDrawer() {
  const a = agentById(ui.drawer), D = DETAIL;
  if (!a || !D || DETAIL_ID !== ui.drawer) return;
  $('#dTitle').innerHTML = `<span class="dot ${a.status}${cxCls(a)}"></span>${a.tag ? `<span class="a-tag">${esc(a.tag)}</span>` : ''}<span>${esc(a.title)}</span>${guessMark(a)}<span class="pill ${a.status}" title="${esc(stateWhy(a))}">${esc(stateLabel(a))}</span><button class="icon-btn close" id="dClose">${t('common.closeEsc')}</button>`;
  $('#dClose').onclick = closeDrawer;
  $('#dSub').innerHTML = `<span>${t('board.drawer.started', { time: hm(a.spawn_ts) })}</span><span>${t('board.drawer.lastActivity', { ago: ago(a.last_ts) })}</span><span>${t('board.agents.tools', { n: a.tool_count })}</span><span>${t('board.drawer.ctx', { ctx: kfmt(a.ctx), limit: kfmt(a.tokens ? a.tokens.ctx_limit : 0) })}</span><span>${t('board.drawer.tokens', { input: kfmt(tokIn(a.tokens)), output: kfmt(a.tokens ? a.tokens.output : 0), cost: usd(a.tokens ? a.tokens.cost : 0) })}</span><span>${esc(modelName(a.model))} ${esc(a.effort || '')}</span>${linkText(D, a)}${lineageText(a)}<span class="faint mono">${esc(a.id)}</span>`;
  const tabs = [['overview', t('board.drawer.tab.overview')], ['activity', t('board.drawer.tab.activity')], ['messages', t('board.drawer.tab.messages', { count: D.orch_msgs.length + D.handbacks.length + 1 })], ['texts', t('board.drawer.tab.texts', { count: D.texts.length })]];
  tabBar($('#dTabs'), tabs, ui.dTab, k => { ui.dTab = k; renderDrawer(); });
  const body = $('#dBody');
  const keep = body.scrollTop;
  if (ui.dTab === 'overview') {
    const reports = D.writes.filter(w => /\.md$/.test(w.path)).reduce((m, w) => (m[w.path] = w, m), {});
    const reportReads = D.reads.filter(r => /\/r\d+\/[^/]+\.md$/.test(r.path));
    // Why it is not working (only where there is something to say: interrupted, unknown, ended without an answer ...) and, for a run that was started more than once, each run
    const why = stateWhy(a), stateBox = why ? `<div class="box"><h4>${t('board.drawer.stateBox')}</h4><div>${esc(stateLabel(a))} — ${esc(why)}${a.reason === 'limit' ? ` · ${esc(heldNote(a))}` : ''}</div></div>` : '';
    const runsBox = a.runs && a.runs.length > 1 ? `<div class="box"><h4>${t('board.drawer.runsBox')}</h4>${a.runs.map(r => `<div class="mono" style="font-size:12px"><span class="faint">#${r.n}</span> ${hm(r.start)} → ${r.end ? hm(r.end) : t('board.drawer.run.now')} · ${I18N.has('board.drawer.run.' + r.kind) ? t('board.drawer.run.' + r.kind) : esc(r.kind)}${r.by ? ' · ' + t('board.drawer.run.by', { who: esc(byName(r.by)) }) : ''}</div>`).join('')}</div>` : '';
    body.innerHTML = `${stateBox}
      <div class="box"><h4>${t('board.drawer.now')}</h4>${a.current ? `<div class="mono" style="font-size:12.5px">${esc(a.current.name || a.current.kind)} ${esc(toolText(a.current))}</div><div class="faint" style="font-size:12px">${ago(a.current.ts)}</div>` : '-'}</div>
      <div class="box"><h4>${t('board.drawer.written')}</h4>${Object.values(reports).map(w => `<div><span class="flink" data-path="${esc(w.path)}">${esc(w.short)}</span> <span class="faint" style="font-size:11.5px">${hm(w.ts)}</span></div>`).join('') || `<span class="faint">${t('board.drawer.noneYet')}</span>`}</div>
      <div class="box"><h4>${t('board.drawer.reads')}</h4>${reportReads.map(r => `<div><span class="flink" data-path="${esc(r.path)}">${esc(r.short)}</span> <span class="faint" style="font-size:11.5px">${hm(r.ts)}</span></div>`).join('') || `<span class="faint">${t('board.drawer.none')}</span>`}</div>
      <div class="box"><h4>${t('board.drawer.tokensBox')}</h4><div class="kv">${a.tokens ? [
        [t('board.drawer.kv.ctx'), `${kfmt(a.tokens.ctx)} / ${kfmt(a.tokens.ctx_limit)}`], [t('board.drawer.kv.input'), kfmt(tokIn(a.tokens))],
        [t('board.drawer.kv.cacheRead'), kfmt(a.tokens.cache_read)], [t('board.drawer.kv.cacheWrite'), kfmt(a.tokens.cache_write)], [t('board.drawer.kv.newInput'), kfmt(a.tokens.input)],
        [t('board.drawer.kv.output'), kfmt(a.tokens.output)], ...(a.tokens.reasoning > 0 ? [[t('board.drawer.kv.reasoning'), kfmt(a.tokens.reasoning)]] : []), [t('board.drawer.kv.calls'), t('unit.call', { count: a.tokens.calls })],
        [t('board.drawer.kv.cost'), `<b style="color:var(--green)">${usd(a.tokens.cost)}</b> <span class="faint">${t('board.drawer.costParts', { read: usd(a.tokens.cost_read), write: usd(a.tokens.cost_write), input: usd(a.tokens.cost_input), output: usd(a.tokens.cost_output) })}</span>`],
        [t('board.drawer.kv.models'), modelCosts(a.tokens).map(mc => `${esc(modelLabel(mc))} ${usd(mc.cost)}`).join(' · ') || '-'],
        ...(a.tokens.adv_calls ? [[t('board.drawer.kv.advisor'), t('board.drawer.callsIo', { calls: t('unit.call', { count: a.tokens.adv_calls }), input: kfmt(a.tokens.adv_input), output: kfmt(a.tokens.adv_output) })]] : []),
        ...(a.tokens.guardian && a.tokens.guardian.calls ? [[t('board.drawer.kv.review'), `${t('board.drawer.callsIo', { calls: t('unit.call', { count: a.tokens.guardian.calls }), input: kfmt((a.tokens.guardian.input || 0) + (a.tokens.guardian.cache_read || 0)), output: kfmt(a.tokens.guardian.output || 0) })} <span class="faint">${t('board.drawer.unpriced')}</span>`]] : []),
      ].map(([k, v]) => `<span class="muted">${k}</span><span>${v}</span>`).join('') : '-'}</div></div>
      <div class="box"><h4>${t('board.drawer.toolsBox')}</h4><div class="kv">${D.tool_counts.map(([n, c]) => `<span class="mono">${esc(n)}</span><span>${c}</span>`).join('')}</div></div>
      <div class="box"><h4>${t('board.drawer.recent')}</h4>${D.reads.slice(0, 15).map(r => `<div class="mono faint" style="font-size:11.5px">${hm(r.ts)} ${esc(r.short)}</div>`).join('')}</div>${runsBox}`;
  } else if (ui.dTab === 'activity') {
    body.innerHTML = [...D.activity].reverse().map(x => x.kind === 'text'
      ? `<div class="act k-text"><span class="n">${hm(x.ts)}</span><span class="x">${esc(x.text)}</span></div>`
      : `<div class="act k-${x.kind}"><span class="n">${hm(x.ts)}</span><span class="n">${esc(x.kind === 'msg' ? t('board.drawer.gotMsg') : x.name)}</span><span class="x">${esc(toolText(x))}</span></div>`).join('');
  } else if (ui.dTab === 'messages') {
    const items = [{ ts: D.spawn_ts, h: t('board.drawer.msg.first'), body: D.spawn_prompt }]
      .concat(D.orch_msgs.map(m => ({ ts: m.ts, h: t('board.drawer.msg.orch', { summary: m.summary }), body: m.text })))
      .concat(D.handbacks.map(m => ({ ts: m.ts, h: t('board.drawer.msg.handback'), body: m.text, rep: 1 })))
      .sort((p, q) => (q.ts || 0) - (p.ts || 0));
    body.innerHTML = items.map(m => `<div class="box" style="${m.rep ? 'border-color:var(--teal)' : ''}"><h4>${esc(m.h)} <span class="faint">${hm(m.ts)}</span></h4><div class="md">${md(m.body || '')}</div></div>`).join('');
  } else {
    body.innerHTML = [...D.texts].reverse().map(x => `<div class="box"><h4>${hm(x.ts)}</h4><div class="md">${md(x.text)}</div></div>`).join('') || `<div class="empty">${t('board.drawer.none')}</div>`;
  }
  body.querySelectorAll('[data-path]').forEach(e => e.onclick = () => openFile(e.dataset.path, e));
  body.scrollTop = keep;
}

// ---------- shared modal and document view ----------
const modalVisible = el => el && el.isConnected !== false && !el.disabled && !el.hidden && !el.closest?.('[hidden], [inert]') && (!el.getClientRects || el.getClientRects().length > 0);
function modalReturnTargets(opener) {
  const idx = opener?.dataset?.encryptedIdx;
  if ($('#modalDialog').contains?.(opener)) {
    // Opening one message replaces the whole-conversation body. Return to its
    // equivalent button in the list, or the button that opened that conversation.
    return /^\d+$/.test(idx) ? [() => $(`#atalkList [data-encrypted-idx="${idx}"]`), ...ui.modalReturn] : ui.modalReturn;
  }
  const area = opener?.closest?.('#feedItems, #atalkList, #talkList');
  const eventIdx = idx || opener?.closest?.('[data-idx]')?.dataset.idx;
  const same = area && /^\d+$/.test(eventIdx) ? `#${area.id} [${idx ? 'data-encrypted-idx' : 'data-idx'}="${eventIdx}"]` : null;
  const fallback = { feedItems: '#feedFilter .on', atalkList: '#atalkBig', talkList: '#talkBig' }[area?.id];
  return [() => opener, () => same && $(same), () => opener?.id && document.getElementById(opener.id), () => fallback && $(fallback), () => $('#sessionSel')];
}
function beginModal(kind, title, sub, html, opener = document.activeElement) {
  const g = nextGen('modal');
  ui.modalReturn = modalReturnTargets(opener);
  ui.modalKind = kind;
  $('#modalBg').classList.add('open');
  $('#mTitle').textContent = title; $('#mSub').textContent = sub; $('#mBody').innerHTML = html;
  $('#mBody').scrollTop = 0;
  $('#mClose').focus?.();
  return g;
}
async function openFile(path, opener = document.activeElement) {
  const g = beginModal('file', path.split('/').slice(-3).join('/'), t('common.loading'), '', opener);
  try {
    const f = await api('/api/file?path=' + encodeURIComponent(path));
    if (!isLatest('modal', g)) return;   // Meanwhile something else was opened, or this was closed
    $('#mSub').textContent = t('board.file.sub', { short: f.short, lines: t('unit.line', { count: f.text.split('\n').length }), time: hm(f.mtime) });
    $('#mBody').innerHTML = md(f.text);
    $('#mBody').scrollTop = 0;
  } catch (e) { if (isLatest('modal', g)) $('#mSub').textContent = t('board.file.error', { message: e.message }); }
}
function closeModal() {
  const wasOpen = $('#modalBg').classList.contains('open'), targets = ui.modalReturn;
  nextGen('modal'); ui.modalKind = null; ui.modalReturn = []; $('#modalBg').classList.remove('open');
  if (wasOpen) for (const get of targets) {
    const el = get();
    if (!modalVisible(el)) continue;
    if (el.tabIndex < 0) el.setAttribute?.('tabindex', '-1');  // Clickable message/file rows can also be return targets.
    el.focus?.();
    if (document.activeElement === el) break;
  }
}
$('#mClose').onclick = closeModal;
$('#modalBg').onclick = e => { if (e.target.id === 'modalBg') closeModal(); };
document.addEventListener('keydown', e => {
  const open = $('#modalBg').classList.contains('open');
  if (e.key === 'Escape') {
    if (open) { e.preventDefault?.(); closeModal(); } else closeDrawer();
  } else if (e.key === 'Tab' && open) {
    const dialog = $('#modalDialog');
    const items = [...dialog.querySelectorAll('a[href], button, input, select, textarea, [tabindex]')].filter(el => el.tabIndex >= 0 && modalVisible(el));
    const first = items[0] || dialog, last = items[items.length - 1] || dialog;
    if (!items.includes(document.activeElement) || document.activeElement === (e.shiftKey ? first : last)) {
      e.preventDefault?.(); (e.shiftKey ? last : first).focus?.();
    }
  }
});
$('#themeBtn').onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = cur === 'light' ? 'dark' : 'light'; store.set('theme', document.documentElement.dataset.theme);
};
{ const th = store.get('theme', null); if (th) document.documentElement.dataset.theme = th; }

// ---------- user ↔ orchestrator conversation ----------
const TALK_KINDS = ['user_say', 'orch_say', 'orch_ask', 'user_answer', 'sys'];       // sys: a dimmed line of the board's own (a usage limit, an API error), neither side's message
function pinBottom(b, follow) {           // Stay pinned to the bottom even when the height changes because a font applies late (while follow() is true)
  const go = () => { if (follow()) b.scrollTop = b.scrollHeight; };
  go(); requestAnimationFrame(go); setTimeout(go, 300); setTimeout(go, 1200);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(go);
}
const talkToBottom = () => pinBottom($('#talkList'), () => ui.talkStick);
// At the bottom (within 40px) it follows new messages; when the user scrolls up it stops following. Reported through set(true/false)
function watchFollow(box, newBtn, set) {
  box.addEventListener('scroll', () => {
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    set(near);
    if (near) newBtn.hidden = true;
  });
}
const plain = tx => String(tx || '').replace(/```[\s\S]*?```/g, t('board.plain.code')).replace(/\*\*|__|`/g, '').replace(/^#+\s*/gm, '')
  .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/\n{3,}/g, '\n\n').trim();
const talkMe = e => e.kind === 'user_say' || e.kind === 'user_answer';
const talkWho = e => talkMe(e) ? t('common.you') : t('common.orchestrator');
const talkKind = e => kindLabel(e.kind);
const dayHeading = ts => { const d = new Date(ts * 1000); return t('time.dayHeading', { m: d.getMonth() + 1, d: d.getDate(), mon: I18N.date(ts, 'monthShort') }); };
async function loadTalk(older) {
  const g = nextGen('talk');
  try {
    const before = older && ui.talk.length ? '&before=' + ui.talk[0].idx : '';
    const r = await api('/api/talk?limit=80' + before);
    if (!isLatest('talk', g)) return;     // A newer request went out (so pressing the same "Load earlier" button twice does not append duplicates)
    ui.talk = older ? r.items.concat(ui.talk) : r.items;
    ui.talkMore = r.more; ui.talkLoaded = true;
    renderTalk({ keepOld: older });
  } catch {}
}
function mergeTalk() {          // Append only the newly arrived messages
  if (!ui.talkLoaded) return false;
  const last = ui.talk.length ? ui.talk[ui.talk.length - 1].idx : -1;
  const add = (S.feed || []).filter(e => TALK_KINDS.includes(e.kind) && e.idx > last);
  if (add.length) ui.talk = ui.talk.concat(add);
  return add.length > 0;
}
function talkHtml(items, o) {
  let html = o.more ? `<button class="tab more-old" data-old="1">${t('board.msg.older')}</button>` : '', prevDay = '', prev = null;
  items.forEach(e => {
    const day = dayHeading(e.ts);
    if (day !== prevDay) { html += `<div class="day">${day}</div>`; prevDay = day; prev = null; }
    if (e.kind === 'sys') { html += `<div class="sysline" data-idx="${e.idx}" title="${esc(I18N.date(e.ts, 'dateTime'))}">${esc(evTitle(e))}<span class="mt-time">${hm(e.ts)}</span></div>`; prev = null; return; }
    const me = talkMe(e), cont = prev && prev.me === me && e.ts - prev.ts < 600 && !['orch_ask', 'user_answer'].includes(e.kind);
    const badge = e.kind === 'orch_ask' || e.kind === 'user_answer' ? `<span class="tagk">${talkKind(e)}</span>` : '';
    html += `<div class="msg ${me ? 'me' : 'orch'}${e.kind === 'orch_ask' ? ' ask' : ''}${cont ? ' cont' : ''}" data-idx="${e.idx}" title="${t('board.msg.openTitle')}">` +
      (cont ? '' : `<div class="mh"><b>${talkWho(e)}</b>${badge}<span>${hm(e.ts)}</span></div>`) +
      (o.full ? `<div class="mt full md">${md(evText(e))}</div>` : `<div class="mt">${esc(plain(evText(e)))}</div>`) + '</div>';
    prev = { me, ts: e.ts };
  });
  return html || `<div class="empty">${t('board.talk.empty')}</div>`;
}
function wireTalk(root) {
  root.querySelectorAll('.msg').forEach(m => m.onclick = () => openTalkMsg(+m.dataset.idx, m));
  const old = root.querySelector('[data-old]'); if (old) old.onclick = () => loadTalk(true);
}
function renderTalk(opt = {}) {
  const box = $('#talkList');
  const sig = ui.talk.length + ':' + (ui.talk.length ? ui.talk[ui.talk.length - 1].idx : '') + ':' + ui.talkMore;
  if (sig === ui.talkSig && !opt.force) return renderTalkTime();
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40, first = !ui.talkSig;
  const oldH = box.scrollHeight, oldTop = box.scrollTop;
  box.innerHTML = talkHtml(ui.talk, { more: ui.talkMore });
  wireTalk(box);
  if (opt.keepOld) box.scrollTop = box.scrollHeight - oldH + oldTop;          // If items were prepended, stay where the reader was
  else if (first || atBottom || ui.talkStick) { ui.talkStick = true; talkToBottom(); $('#talkNew').hidden = true; }   // Follow the bottom
  else $('#talkNew').hidden = false;                                              // If reading further up, only show the notice
  ui.talkSig = sig;
  renderTalkTime();
}
function renderTalkTime() {
  const e = [...ui.talk].reverse().find(x => x.kind !== 'sys');            // the last thing somebody said (a system line is nobody's)
  $('#talkTime').textContent = e ? t('board.talk.last', { who: talkWho(e), time: hm(e.ts), ago: ago(e.ts) }) : '';
}
$('#talkNew').onclick = () => { ui.talkStick = true; talkToBottom(); $('#talkNew').hidden = true; };
watchFollow($('#talkList'), $('#talkNew'), v => { ui.talkStick = v; });
function showModal(title, sub, html, opener = document.activeElement) {         // One message or the whole conversation; the document and diagnosis views share the same focus lifecycle.
  return beginModal('talk', title, sub, html, opener);
}
async function openEventModal(e, title, opener = document.activeElement) {       // The full text of one event in the modal (shared by the two conversation cards)
  let text = evText(e);
  showModal(title, I18N.date(e.ts, 'dateTime'), md(text), opener);
  const g = gens.modal;                             // The number showModal raised: if another modal opens or this one closes meanwhile, the response is dropped
  if (!Array.isArray(e.questions) && !e.text_i18n && (e.full_len || 0) > text.length) {            // A cut-off text is fetched in full and drawn again (a question comes as structure, never cut)
    try { text = (await api('/api/event?idx=' + e.idx)).text; if (isLatest('modal', g)) $('#mBody').innerHTML = md(text); } catch {}
  }
}
async function openTalkMsg(idx, opener = document.activeElement) {
  const e = ui.talk.find(x => x.idx === idx);
  if (!e) return;
  return openEventModal(e, `${talkWho(e)} · ${talkKind(e)}`, opener);
}
$('#talkBig').onclick = () => {
  showModal(t('board.talk.bigTitle'), `${t('board.msg.loaded', { count: ui.talk.length })}${ui.talkMore ? ' · ' + t('board.msg.moreHint', { label: t('board.msg.older') }) : ''} · ${t('board.msg.clickHint')}`, '<div class="talk">' + talkHtml(ui.talk, { full: true }) + '</div>', $('#talkBig'));
  wireTalk($('#mBody'));
  $('#mBody').scrollTop = $('#mBody').scrollHeight;
};

// ---------- agent talk ----------
// A second card with the same grammar as the user ↔ orchestrator card: orchestrator ↔ agent and agent ↔ agent conversation (right of the office). The server's /api/talk?scope=agents gives the past,
// and the feed in the state gives the new events. Six kinds of event: spawn (task assignment) and orch_msg (message) = orchestrator → agent, handback (final report) and peer = agent → orchestrator,
// agent_msg = agent → agent (or orchestrator), xread = reading another agent's report
const ATALK_KINDS = ['spawn', 'orch_msg', 'handback', 'peer', 'agent_msg', 'xread'];
const atalkKind = k => k === 'agent_msg' ? t('kind.agent_msg.short') : ['spawn', 'orch_msg', 'handback', 'peer'].includes(k) ? t('kind.' + k) : k;
const ATALK_FILTERS = ['all', 'orch', 'peer'];
// Bubble side: orch (left, sent by the orchestrator) | agent (right, sent by an agent) | peer (centre, agent to agent, cross review)
const atalkSide = e => e.kind === 'xread' ? 'peer' : e.kind === 'agent_msg' ? (e.to === 'orch' ? 'agent' : 'peer') : e.from === 'orch' ? 'orch' : 'agent';
// Resolve to an agent id: the to of a Claude SendMessage may be a name the orchestrator wrote (T1-A). If it cannot be resolved, it is left as it is
const atalkId = id => {
  if (!id || id === 'orch' || id === 'user' || agentById(id)) return id;
  const l = String(id).toLowerCase(), a = S.agents.find(x => [x.tag, x.description].some(v => v && String(v).toLowerCase() === l));
  return a ? a.id : id;
};
const atalkEnds = e => [atalkId(e.from), atalkId(e.kind === 'spawn' ? (e.agent || e.to) : e.to)];   // [sender, receiver]. For a cross review: [author, reader]
const atalkName = id => id === 'orch' ? t('common.orchestrator') : id === 'user' ? t('common.you') : agentName(id);
// A name inside its span, then what the language adds after a subject (ko: the particle i/ga by the name's last sound; en: nothing). The rule is the dictionary's {name:subject}, which works on the plain name, so the span is added around the name only
const subjectSpan = (name, span) => span + t('board.subject', { name }).slice(String(name).length);
const atalkShown = () => ui.atalk.filter(e => ui.atalkFilter === 'all' || (ui.atalkFilter === 'peer') === (atalkSide(e) === 'peer'));
async function loadAtalk(older) {
  const g = nextGen('atalk');
  try {
    const before = older && ui.atalk.length ? '&before=' + ui.atalk[0].idx : '';
    const r = await api('/api/talk?scope=agents&limit=80' + before);
    if (!isLatest('atalk', g)) return;    // A newer request went out (so pressing the same "Load earlier" button twice does not append duplicates)
    ui.atalk = older ? r.items.concat(ui.atalk) : r.items;
    ui.atalkMore = r.more; ui.atalkLoaded = true; ui.atalkErr = false;
    renderAtalk({ keepOld: older });
  } catch { if (isLatest('atalk', g) && !ui.atalkLoaded) { ui.atalkErr = true; renderAtalk(); } }
}
function mergeAtalk() {          // Append only the newly arrived messages
  if (!ui.atalkLoaded) return false;
  const last = ui.atalk.length ? ui.atalk[ui.atalk.length - 1].idx : -1;
  const add = (S.feed || []).filter(e => ATALK_KINDS.includes(e.kind) && e.idx > last);
  if (add.length) ui.atalk = ui.atalk.concat(add);
  return add.length > 0;
}
function atalkHtml(items, o) {
  const who = id => {          // Name (◆ included) + model (left out when the name already is the model name)
    if (id === 'orch' || id === 'user') return whoBadge(id);
    if (!agentById(id)) return `<span class="who-b other" title="${esc(id)}">${/^uds:/.test(id) ? t('board.atalk.otherSession') : /^toolu_/.test(id) ? t('board.atalk.newAgent') : esc(agentName(id))}</span>`;   // A sender that is not an agent (a message from another Claude session, etc.)
    const a = agentById(id), ms = a ? AgentGame.modelShort(a.model) : '';
    return whoBadge(id) + guessMark(a) + (ms && !agentName(id).startsWith(ms) ? `<span class="am">${esc(ms)}</span>` : '');
  };
  let html = o.more ? `<button class="tab more-old" data-old="1">${t('board.msg.older')}</button>` : '', prevDay = '';
  items.forEach(e => {
    const day = dayHeading(e.ts);
    if (day !== prevDay) { html += `<div class="day">${day}</div>`; prevDay = day; }
    const [from, to] = atalkEnds(e), hl = esc([from, to].filter(id => id && id !== 'user').join(',')), tm2 = `<span class="mt-time" title="${esc(I18N.date(e.ts, 'dateTime'))}">${hm(e.ts)}</span>`;
    if (e.kind === 'xread') {    // Cross review: one dimmed line
      const rd = agentName(to);
      html += `<div class="xr" data-hl="${hl}">${t('board.atalk.xread', { reader: subjectSpan(rd, `<span class="xn" data-agent="${esc(to)}">${esc(rd)}</span>`), author: `<span class="xn" data-agent="${esc(from)}">${esc(agentName(from))}</span>`, title: esc(e.title) })}${tm2}</div>`;
      return;
    }
    const text = evText(e) || (e.kind === 'spawn' ? evTitle(e) : '');
    html += `<div class="msg ${atalkSide(e)}" data-idx="${e.idx}" data-hl="${hl}" title="${t('board.msg.openTitle')}"><div class="mh">${who(from)}<span class="faint">→</span>${who(to)}` +
      `<span class="tagk">${esc(atalkKind(e.kind))}</span>${tm2}</div>` +
      (encryptedEvent(e) ? `<div class="mt">${encryptedNote(e)}</div>` : o.full ? `<div class="mt full md">${md(text)}</div>` : text ? `<div class="mt">${esc(plain(text))}</div>` : '') + '</div>';
  });
  if (html && !items.length) html += `<div class="empty">${t('board.atalk.emptyFilter')}</div>`;
  return html || `<div class="empty">${ui.atalkErr ? t('board.atalk.error') : ui.atalkLoaded ? t('board.atalk.empty') : t('common.loading')}</div>`;
}
// Office highlight: hovering a conversation item highlights its sender and receiver (not during a demo)
const atalkHl = ids => { if (GAME && !GAME.demoOn) GAME.highlight(ids); };
function wireAtalk(root) {
  root.querySelectorAll('.msg').forEach(m => m.onclick = () => openAtalkMsg(+m.dataset.idx, m));
  wireEncryptedNotes(root, ui.atalk);
  root.querySelectorAll('[data-agent]').forEach(b => b.onclick = ev => { ev.stopPropagation(); if (ui.modalKind) closeModal(); openDrawer(b.dataset.agent); });
  const old = root.querySelector('[data-old]'); if (old) old.onclick = () => loadAtalk(true);
}
function renderAtalk(opt = {}) {
  const box = $('#atalkList');
  tabBar($('#atalkTabs'), ATALK_FILTERS.map(k => [k, t('board.atalk.tab.' + k)]), ui.atalkFilter, k => { ui.atalkFilter = k; store.set('atalkFilter', k); renderAtalk({ force: true }); });
  const shown = atalkShown(), html = atalkHtml(shown, { more: ui.atalkMore });
  if (html === ui.atalkHtml && !opt.force) return renderAtalkTime();
  const last = shown.length ? shown[shown.length - 1].idx : -1;
  const first = !ui.atalkHtml || opt.force, grew = !first && !opt.keepOld && last > ui.atalkLast;
  const oldH = box.scrollHeight, oldTop = box.scrollTop;
  box.innerHTML = html; ui.atalkHtml = html; ui.atalkLast = last;
  wireAtalk(box);
  atalkHl(null);                                       // The item under the mouse may have disappeared in the redraw (if it is still there, the next mouseover highlights it again)
  if (opt.keepOld) box.scrollTop = box.scrollHeight - oldH + oldTop;          // If items were prepended, stay where the reader was
  else if (first || ui.atalkStick) { ui.atalkStick = true; pinBottom(box, () => ui.atalkStick); $('#atalkNew').hidden = true; }   // Follow the bottom
  else { box.scrollTop = oldTop; if (grew) $('#atalkNew').hidden = false; }    // If reading further up, only show the notice
  renderAtalkTime();
}
function renderAtalkTime() {
  const e = [...ui.atalk].reverse().find(x => x.kind !== 'xread') || ui.atalk[ui.atalk.length - 1];   // The last "conversation" (read records left out)
  $('#atalkTime').textContent = e ? t('board.atalk.last', { time: hm(e.ts), ago: ago(e.ts) }) : '';
}
$('#atalkNew').onclick = () => { ui.atalkStick = true; pinBottom($('#atalkList'), () => ui.atalkStick); $('#atalkNew').hidden = true; };
watchFollow($('#atalkList'), $('#atalkNew'), v => { ui.atalkStick = v; });
$('#atalkList').addEventListener('mouseover', ev => { const m = ev.target.closest && ev.target.closest('[data-hl]'); atalkHl(m ? m.dataset.hl.split(',') : null); });
$('#atalkList').addEventListener('mouseleave', () => atalkHl(null));
function openAtalkMsg(idx, opener = document.activeElement) {
  let e = ui.atalk.find(x => x.idx === idx);
  if (!e || e.kind === 'xread') return;
  if (!e.text && e.kind === 'spawn') e = Object.assign({}, e, { text: evTitle(e) || '' });     // An assignment without a body (its instructions could not be read) shows at least its description
  const [from, to] = atalkEnds(e);
  return openEventModal(e, `${atalkName(from)} → ${atalkName(to)} · ${atalkKind(e.kind)}`, opener);
}
$('#atalkBig').onclick = () => {
  showModal(t('board.atalk.bigTitle'), `${t('board.msg.loaded', { count: ui.atalk.length })}${ui.atalkFilter === 'all' ? '' : ' · ' + t('board.atalk.only', { name: t('board.atalk.tab.' + ui.atalkFilter) })}${ui.atalkMore ? ' · ' + t('board.msg.moreHint', { label: t('board.msg.older') }) : ''} · ${t('board.msg.clickHint')}`, '<div class="talk atalk">' + atalkHtml(atalkShown(), { full: true }) + '</div>', $('#atalkBig'));
  wireAtalk($('#mBody'));
  $('#mBody').scrollTop = $('#mBody').scrollHeight;
};

// ---------- alerts ----------
const dismissed = dismissedIds();
const alertTitle = a => a.title_i18n && I18N.has(a.title_i18n.key) ? t(a.title_i18n.key, i18nParams(a.title_i18n.params)) : a.title;
// The one alert text the server writes is the Codex limit line; its dates arrive as epoch seconds (null = none) and are shown here
const alertText = a => {
  const ti = a.text_i18n;
  if (!ti || !I18N.has(ti.key)) return a.text;
  return t(ti.key, i18nParams(ti.params));
};
function activeAlerts() { return (ui.demoAlert ? [ui.demoAlert] : []).concat((S.alerts || []).filter(a => !dismissed.has(a.id))); }
function renderAlerts() {
  const box = $('#alerts'), list = activeAlerts(), hid = (S.alerts || []).filter(a => dismissed.has(a.id)).length;
  document.title = (list.length ? `(${list.length}) ` : '') + t('page.title.dashboard');
  if (!list.length) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  box.className = 'card alerts lv-' + list[0].level;
  const IC = { decide: '!', check: '?', info: 'i', note: 'i' };
  // A new alert opens the box by itself. Once folded, it is reopened with the "alerts" chip in the header.
  const ids = list.map(a => a.id).join('|');
  if (ids !== ui.alertIds) { if (list.some(a => !(ui.alertIds || '').split('|').includes(a.id))) ui.alertsOpen = true; ui.alertIds = ids; }
  if (!ui.alertsOpen) { box.hidden = true; return; }
  box.innerHTML = `<div class="label">${t('board.alert.head')} <span class="faint" style="text-transform:none">${t('board.alert.count', { count: list.length })}${hid ? ` · ${t('board.alert.hidden', { count: hid })}` : ''} · ${t('board.alert.answerIn')}</span><button class="icon-btn" id="alertsFold" title="${t('board.alert.foldTitle')}">${t('common.collapse')}</button></div>` +
    list.map(a => `<div class="al ${a.level} ${ui.openAlerts.has(a.id) ? 'open' : ''}" data-id="${esc(a.id)}">
      <span class="ic">${IC[a.level] || 'i'}</span>
      <div><div class="t">${t('board.alert.level.' + a.level)} · ${a.agent ? cxMark(agentById(a.agent)) : ''}${esc(nameIds(alertTitle(a)))}${/^(say|turn):/.test(a.id) ? ` <span class="faint" style="font-weight:400">${t('board.alert.guess')}</span>` : ''}</div>
        <div class="x" title="${t('board.alert.expandTitle')}">${esc(nameIds(alertText(a)))}</div>
        <div class="m">${hm(a.ts)} · ${ago(a.ts)}${a.agent ? ` · <a href="#" data-agent="${esc(a.agent)}">${t('board.alert.viewAgent')}</a>` : ''}</div></div>
      <button class="icon-btn" data-dismiss="${esc(a.id)}" title="${t('board.alert.dismissTitle')}">${t('board.alert.dismiss')}</button></div>`).join('');
  $('#alertsFold').onclick = () => { ui.alertsOpen = false; renderAlerts(); };
  box.querySelectorAll('.x').forEach(x => x.onclick = () => { const id = x.closest('.al').dataset.id; ui.openAlerts.has(id) ? ui.openAlerts.delete(id) : ui.openAlerts.add(id); renderAlerts(); });
  box.querySelectorAll('[data-dismiss]').forEach(b => b.onclick = () => { dismissed.add(b.dataset.dismiss); store.set('dismissed', [...dismissed].slice(-300)); renderAlerts(); renderTop(); });
  box.querySelectorAll('[data-agent]').forEach(l => l.onclick = ev => { ev.preventDefault(); openDrawer(l.dataset.agent); });
}

// ---------- diagnostics ----------
// What the server noticed but could not settle for this session (S.diag = counts, /api/diag = the list). The header chip shows the count (none: no chip); the list opens in the modal: what it is
// (one sentence per code, from the dictionary), who it is about and how serious. The server never puts transcript text in it; a code this page has no sentence for shows as the code.
function diagChipHtml() {
  const d = S && S.diag;
  if (!d || !d.n) return '';
  return `<button class="chip" id="diagChip" style="cursor:pointer${d.warn ? ';color:var(--amber)' : ''}" title="${esc(t('board.top.diag.title'))}">${t('board.top.diag', { n: `<b>${d.n}</b>` })}</button>`;
}
const diagText = c => I18N.has('board.diag.code.' + c) ? t('board.diag.code.' + c) : c;
const diagSubject = x => x.agent ? `<span class="who-b agent" data-agent="${esc(x.agent)}">${cxMark(agentById(x.agent))}${esc(agentName(x.agent))}</span>`
  : x.unit ? `<span class="mono">${esc(x.unit)}</span>` : x.scope === 'orch' ? `<span class="who-b orch">${t('common.orchestrator')}</span>` : `<span class="faint">${t('board.diag.subject.session')}</span>`;
// The small values the server attached (counts, flags): kept for the tooltip of a row, not worded
const diagParams = p => Object.entries(p || {}).filter(([, v]) => v !== null && v !== '' && !(Array.isArray(v) && !v.length)).map(([k, v]) => k + ' ' + (Array.isArray(v) ? v.join(',') : v)).join(' · ');
async function openDiag(opener = document.activeElement) {
  const g = beginModal('diag', t('board.diag.title'), t('common.loading'), '', opener);
  try {
    const D = await api('/api/diag'), items = D.items || [];
    if (!isLatest('modal', g)) return;   // Meanwhile something else was opened, or this was closed
    $('#mSub').textContent = t('board.diag.sub', { count: D.n, warn: D.warn }) + (D.capped ? ' · ' + t('board.diag.capped', { count: items.length }) : '');
    $('#mBody').innerHTML = `<p class="faint">${t('board.diag.intro')}</p>` + (items.length ? items.map(x => `<div class="dg-item ${esc(x.level)}" title="${esc(diagParams(x.params))}"><span class="dg-lv">${esc(I18N.has('board.diag.level.' + x.level) ? t('board.diag.level.' + x.level) : x.level)}</span><div><div>${esc(diagText(x.code))}</div><div class="dg-sj">${diagSubject(x)}</div></div></div>`).join('')
      : `<div class="empty">${t('board.diag.empty')}</div>`);
    $('#mBody').querySelectorAll('[data-agent]').forEach(b => b.onclick = () => { closeModal(); openDrawer(b.dataset.agent); });
    $('#mBody').scrollTop = 0;
  } catch { if (isLatest('modal', g)) $('#mSub').textContent = t('board.diag.error'); }
}

// ---------- office (game) view ----------
let GAME = null;
function renderGame() {
  $('#gameToggle').textContent = ui.gameOpen ? t('common.collapse') : t('common.expand');
  $('#gameBox').hidden = !ui.gameOpen;
  $('#officeRow').classList.toggle('folded', !ui.gameOpen);
  $('#gameLegend').hidden = !ui.gameOpen;
  $('#gameFolded').hidden = ui.gameOpen;      // a folded office says so in one line, which opens it
  if (window.AgentGame && S) {   // Legend: the same text as under /game (the large view)
    const lh = AgentGame.legendHtml(hasCodex(S) || !!(GAME && GAME.demoOn));
    if ($('#gameLegend').innerHTML !== lh) $('#gameLegend').innerHTML = lh;
  }
  $('#gameFull').href = I18N.link('game' + (SESSION ? '?session=' + encodeURIComponent(SESSION) : ''));   // a ?lang= in this address carries over to the office page
  if (S) { const h = t(hasCodex(S) ? 'page.game.hint.codex' : 'page.game.hint');
    if ($('#gameHint').textContent !== h) $('#gameHint').textContent = h; }
  if (!ui.gameOpen || !window.AgentGame || !S) return;
  if (!GAME) GAME = AgentGame.mount($('#gameBox'), { mode: 'strip', onAgent: id => openDrawer(id) });
  GAME.update(S, currentDebate());
}
const toggleGame = () => { ui.gameOpen = !ui.gameOpen; store.set('gameOpen', ui.gameOpen); renderGame(); };
$('#gameToggle').onclick = toggleGame;
$('#gameFolded').onclick = toggleGame;
// Demo: plays a scenario in the game strip. The "trouble" scenario also puts fake alerts in the alert box (to check that it appears without jitter)
function startDemo(key) {
  if (!ui.gameOpen) { ui.gameOpen = true; store.set('gameOpen', true); renderGame(); }
  if (!GAME) return;
  const alertHook = a => { ui.demoAlert = a; renderAlerts(); renderTop(); };
  GAME.demo(key || 'basic', { onAlert: alertHook, onEnd: () => alertHook(null) });
}
$('#gameDemo').onclick = ev => { ev.stopPropagation(); if (GAME && GAME.demoOn) return GAME.stopDemo(); AgentGame.demoMenu($('#gameDemo'), startDemo); };

// ---------- refresh ----------
function renderAll() {
  if (!S) return;
  keepScroll(() => {
    renderTop(); renderAlerts(); renderOrch(); renderSummary(); renderDebates(); renderAgents(); renderFeed(); renderGame();
    if (mergeTalk()) renderTalk();
    mergeAtalk(); renderAtalk();
    if (ui.drawer) renderDrawer();
  });
}
// So that what the reader is looking at does not shift on a redraw: measure the position of the section that straddles the top of the screen, and scroll back by the difference
function keepScroll(fn) {
  // Remember the positions of the inner scroll boxes and restore them after the redraw
  const inner = [...document.querySelectorAll('.now > .card, .scroll:not(#talkList), #feedItems, #agentList, #alerts')].map(el => [el, el.scrollTop]);
  const run = () => { fn(); inner.forEach(([el, top]) => { if (top && el.isConnected) el.scrollTop = top; }); };
  if (scrollY < 4) return run();
  const head = $('.top').getBoundingClientRect().bottom;
  const secs = [...document.querySelectorAll('.wrap > section, .office-row > section, .main > section > *, .main > aside, .lower > section, #topics > .topic')];
  const anchor = secs.find(el => el.getBoundingClientRect().bottom > head + 8);
  const t0 = anchor ? anchor.getBoundingClientRect().top : 0;
  run();
  if (anchor && anchor.isConnected) { const d = anchor.getBoundingClientRect().top - t0; if (Math.abs(d) >= 1) scrollBy(0, d); }
}
let lastOk = 0, ticking = false;
// First-screen diagnosis (common.js): when there is no session to open or the server cannot be reached, it shows the reason in one box, rechecks every 5 s and opens through tick once a session appears
const diag = makeDiag($('#diag'), { session: () => SESSION, retry: () => tick(),
  status: (kind, text, note) => { $('#updated').innerHTML = `<span class="dot${kind === 'empty' ? '' : ' dead'}"></span>${text} · ${note}`; } });
// After a server restart the event numbers (idx) may differ: everything that depends on a number goes back to the new baseline.
// Raise the generation so that a late response to a conversation or event request sent with an old number is not applied (the new conversation requests go out right after),
// and close a conversation modal that was opened by number (the response of a text being requested is dropped too). A document modal is opened by path, so it stays
function onReboot() {
  nextGen('talk'); nextGen('atalk');
  if (ui.modalKind === 'talk') { closeModal(); $('#mTitle').textContent = ''; $('#mSub').textContent = ''; $('#mBody').innerHTML = ''; }   // Also empty the old text that is hidden
  ui.talk = []; ui.talkSig = ''; ui.talkLoaded = false; ui.openEvents.clear();
  ui.atalk = []; ui.atalkHtml = ''; ui.atalkLoaded = false; ui.atalkMore = false; ui.atalkLast = -1;
  setTimeout(() => { loadTalk(false); loadAtalk(false); }, 0);
}
// Clicking a character in /game lands on ./?session=…#agent=<id> (id: Claude a + 16 hex digits, Codex UUID). Only an id that is in the agent list now is opened
function hashAgent() {
  const m = location.hash.match(/^#agent=(.+)$/);
  if (!m) return null;
  try { return decodeURIComponent(m[1]); } catch { return null; }
}
async function tick() {
  if (ticking || diag.on) return;    // The next request goes out only after the previous one ends (so responses arriving out of order cannot take the screen back to an old state). While the diagnosis box is up, its 5 s recheck starts tick again
  ticking = true;
  try {
    // At first, check whether there is a session to open: if there is none, there are no records rather than a broken server, so the diagnosis box is shown and /api/state (404) is not called
    if (!S) { const sess = await diag.probe(); if (!sess) return; SESS = sess; if (!SESSION) SESSION = sess.default || ''; }   // Knowing the list first makes the solo marks right from the first draw
    const st = await apiGuarded(`/api/state?v=${S ? S.version : ''}&t=${S ? S.now : ''}`);   // A request with no answer is cut so it never blocks polling for ever
    lastOk = Date.now();
    if (!st.unchanged) {
      serverSkew = st.now - Date.now() / 1000;
      // After a server restart the event numbers may differ: the conversations are loaded anew and the expanded marks in the feed are cleared
      const rebooted = S && st.boot && S.boot && st.boot !== S.boot;
      if (rebooted) onReboot();
      const first = !S;
      S = st; if (!SESSION) SESSION = st.session.id;
      renderAll();
      if (first) { loadTimeline(); loadTalk(false); loadAtalk(false); const hid = hashAgent(); if (hid && agentById(hid)) openDrawer(hid); const dk = qs.get('demo'); if (dk) setTimeout(() => startDemo(dk === '1' ? 'basic' : dk), 800); }
    } else if (S) {
      S.now = st.now || S.now;
      keepScroll(() => { renderTop(); renderAgents(); renderDebates(); renderOrch(); });
    }
    $('#updated').innerHTML = `<span class="dot alive"></span>${t('common.live')} · ${I18N.date(new Date(), 'timeSec')}`;
  } catch (e) {
    if (!S) await diag.fail(e);          // A failure before the first state: "session not found", "server refused" or "connection failed" goes to the diagnosis box
    // 401 on a tab that was open before: the server restarted and the access token is new. Keep the board, say what to do (the full sentence is the tooltip), and carry on: it goes live again once this browser holds the new token
    else if (e && e.status === 401) $('#updated').innerHTML = `<span class="dot dead"></span><span title="${esc(t('diag.http.token401'))}">${t('board.live.token401')}</span>`;
    else $('#updated').innerHTML = `<span class="dot dead"></span>${t('board.live.down', { sec: Math.round((Date.now() - lastOk) / 1000) })}`;
  } finally { ticking = false; }
}
// Started at the bottom of this file once the dictionary is here (I18N.ready): t() must be able to answer before the first draw
// The agent column sticks below the header, which is two lines when many chips are shown: the CSS reads the real height (--toph, see board.css)
function watchHeader() {
  const top = $('.top');
  if (!top || typeof ResizeObserver !== 'function') return;
  const set = () => document.documentElement.style.setProperty('--toph', Math.round(top.getBoundingClientRect().height) + 'px');
  new ResizeObserver(set).observe(top);
  set();
}
function startMain() {
  watchHeader();
  tick();
  setInterval(tick, 3000);
  setInterval(() => loadTimeline(), 15000);
  setInterval(() => refreshDrawer(), 5000);
  setInterval(() => { if (S && !ui.atalkLoaded) loadAtalk(false); }, 15000);      // Try again if the agent talk could not be loaded
  addEventListener('resize', () => { if (S) tlControls(); renderTimeline(); });
}
// Bottom row: the same order for both services (plan → 5 hours → week → extra usage and credits → recorded time)
const PLAN_NAMES = { prolite: 'Pro Lite', pro: 'Pro', plus: 'Plus', team: 'Team', enterprise: 'Enterprise', free: 'Free', business: 'Business' };
const pbPct = v => `<span class="${v >= 90 ? 'bad' : v >= 70 ? 'warn' : ''}"><b>${Math.round(v)}%</b></span>`;
const pbNext = (r, days) => { let tt = r; const step = days * 86400; if (tt <= now()) tt += Math.ceil((now() - tt) / step) * step; return tt; };
function renderPlanBar(P) {
  const bar = $('#planBar'); if (!bar || !P) return;
  const segs = [];
  // Without ~/.claude.json the server gives claude as null. The "no cache" notice is shown only to someone who has Claude sessions (someone using only Codex has no such box at all)
  const claudeUsed = !!SESS && (SESS.sources || []).some(x => x.provider === 'claude' && x.sessions > 0);
  const c = P.claude || (claudeUsed ? {} : null);
  if (c) {
    const items = [];
    const hit = k => { const h = (c.hits || {})[k]; return h && h.status === 'rejected' && h.resets_at > now() ? h : null; };
    const win = (label, w, k, days) => {
      const h = hit(k);
      if (h) return `<span class="bad">${t('board.planbar.hit', { label, time: hm(h.resets_at) })}</span>`;
      if (w && w.resets_at > now()) return `<span>${t('board.planbar.reset', { label, pct: pbPct(w.percent), time: hm(w.resets_at) })}</span>`;
      return `<span title="${t(c.source === 'statusline' ? 'board.planbar.statuslineStale' : 'board.planbar.staleTitle', { time: hm(c.as_of) })}">${label} <b>—</b>${days && w ? ` · ${t('board.planbar.estimate', { time: hm(pbNext(w.resets_at, days)) })}` : ''}</span>`;
    };
    if (!c.as_of && !c.five_hour && !c.seven_day && !Object.keys(c.hits || {}).length) {
      items.push(`<span class="faint" title="${t('board.planbar.noCache.title')}">${t('board.planbar.noCache')}</span>`);   // There is no cache yet
    } else {
      items.push(win(t('board.planbar.five'), c.five_hour, 'five_hour', 0), win(t('board.planbar.week'), c.seven_day, 'seven_day', 7));
      // The status line carries only the 5 hour and weekly windows: its label closes them. The model weeks and extra usage always come from the .claude.json cache, which can be days older,
      // so they follow with a time of their own, and are left out when the server did not say when that cache was written (never drawn under the status line's time)
      const sl = c.source === 'statusline', cacheAt = sl ? c.cache_as_of : c.as_of;
      if (sl) items.push(`<span class="faint" title="${t('board.planbar.statuslineTitle')}">${t('board.planbar.statusline', { time: hm(c.as_of) })}</span>`);
      const cached = [];
      (c.scoped || []).filter(x => x.resets_at > now()).forEach(x => cached.push(`<span>${t('board.planbar.scoped', { name: esc(x.name), pct: pbPct(x.percent) })}</span>`));
      const ex = c.extra;
      if (ex) cached.push(ex.enabled ? `<span>${t('board.planbar.extraOn', { used: `<b>$${((ex.used || 0) / 100).toFixed(2)}</b>`, limit: `$${((ex.limit || 0) / 100).toFixed(0)}` })}</span>`
        : `<span>${t(ex.reason === 'out_of_credits' ? 'board.planbar.extraOffNoCredits' : 'board.planbar.extraOff')}</span>`);
      if (!sl || cacheAt) items.push(...cached);
      if (!sl && c.as_of) items.push(`<span class="faint" title="${t('board.planbar.cacheTitle')}">${t('board.planbar.recordedRefresh', { time: hm(c.as_of) })}</span>`);
      else if (sl && cacheAt && cached.length) items.push(`<span class="faint" title="${t('board.planbar.cacheTitle')}">${t('board.planbar.recorded', { time: hm(cacheAt) })}</span>`);
    }
    segs.push(`<span class="pb-seg"><span class="pb-name">Claude Code</span>${c.plan ? `<span class="pb-plan">${esc(c.plan)}</span>` : ''}${items.join('')}</span>`);
  }
  const x = P.codex;
  if (x) {
    const items = [];
    const sec = x.secondary;
    items.push(sec && !sec.stale ? `<span>${t('board.planbar.reset', { label: t('board.planbar.five'), pct: pbPct(sec.used_percent), time: hm(sec.resets_at) })}</span>`
      : `<span title="${t('board.planbar.noFive')}">${t('board.planbar.five')} <b>—</b></span>`);
    // Check freshness (the reset time) first: once the reset has passed, "limit reached" is a past value too (same rule as the token card)
    const fresh = !x.stale && !(x.resets_at && tsOf(x.resets_at) <= now());
    items.push(!fresh ? `<span title="${t('board.planbar.cxStaleTitle')}">${t('board.planbar.week')} <b>—</b> · ${t('board.planbar.estimate', { time: hm(pbNext(x.resets_at, (x.window_minutes || 10080) / 1440)) })}</span>`
      : x.reached ? `<span class="bad">${t('board.planbar.hit', { label: t('board.planbar.week'), time: hm(x.resets_at) })}</span>`
      : `<span>${t('board.planbar.reset', { label: t('board.planbar.week'), pct: pbPct(x.used_percent), time: hm(x.resets_at) })}</span>`);
    if (x.credits && x.credits.has_credits) items.push(`<span>${t('board.planbar.credits', { balance: `<b>${esc(x.credits.balance)}</b>` })}</span>`);
    if (x.as_of) items.push(`<span class="faint" title="${t('board.planbar.cxRecordTitle')}">${t('board.planbar.recorded', { time: hm(x.as_of) })}</span>`);
    segs.push(`<span class="pb-seg"><span class="pb-name">${CXG} Codex</span>${x.plan_type ? `<span class="pb-plan">${esc(PLAN_NAMES[x.plan_type] || x.plan_type)}</span>` : ''}${items.join('')}</span>`);
  }
  bar.innerHTML = segs.join('<span class="pb-sep"></span>') || `<span class="faint">${t('board.planbar.none')}</span>`;
  document.documentElement.style.setProperty('--barh', bar.offsetHeight + 'px');
}
let PLANS = null;
async function loadPlans() { try { PLANS = await fetch('api/plans').then(r => r.json()); renderPlanBar(PLANS); } catch {} }
function startPlans() {
  loadPlans();
  setInterval(() => { if (!document.hidden) loadPlans(); }, 30000);   // the status line file and the .claude.json cache change while Claude Code runs
  setInterval(() => renderPlanBar(PLANS), 30000);            // The display changes once a reset time has passed
  addEventListener('resize', () => renderPlanBar(PLANS));
}
I18N.ready.then(() => { I18N.mountPicker($('#langSel')); startMain(); startPlans(); });
