/* Small helpers shared by the dashboard (index.html → board.js) and the full-screen office (game.html).
 * Holds only what the two pages used to build on their own and let drift apart (the amount text, the Codex test, the cumulative cost, the list of dismissed alerts)
 * and the first-screen diagnostics both show when the server has no data (httpError, fetchSessions, makeDiag, DIAG_CSS).
 * game.js (the office drawing) does not depend on this file. Load it before game.js, board.js and the script in game.html. */
'use strict';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Amount text: $1.50; from 100 dollars up without decimals, $150
const usd = v => '$' + (v || 0).toLocaleString('en-US', { minimumFractionDigits: v >= 100 ? 0 : 2, maximumFractionDigits: v >= 100 ? 0 : 2 });
// Is there Codex in the state (/api/state): when the orchestrator is Codex or at least one agent is Codex. false for a session with only Claude
const hasCodex = st => !!st && ((!!st.orch && st.orch.provider === 'codex') || st.agents.some(a => a.provider === 'codex'));
// Cumulative cost of the whole session (orchestrator + every agent, ended ones included) and the Codex share of it
const sumCost = list => list.reduce((sum, t) => sum + ((t && t.cost) || 0), 0);
const sessionCost = st => sumCost([st.orch.tokens, ...st.agents.map(a => a.tokens)]);
const codexCost = st => sumCost([st.orch && st.orch.provider === 'codex' ? st.orch.tokens : null, ...st.agents.filter(a => a.provider === 'codex').map(a => a.tokens)]);
// Ids of the alerts hidden as "dismissed" in this browser (localStorage ab.dismissed)
const dismissedIds = () => { try { return new Set(JSON.parse(localStorage.getItem('ab.dismissed') || '[]')); } catch { return new Set(); } };

