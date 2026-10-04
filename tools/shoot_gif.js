// Frames for docs/images/<lang>/office.gif: the "New work" demo of the office page (/game?demo=new, static/game-demo.js) played on a time that this script steps by hand, one picture per step.
// usage: NODE_PATH=<pw>/node_modules PLAYWRIGHT_BROWSERS_PATH=<pw>/browsers node tools/shoot_gif.js <board url> <frames dir> [en|ko]
//   tools/shoot_gif.py starts the board on a synthetic HOME (tools/docs_scene.py), runs this once per language and turns the frames into the GIF.
//   Writes <frames dir>/f0001.png ... and <frames dir>/frames.json ({ fps, count, hold, at }); prints one line per try of the start time.
// The scene is the docs scene cut down to two topics (a working room and a closed one whose three reviewers rest) so that the office is two rows high and the new room sits next to the lounge.
// The clock of the page (Date, timers, requestAnimationFrame) is Playwright's fake one and stands still between steps: every picture is exact whatever the speed of the machine, and the walk of a
// person is the walk of the page (4 px every 64 ms of that clock). The demo is shown at different speeds (PLAN): the walks nearly as they are, the waiting much faster. The caption of the demo
// ("Assigned: T9-A comes in at the entrance and goes to a desk") stays under the office as a line of its own; its buttons are hidden.
// The picks of the people who rest in the lounge (see assignLounge in static/game.js) change with the time of day, and some times give a dull lounge (everybody on the sofa) or name tags that cross each
// other while somebody walks to a crowded place. So the clock of the page starts at 0, 37, 74... seconds after the start of this script, and each start is played without pictures first: the six people
// who rest at the end must do at least three different things (GOOD, at most three of them on the sofa), and the pictures in which a name tag lies over another name tag are counted (a person who walks past somebody's tag; a bubble over a tag is how the page draws a bubble and is not counted).
// The first start with at most LIMIT such pictures is shot, else the one with the fewest (the clock is exact, so the shot repeats the try). GIF_AT=<seconds> uses that start without trying;
// GIF_FPS=<n> changes the 12 pictures per second.
// All dark; every request must stay on the board (fonts are served by it); nothing real is read: the board runs on a synthetic HOME.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const [base, out, lang = 'en'] = process.argv.slice(2);
if (!base || !out || !['en', 'ko'].includes(lang)) { console.error('usage: shoot_gif.js <board url> <frames dir> [en|ko]'); process.exit(2); }

const FPS = +process.env.GIF_FPS || 12;
const FRAME_MS = 1000 / FPS;
const KEEP = ['t3_retry', 't4_config'];        // the topics of the docs scene that stay: T3 (three people write) and T4 (settled; its three reviewers rest in the lounge)
// The story in the time of the demo (ms after it starts): [from, to, speed]. 0-10.4 s the orchestrator walks to the user's office and listens (the first 9.2 s pass without a picture),
// 10.4-19.4 A, B and C come in at the entrance and walk to their desks, 19.4-30 they read and draft (B's thought is in a bubble from 25.8 s), 30-44.5 they submit one after another and walk to the
// lounge while the orchestrator goes to report to the user.
const PLAN = [[9200, 10400, 1.4], [10400, 19400, 2.5], [19400, 25800, 8], [25800, 29800, 3], [29800, 44500, 3]];
const HOLD_MS = 1000;                          // the last picture stays (at normal speed); tools/shoot_gif.py fades the end of it out and the start in
// The bubbles of the reports ("Wrote the round 1 report ...": three of them over the lounge at once, covering its name tags) are left out: the paper that flies to the orchestrator and the caption tell it.
const HIDE = '.ag-bubble.rep';
const TRIES = 10, STEP_S = 37;                 // how many starts are tried at most, and how far apart (seconds)
const LIMIT = 6;                               // pictures with a name tag over another one that are still good enough
const sleep = ms => new Promise(r => setTimeout(r, ms));
const T0 = Date.now();                         // the clock of every try starts at T0 + a multiple of STEP_S

