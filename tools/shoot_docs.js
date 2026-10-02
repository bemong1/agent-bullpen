// Screenshots for docs/images/<lang>/ with playwright. Needs two running boards (tools/shoot_docs.py starts them on a synthetic HOME).
// usage: NODE_PATH=<pw>/node_modules PLAYWRIGHT_BROWSERS_PATH=<pw>/browsers node tools/shoot_docs.js <board url> <empty board url> <out dir> [en|ko]
//   The screen language is the last argument (default en): it goes into every address as ?lang= and into the browser's locale, so the board shows that language whatever the machine's settings are.
//   dashboard.png  1600 wide: top cards + office + agent talk card (the office opened, as a user would open it)
//   office.png     /game, 1600 wide
//   agents.png     the agent list cut down to a launched tree and two runs that stopped (the new states: the first two of the list, a usage limit and the background time limit)
//   debates.png    the debate table (topics x rounds x participants), cut to a few topics (SHOOT_TOPICS) so the picture stays small
//   empty.png      first screen of a board that has no records
// All dark: the colour scheme is dark and the saved theme is "dark" (board.js keeps it as JSON in localStorage 'ab.theme').
// Every request must stay on the two boards (fonts are served by them): anything else aborts the run. A fixed-offset time zone (zoneNow), so no local time zone shows.
// SHOOT_FOLD_ALERTS=1 folds the alert card before the dashboard picture; SHOOT_TOPICS keeps the topics of debates.png: a number n = the first n that have participants (default 5), or a list
// of topic numbers (T1,T3,T5) = those cards.
// SHOOT_LOUNGE=pick moves the clock of the office (game.js only; the rest of the page keeps the real one) to a moment when the people who rest in the lounge sit at the tea table (two or more) and
// do at least two other things, two of them at the same table (the picks are random per person and change every 150 s: see assignLounge in static/game.js). Every page that shows the office is opened at such a moment and
// checked as it stands; the run stops with an error when none works. Without it the lounge shows whatever the real clock gives.
const { chromium } = require('playwright');
const path = require('path');
const [base, emptyBase, out, lang = 'en'] = process.argv.slice(2);
if (!['en', 'ko'].includes(lang)) { console.error('language must be en or ko'); process.exit(2); }
const sleep = ms => new Promise(r => setTimeout(r, ms));
// The browser's time zone: a fixed offset chosen so that it is 14:00 there now. The scene reaches five hours back and the board writes a date next to the time of any other day ("Oct 1 20:41"), so the
// pictures must not be taken just after a midnight; and no real time zone shows.
const zoneNow = (() => { let off = (14 - new Date().getUTCHours()) % 24; if (off > 12) off -= 24; if (off < -12) off += 24; return off ? 'Etc/GMT' + (off > 0 ? '-' : '+') + Math.abs(off) : 'UTC'; })();

// ---- the lounge moment (SHOOT_LOUNGE=pick) ----
// Runs before any script of the page: Date.now() answers `off` ms later for callers in game.js only, and the handle that AgentGame.mount returns is kept in window.__games (its `_g` is the
// office state: who sits where).
const clockHook = off => {
  const real = Date.now.bind(Date);
  Date.now = () => (off && new Error().stack.includes('/game.js') ? real() + off : real());
  let lib;
  Object.defineProperty(window, 'AgentGame', { configurable: true, get: () => lib, set: v => {
    lib = v; const mount = v.mount;
    v.mount = (...a) => { const h = mount(...a); (window.__games = window.__games || []).push(h); return h; };
  } });
};
// Port of the pick in assignLounge (static/game.js): which facility a resting person with status done draws for the slot that holds nowS. Only used to find candidate moments; every one is checked in the page.
const hash32 = s => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } h ^= h >>> 15; h = Math.imul(h, 2246822519); h ^= h >>> 13; h = Math.imul(h, 3266489917); h ^= h >>> 16; return h >>> 0; };
const rnd = s => (hash32(s) + 0.5) / 4294967296;
const WEIGHT = { sofa: 35, tea: 20, game: 15, tread: 8, dumb: 7, coffee: 10 };
const pickAt = (a, nowS) => {
  const sec = 150, ph = hash32(a.id + '#ph') % sec, k = Math.floor((nowS + ph) / sec), id = a.id + '|' + k, long = a.last_ts > 0 && nowS - a.last_ts > 1800;
  const kind = Object.keys(WEIGHT).map(w => [-Math.log(rnd(id + '|' + w)) / (WEIGHT[w] * (w === 'sofa' && long ? 2 : 1)), w]).sort((p, q) => p[0] - q[0])[0][1];
  return { kind, left: (k + 1) * sec - ph - nowS };
};
const good = people => people.filter(p => p.kind === 'tea').length >= 2 && new Set(people.map(p => p.kind)).size >= 3;     // two at the tea table and two other things
const sameTable = people => { const n = {}; people.filter(p => p.kind === 'tea').forEach(p => { n[p.table] = (n[p.table] || 0) + 1; }); return Math.max(0, ...Object.values(n)) >= 2; };     // and two of the tea sitters share a table