// ---------- first-screen diagnosis ----------
// When there is no session to open (the transcript folder is missing or empty) and when the server cannot be reached, the reason is shown in one box. Shared by the dashboard and the office page.
// A one-line note per folder is built from the sources of /api/sessions (per provider: the folder read, where it came from, the session count); it is fetched again every 5 s and a session opens automatically once one appears.
// The box's style is added here, once, so that both pages look the same (game.html does not load board.css).
// The error to throw when a response has an error status: it carries the status and the text the server gave (the JSON error, else the body as it is: a 403 is text/plain). The screen tells "server refused" from "connection failed"
async function httpError(r, what) {
  let detail = '';
  try { const t = await r.text(); try { detail = (JSON.parse(t) || {}).error || ''; } catch { detail = t.trim(); } } catch {}
  return Object.assign(new Error(r.status + ' ' + what), { status: r.status, detail: String(detail).slice(0, 400) });
}
// Session list: the first screen and the 5 s requery wait for this request, so it has a time limit (so a server that connects but never answers cannot stall the screen for ever).
// On timeout it throws an error marked timeout (it has no status, so the caller draws it as "connection failed" and tries again 5 s later)
const SESSIONS_TIMEOUT_MS = 10000;
async function fetchSessions(ms = SESSIONS_TIMEOUT_MS) {
  const ctl = new AbortController(), guard = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch('api/sessions', { signal: ctl.signal });
    if (!r.ok) throw await httpError(r, 'api/sessions');
    return await r.json();                       // Reading the body is inside the same limit
  } catch (e) {
    if (e && e.name === 'AbortError') throw Object.assign(new Error('timeout api/sessions'), { timeout: true });
    throw e;
  } finally { clearTimeout(guard); }
}
const DIAG_NAME = { claude: 'Claude Code', codex: '◆ Codex' };
const DIAG_FLAG = { claude: '--claude-config-dir', codex: '--codex-home' };
const DIAG_ENV = { claude: 'CLAUDE_CONFIG_DIR', codex: 'CODEX_HOME' };
const DIAG_CSS = `
.diag { margin: 12px 0; padding: 14px 18px; max-width: 880px; line-height: 1.6; border: 1px solid var(--line, #2c2a48); border-radius: 12px; background: var(--panel, rgba(127,127,127,.08)); }
.diag.down { border-color: var(--red, #f85149); }
.diag h2 { margin: 0 0 4px; font-size: 1.15em; }
.diag p { margin: 4px 0; color: var(--muted, #9a98b5); }
.diag p.dg-detail { white-space: pre-line; }
.diag code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .92em; padding: 0 4px; border: 1px solid var(--line, #2c2a48); border-radius: 4px; overflow-wrap: anywhere; color: var(--text); }
.diag .dg-src { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--line, #2c2a48); }
.diag .dg-row { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; }
.diag .dg-name { font-weight: 700; }
.diag .dg-from { color: var(--muted, #9a98b5); font-size: .92em; }
.diag .dg-n { margin-left: auto; font-variant-numeric: tabular-nums; }
.diag .dg-n.warn { color: var(--amber, #e3b341); }
.diag .dg-hint { margin: 2px 0 0; }
.diag .dg-foot { margin-top: 12px; font-size: .9em; }
body.nodata .wrap > :not(.diag), body.nodata main > :not(.diag), body.nodata #sessionSel, body.nodata #sessionLive, body.nodata #counters,
body.nodata #demoBtn, body.nodata #cost, body.nodata #sess { display: none; }
`;
const DIAG_SUB = { claude: 'projects', codex: 'sessions' };     // Name of the transcript folder under the config folder
const dgCode = text => `<code>${esc(text)}</code>`;       // a code span for the dictionary texts that take markup as a value (t() does not escape)
function diagSource(x) {
  const name = DIAG_NAME[x.provider] || esc(x.provider), n = x.sessions || 0, cx = x.provider === 'codex', sub = DIAG_SUB[x.provider] || '';
  const dir = String(x.dir || '').replace(/\/$/, ''), shown = !sub || dir.endsWith('/' + sub) ? dir : dir + '/' + sub;   // Shows the transcript folder that is actually read
  const from = x.from === 'flag' ? t('diag.from.flag', { flag: dgCode(DIAG_FLAG[x.provider] || '') }) : x.from === 'env' ? t('diag.from.env', { var: dgCode(DIAG_ENV[x.provider] || '') }) : t('diag.from.default');
  const cnt = cx ? 'diag.count.recent' : 'diag.count.sessions';      // Codex lists only the last 7 days
  const [warn, count, hint] = !x.exists ? [true, t('diag.count.nofolder'), t('diag.hint.nofolder')]
    : n === 0 ? [true, t(cnt, { count: 0 }), t(cx ? 'diag.hint.empty.recent' : 'diag.hint.empty')]
    : [false, x.agent_sessions != null ? t(cnt + '.agents', { count: n, agents: x.agent_sessions }) : t(cnt, { count: n }), ''];
  return `<div class="dg-src"><div class="dg-row"><span class="dg-name">${name}</span><code>${esc(shown)}</code><span class="dg-from">${from}</span><span class="dg-n${warn ? ' warn' : ''}">${count}</span></div>` +
    (hint ? `<p class="dg-hint">${hint}</p>` : '') + '</div>';
}
// The 403 body holds the same sentence once per language in this order (server.host_hint): the screen shows the line of its own language, the first (English) line for any other
const BODY_LANGS = ['en', 'ko'];
function bodyLine(text) {
  const lines = String(text || '').split('\n').map(x => x.trim()).filter(Boolean);
  return lines.length > 1 ? lines[Math.max(0, BODY_LANGS.indexOf(I18N.lang))] || lines[0] : lines[0] || '';
}
function diagHtml(kind, d) {
  const sources = d.sess && d.sess.sources || [];
  const list = `<p>${t('diag.intro')}</p>` + (sources.length ? sources.map(diagSource).join('') : `<p class="dg-hint">${t('diag.nosources')}</p>`) +
    '<p class="dg-foot">' + t('diag.foot.dirs', { claudeFlag: dgCode('--claude-config-dir <' + t('diag.dir.claude') + '>'), codexFlag: dgCode('--codex-home <' + t('diag.dir.codex') + '>'),
      claudeEnv: dgCode('CLAUDE_CONFIG_DIR'), codexEnv: dgCode('CODEX_HOME') }) + '</p>';
  const head = {
    empty: () => `<h2>${t('diag.empty.title')}</h2>` + list,
    missing: () => `<h2>${t('diag.missing.title')}</h2><p>${t(d.id ? 'diag.missing.body.id' : 'diag.missing.body', { id: dgCode(d.id) })} ${d.sess && d.sess.default ? `<a href="${I18N.link('?')}">${t('diag.missing.openDefault')}</a>` : ''}</p>` + list,
    down: () => (d.timeout ? `<h2>${t('diag.timeout.title')}</h2><p>${t('diag.timeout.body', { sec: SESSIONS_TIMEOUT_MS / 1000 })}</p><p>` : `<h2>${t('diag.down.title')}</h2><p>`) +
      t(location.host ? 'diag.down.body.host' : 'diag.down.body', { host: dgCode(location.host), cmd: dgCode('python3 server.py') }) + '</p>',
    http: () => `<h2>${t('diag.http.title', { status: esc(d.status) })}</h2>` + (d.detail ? `<p class="dg-detail">${esc(d.status === 403 ? bodyLine(d.detail) : d.detail)}</p>`    // the server's text says how to fix it (403: --allow-host)
      : d.status === 403 ? `<p>${t('diag.http.host403', { flag: dgCode('--allow-host <' + t('diag.dir.host') + '>') })}</p>` : ''),
  }[kind];
  return head() + `<p class="dg-foot">${t('diag.foot.recheck', { time: '<span class="dg-time"></span>' })}</p>`;
}
// box = the box element, session() = the session id being opened now (decided by the address), retry() = that page's tick when a session to open appears, status(kind, text) = updates the header display
function makeDiag(box, { session, retry, status }) {
  let timer = 0, cur = null, html = '', styled = false;
  function draw(kind, d) {
    if (!styled) { styled = true; const st = document.createElement('style'); st.textContent = DIAG_CSS; document.head.appendChild(st); }
    cur = kind;
    const h = diagHtml(kind, d);
    if (h !== html || !box.querySelector('.dg-time')) { html = h; box.innerHTML = h; }       // Not rewritten when unchanged (so text the user has selected by dragging stays selected)
    const tm = box.querySelector('.dg-time'); if (tm) tm.textContent = I18N.date(new Date(), 'timeSec');
    box.hidden = false; box.classList.toggle('down', kind === 'down' || kind === 'http');
    document.body.classList.add('nodata');
    clearTimeout(timer); timer = setTimeout(poll, 5000);
    if (status) status(kind, t('diag.label.' + kind), t('diag.recheck.short'));      // the third text is the "every 5 s" note for headers that have room
  }
  function hide() {
    clearTimeout(timer); timer = 0;
    if (!cur) return;
    cur = null; html = ''; box.hidden = true; box.innerHTML = '';
    document.body.classList.remove('nodata');
  }
  // Returns the /api/sessions response when there is a session to open; otherwise draws the reason and returns null
  async function probe() {
    let sess;
    try { sess = await fetchSessions(); }
    catch (e) { draw(e.status ? 'http' : 'down', { status: e.status, detail: e.detail, timeout: e.timeout }); return null; }
    const want = session(), missing = cur === 'missing';
    // While the box is up because the session could not be opened, it is retried only once that session is in the list (so a session that does not exist is not requested every 5 s)
    if (missing ? !(sess.sessions || []).some(s => s.id === want) : !(want || sess.default)) { draw(missing ? 'missing' : 'empty', { sess, id: want }); return null; }
    hide();
    return sess;
  }
  async function poll() { timer = 0; if (await probe()) retry(); }
  // When the first /api/state fails: 404 = that session was not found, any other status = the server refused or erred, no status (a fetch exception) = connection failed
  async function fail(e) {
    if (e && e.status === 404) { let sess = null; try { sess = await fetchSessions(); } catch {} draw('missing', { sess, id: session() }); }
    else draw(e && e.status ? 'http' : 'down', { status: e && e.status, detail: e && e.detail, timeout: e && e.timeout });
  }
  return { probe, fail, hide, get on() { return !!cur; } };
}