// Cut the state of the docs scene down to KEEP. A settled topic keeps its room 20 minutes after the last activity of its people, and the docs scene was written some minutes ago: the last activity of the
// reviewers of a settled topic is put at 14 minutes before now, so that the room (and the people in the lounge) are the same whenever the pictures are taken.
const problems = [];
const trim = j => {
  const d = j.debates.find(x => x.current);
  d.topics = d.topics.filter(t => KEEP.includes(t.key));
  if (d.topics.length !== KEEP.length) problems.push('the docs scene has no topics ' + KEEP.join(', ') + ' any more (tools/docs_scene.py changed?)');
  const ids = new Set(d.topics.flatMap(t => t.rows.flatMap(r => r.agents)));
  j.agents = j.agents.filter(a => ids.has(a.id));
  const settled = new Set(d.topics.filter(t => t.final && t.final.exists).flatMap(t => t.rows.flatMap(r => r.agents)));
  j.agents.forEach(a => { if (settled.has(a.id)) a.last_ts = j.now - 840; });
  j.debates = [d];
  j.alerts = [];
  if (j.orch) { j.orch.last_say = ''; j.orch.last_say_ts = 0; }      // no sentence of the docs scene (it names topics that are not here) over the orchestrator
  return j;
};

(async () => {
  const browser = await chromium.launch();
  const outside = [], consoleErrors = [];
  fs.mkdirSync(out, { recursive: true });
  fs.readdirSync(out).filter(f => /^f\d{4}\.png$|^frames\.json$/.test(f)).forEach(f => fs.unlinkSync(path.join(out, f)));      // the frames of an earlier run, nothing else

  // Open the office page on a clock that starts `off` ms after T0 and stands still from the first instant (nothing of the page runs by itself: the clock is only moved by runFor), so that the same
  // `off` gives the same story to the millisecond; then start the demo and run it to the start of the first PLAN step before the page is handed over.
  const open = async off => {
    const ctx = await browser.newContext({ viewport: { width: 1100, height: 720 }, deviceScaleFactor: 1, locale: lang === 'ko' ? 'ko-KR' : 'en-US', colorScheme: 'dark' });
    await ctx.addInitScript(() => localStorage.setItem('ab.theme', '"dark"'));
    let polls = 0;
    await ctx.route('**/*', async route => {
      const u = route.request().url();
      if (!u.startsWith(base)) { outside.push(u); return route.abort(); }
      if (!/\/api\/state/.test(u)) return route.continue();
      if (polls++) return route.fulfill({ json: { unchanged: true } });         // the page asks again every 3 s of its clock: nothing changes under the demo
      const r = await route.fetch();
      return route.fulfill({ json: trim(await r.json()) });
    });
    await ctx.clock.install({ time: T0 + off });
    await ctx.clock.pauseAt(T0 + off + 1000);
    const page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
    page.on('pageerror', e => consoleErrors.push(String(e)));
    await page.goto(base + '/game?lang=' + lang, { waitUntil: 'load' });
    for (let i = 0; !(await page.evaluate(() => !!(window.game && game._g && game._g.ents.size > 0))); i++) {      // (waitForFunction needs the clock)
      if (i > 200) throw new Error('the office did not come up');
      await sleep(100);
    }
    if (problems.length) throw new Error(problems[0]);
    await page.evaluate(() => document.fonts.ready);
    await page.clock.runFor(1500);
    // the caption of the demo goes under the office (a line of its own, one line high), its buttons are hidden, and nothing fades by the real clock of the browser
    await page.addStyleTag({ content: '.ag-demo{position:relative!important;left:auto!important;top:auto!important;transform:none!important;margin:8px auto 0!important;height:28px;white-space:nowrap}' +
      '.ag-demo button{display:none!important} .ag-bubble{transition:none!important} ' + HIDE + '{display:none!important}' });
    await page.evaluate(() => { game.demo('new', {}); const wrap = game._g.wrap; wrap.parentElement.appendChild(document.querySelector('.ag-demo')); });
    await page.clock.runFor(PLAN[0][0]);
    return page;
  };
  const sitting = page => page.evaluate(() => [...game._g.ents.values()].filter(e => e.where === 'lounge' && !e.moving && e.act).map(e => ({ name: e.name, kind: e.act.kind })));
  // how many of the name tags on the page lie over another one (a few pixels of the edge do not count)
  const crossings = page => page.evaluate(() => {
    const tags = [...document.querySelectorAll('.ag-tag')].filter(e => { const c = getComputedStyle(e); return c.display !== 'none' && c.visibility !== 'hidden'; }).map(e => e.getBoundingClientRect());
    let n = 0;
    for (let i = 0; i < tags.length; i++) for (let j = i + 1; j < tags.length; j++) {
      const a = tags[i], b = tags[j];
      if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2) n++;
    }
    return n;
  });
  // frame times (ms of the demo) of the whole story
  const times = [];
  PLAN.forEach(([from, to, speed]) => { for (let t = Math.max(from, times.length ? times[times.length - 1] + 1 : from); t < to; t += FRAME_MS * speed) times.push(t); });
  const holdFrames = Math.round(HOLD_MS / FRAME_MS);

  // the story is played once without pictures per start (the same steps as the shot), until one is good enough (see the head of this file); then once more at that start with pictures
  const GOOD = people => people.length >= 6 && new Set(people.map(p => p.kind)).size >= 3 && people.filter(p => p.kind === 'sofa').length <= 3;
  const stepsOf = async (page, each) => {
    let last = PLAN[0][0];
    for (const t of times) { if (t > last) { await page.clock.runFor(Math.round(t - last)); last = t; } await each(); }
    for (let i = 0; i < holdFrames; i++) { await page.clock.runFor(Math.round(FRAME_MS)); await each(); }
  };
  let at = process.env.GIF_AT !== undefined ? +process.env.GIF_AT : -1, tried = null;       // tried: the lounge at the end of the chosen try
  if (at < 0) {
    let best = null;
    for (let i = 0; i < TRIES; i++) {
      const page = await open(i * STEP_S * 1000);
      let dirty = 0;
      await stepsOf(page, async () => { if (await crossings(page)) dirty++; });
      const seen = await sitting(page), ok = GOOD(seen);
      console.log('[%s] start +%ds: %s %s, %d pictures with tags over tags', lang, i * STEP_S, seen.map(p => p.name + '=' + p.kind).join(' '), ok ? 'OK' : 'no', dirty);
      await page.context().close();
      if (ok && (!best || dirty < best.dirty)) best = { at: i * STEP_S, dirty, lounge: seen.map(p => p.name + '=' + p.kind).join(' ') };
      if (ok && dirty <= LIMIT) break;
    }
    if (!best) throw new Error('no start time gives a lively lounge (' + TRIES + ' tried)');
    at = best.at;
    tried = best.lounge;
  }

  const page = await open(at * 1000);
  const box = await page.evaluate(() => { const w = game._g.wrap.getBoundingClientRect(), b = document.querySelector('.ag-demo').getBoundingClientRect(); return { x: w.x, y: w.y, width: w.width, height: b.bottom + 8 - w.y }; });
  const clip = { x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height) };
  console.log('[%s] picture %dx%d, %d steps + %d held', lang, clip.width, clip.height, times.length, holdFrames);
  let n = 0;
  await stepsOf(page, () => page.screenshot({ path: path.join(out, 'f' + String(++n).padStart(4, '0') + '.png'), clip }));
  const finalSeen = await sitting(page);
  const lounge = finalSeen.map(p => p.name + '=' + p.kind).join(' ');
  console.log('[%s] at the end: %s', lang, lounge);
  if (!GOOD(finalSeen) || (tried !== null && lounge !== tried)) throw new Error('the lounge at the end of the shot differs from the one of the try (the page is no longer deterministic?)');
  fs.writeFileSync(path.join(out, 'frames.json'), JSON.stringify({ fps: FPS, count: n, hold: holdFrames, at }));
  await page.context().close();
  await browser.close();
  console.log('[%s] outside requests: %d, console errors: %d', lang, outside.length, consoleErrors.length);
  outside.forEach(u => console.log('  outside: ' + u));
  consoleErrors.forEach(e => console.log('  console: ' + e));
  process.exit(outside.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