(async () => {
  const browser = await chromium.launch();
  const outside = [], consoleErrors = [];
  const open = async (url, opts = {}) => {
    const ctx = await browser.newContext({ viewport: { width: 1600, height: opts.height || 900 }, deviceScaleFactor: 1, timezoneId: zoneNow, locale: lang === 'ko' ? 'ko-KR' : 'en-US', colorScheme: 'dark' });
    await ctx.addInitScript(() => { localStorage.setItem('ab.theme', '"dark"'); localStorage.setItem('ab.agentFilter', '"all"'); });     // the agent list shows every agent, not only those of the open debate
    await ctx.addInitScript(clockHook, opts.off || 0);
    if (opts.gameOpen) await ctx.addInitScript(() => localStorage.setItem('ab.gameOpen', 'true'));
    await ctx.route('**/*', route => {
      const u = route.request().url();
      if (u.startsWith(base) || u.startsWith(emptyBase)) return route.continue();
      outside.push(u);
      return route.abort();
    });
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
    page.on('pageerror', e => consoleErrors.push(String(e)));
    await page.goto(url, { waitUntil: 'networkidle' });
    return page;
  };

  // The lounge (SHOOT_LOUNGE=pick): who rests there, what each of them does, and a page that is opened at a moment when it looks right.
  const sittingOf = page => page.evaluate(() => {
    const g = (window.__games || [])[0], G = g && g._g;
    return G ? [...G.ents.values()].filter(e => e.where === 'lounge' && !e.moving && e.act).map(e => ({ id: e.id, name: e.name, kind: e.act.kind, table: e.act.spot.node })) : [];
  });
  let rested = [], cursor = 0;
  if (process.env.SHOOT_LOUNGE === 'pick') {
    const probe = await open(base + '/game?lang=' + lang, { off: 0 });
    await sleep(2500);
    rested = await probe.evaluate(() => [...window.__games[0]._g.ents.values()].filter(e => e.where === 'lounge' && e.agent).map(e => ({ id: e.id, status: e.agent.status, last_ts: e.agent.last_ts })));
    await probe.context().close();
    if (rested.filter(r => r.status === 'done').length < 4) throw new Error('lounge: fewer than four people with status done rest there (' + rested.length + ' in all): nothing to pick a moment for');
  }
  // the next offset (ms) at or after `cursor` where the port of assignLounge says that two sit at the tea table and the picks hold for another 45 s (a page is opened and shot within that)
  const nextMoment = () => {
    const t0 = Date.now() / 1000;
    for (let off = cursor; off < cursor + 6 * 3600; off += 5) {
      const picks = rested.map(r => r.status === 'done' ? pickAt(r, t0 + off) : { kind: 'sofa', left: Infinity });      // the others (stopped, failed...) sit on the sofa
      if (picks.every(p => p.left >= 45) && good(picks)) { cursor = off + 600; return off * 1000; }
    }
    throw new Error('lounge: no moment found');
  };
  // open a page that shows the office; with SHOOT_LOUNGE=pick, again and again until the page itself shows two people at the tea table and at least two other things
  const openPicked = async (url, opts = {}) => {
    if (process.env.SHOOT_LOUNGE !== 'pick') return open(url, opts);
    for (let i = 0; i < 30; i++) {
      const off = nextMoment(), pg = await open(url, Object.assign({}, opts, { off }));
      await sleep(2500);
      const seen = await sittingOf(pg), ok = good(seen) && sameTable(seen);
      console.log('[%s] %s office +%ds: %s %s', lang, url.replace(base, '').split('?')[0] || '/', off / 1000, seen.map(q => q.name + '=' + q.kind + (q.kind === 'tea' ? '@' + q.table : '')).join(' '), ok ? 'OK' : 'no');
      if (ok) return pg;
      await pg.context().close();
    }
    throw new Error('lounge: no moment where the page shows two people at one tea table (30 tried)');
  };

  // dashboard: size the window to the end of the office row, so the plan bar (fixed at the bottom) closes the picture
  let page = await openPicked(base + '/?lang=' + lang, { gameOpen: true });
  await page.waitForFunction(() => document.querySelector('#topics') && document.querySelector('#topics').children.length > 0);
  await sleep(2500);
  if (process.env.SHOOT_FOLD_ALERTS) { await page.evaluate(() => { const b = document.querySelector('#alertsFold'); if (b) b.click(); }); await sleep(500); }
  const bottom = await page.evaluate(() => Math.max(...['#gameCard', '#atalkCard'].map(s => document.querySelector(s).getBoundingClientRect().bottom + scrollY)));
  await page.setViewportSize({ width: 1600, height: Math.ceil(bottom) + 54 });
  await sleep(1500);
  await page.screenshot({ path: path.join(out, 'dashboard.png') });

  // from here on the page is frozen (the screen asks for new data every 3 seconds and would redraw the cards that are hidden below), and nothing floats over the pictures
  await page.route('**/api/state*', () => {});
  await page.addStyleTag({ content: '#planBar,#alerts{display:none!important} header.top{position:static!important}' });

  // agents.png: the agent list cut down to one launched tree (a run, the run it launched and the one that run launched) and three runs that stopped for different reasons,
  // so that the new states show in one picture (the rest of the list is just more working agents)
  await page.evaluate(() => {
    const list = document.querySelector('#agentList'), cards = [...list.querySelectorAll('.card.agent')], kids = cards.filter(c => c.classList.contains('child'));
    const tree = kids.length ? [cards[cards.indexOf(kids[0]) - 1], ...kids] : [];
    const stopped = cards.filter(c => c.querySelector('.dot.interrupted, .dot.unknown')).slice(0, 2);
    cards.forEach(c => { if (!tree.includes(c) && !stopped.includes(c)) c.style.display = 'none'; });
    list.querySelectorAll('.group-label').forEach(e => { e.style.display = 'none'; });
  });
  await sleep(300);
  await page.locator('aside.agents').screenshot({ path: path.join(out, 'agents.png') });
  await page.evaluate(() => { document.querySelectorAll('#agentList .card.agent, #agentList .group-label').forEach(e => { e.style.display = ''; }); });

  // debates: the table only (no sticky header or fixed bar over it); keep the first n topics
  const keep = process.env.SHOOT_TOPICS || '5';
  // a card with no participant row (a folder that only holds a brief) is not a topic of the picture; the first n cards that have participants stay (or the cards of the topic numbers listed: T1,T3)
  await page.evaluate(spec => {
    const nums = /^\d+$/.test(spec) ? null : spec.split(',').map(x => x.trim().toUpperCase());
    let seen = 0;
    [...document.querySelectorAll('#topics > *')].forEach(e => {
      const has = !!e.querySelector('table.mx tbody tr'), first = (e.textContent.trim().match(/^T\d+/) || [''])[0];
      if (has && (nums ? nums.includes(first) : seen < +spec)) seen++; else e.style.display = 'none';
    });
  }, keep);
  await sleep(500);
  await page.locator('.main > section').first().screenshot({ path: path.join(out, 'debates.png') });
  await page.context().close();

  page = await openPicked(base + '/game?lang=' + lang, { height: 900 });
  await sleep(4000);
  const gh = await page.evaluate(() => Math.ceil(document.getElementById('legend').getBoundingClientRect().bottom + scrollY) + 18);     // end of the legend
  await page.setViewportSize({ width: 1600, height: gh });
  await sleep(1500);
  await page.screenshot({ path: path.join(out, 'office.png') });
  await page.context().close();

  page = await open(emptyBase + '/?lang=' + lang, { height: 760 });
  await sleep(1500);
  await page.screenshot({ path: path.join(out, 'empty.png') });
  await page.context().close();

  await browser.close();
  console.log('[%s] outside requests: %d, console errors: %d', lang, outside.length, consoleErrors.length);
  outside.forEach(u => console.log('  outside: ' + u));
  consoleErrors.forEach(e => console.log('  console: ' + e));
  process.exit(outside.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
