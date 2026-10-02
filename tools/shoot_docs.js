// Screenshots for docs/images/<lang>/ with playwright. Needs two running boards (tools/shoot_docs.py starts them on a synthetic HOME).
// usage: NODE_PATH=<pw>/node_modules PLAYWRIGHT_BROWSERS_PATH=<pw>/browsers node tools/shoot_docs.js <board url> <empty board url> <out dir> [en|ko]
//   The screen language is the last argument (default en): it goes into every address as ?lang= and into the browser's locale, so the board shows that language whatever the machine's settings are.
//   dashboard.png  1600 wide: top cards + office + agent talk card (the office opened, as a user would open it)
//   office.png     /game, 1600 wide
//   agents.png     the agent list cut down to a launched tree and three runs that stopped (the new states)
//   debates.png    the debate table (topics x rounds x participants), cut to the first topics so the picture stays small
//   empty.png      first screen of a board that has no records
// All dark: the colour scheme is dark and the saved theme is "dark" (board.js keeps it as JSON in localStorage 'ab.theme').
// Every request must stay on the two boards (fonts are served by them): anything else aborts the run. UTC, so no local time zone shows.
// SHOOT_FOLD_ALERTS=1 folds the alert card before the dashboard picture; SHOOT_TOPICS=n keeps the first n topics that have participants in debates.png (default 5).
const { chromium } = require('playwright');
const path = require('path');
const [base, emptyBase, out, lang = 'en'] = process.argv.slice(2);
if (!['en', 'ko'].includes(lang)) { console.error('language must be en or ko'); process.exit(2); }
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch();
  const outside = [], consoleErrors = [];
  const open = async (url, opts = {}) => {
    const ctx = await browser.newContext({ viewport: { width: 1600, height: opts.height || 900 }, deviceScaleFactor: 1, timezoneId: 'UTC', locale: lang === 'ko' ? 'ko-KR' : 'en-US', colorScheme: 'dark' });
    await ctx.addInitScript(() => { localStorage.setItem('ab.theme', '"dark"'); localStorage.setItem('ab.agentFilter', '"all"'); });     // the agent list shows every agent, not only those of the open debate
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

  // dashboard: size the window to the end of the office row, so the plan bar (fixed at the bottom) closes the picture
  let page = await open(base + '/?lang=' + lang, { gameOpen: true });
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
    const stopped = cards.filter(c => c.querySelector('.dot.interrupted, .dot.unknown')).slice(1, 4);
    cards.forEach(c => { if (!tree.includes(c) && !stopped.includes(c)) c.style.display = 'none'; });
    list.querySelectorAll('.group-label').forEach(e => { e.style.display = 'none'; });
  });
  await sleep(300);
  await page.locator('aside.agents').screenshot({ path: path.join(out, 'agents.png') });
  await page.evaluate(() => { document.querySelectorAll('#agentList .card.agent, #agentList .group-label').forEach(e => { e.style.display = ''; }); });

  // debates: the table only (no sticky header or fixed bar over it); keep the first n topics
  const keep = +(process.env.SHOOT_TOPICS || 5);
  // a card with no participant row (a folder that only holds a brief) is not a topic of the picture; the first n cards that have participants stay
  await page.evaluate(n => { let seen = 0; [...document.querySelectorAll('#topics > *')].forEach(e => { if (e.querySelector('table.mx tbody tr') && seen < n) seen++; else e.style.display = 'none'; }); }, keep);
  await sleep(500);
  await page.locator('.main > section').first().screenshot({ path: path.join(out, 'debates.png') });
  await page.context().close();

  page = await open(base + '/game?lang=' + lang, { height: 900 });
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
