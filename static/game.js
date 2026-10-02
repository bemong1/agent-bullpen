/* Agent office — the pixel-art game view.
 * Takes the same /api/state data as the dashboard and draws it. It makes no summary or model call.
 * A speech-bubble sentence is a sentence from the records (the message summary field, the first sentence of a report, a tool action) shortened by fixed rules.
 * Every room has a door in its front wall, and a person walks room → door → corridor → (the vertical passage on the left) → door → seat.
 *
 *   const game = AgentGame.mount(el, { mode: 'strip' | 'full', onAgent: id => ... });
 *   game.update(state, debate);
 *
 * It is split into three files. Load order: game-art.js → game.js → game-demo.js.
 *   game-art.js  pixel-art data and sprites (window.AgentGameArt). Only what has nothing to do with layout or drawing
 *   game.js      this file: layout, path finding, event staging, drawing, the legend. Exports what the demo file leans on as AgentGame._core
 *   game-demo.js demo scenarios (adds AgentGame.demoMenu / scenarios / _demo). mount calls it from game.demo() and stopDemo()
 *
 * Lounge: a resting person (an agent that is not working) picks one of sofa, tea table, games, gym (treadmill, dumbbells) or coffee for each time slot (150 s, 20 s in the demo)
 * and walks there (layoutLounge = the spots, assignLounge = the assignment, step = detects a slot change, actSprite·actProps = pose and props). Only the looks change.
 */
(function () {
  'use strict';
  const { SHIRTS, HAIR, C, ICONS, LOGO, charSprite, icon, LOUNGE_ART } = window.AgentGameArt;   // game-art.js

  const WALL = 36, ROOM = 82, CORR = 16, ROWH = WALL + ROOM + CORR, HALL = 18, HX = 3, FPS = 20;
  const ERRAND_STAY = 7000;   // how long the orchestrator stays in the user's office listening to a request or reporting
  const SEAT = 38, SOFA = 132, SOFA_SEATS = 6, MAX_SOFAS = 4;   // a 6-seat sofa in 2 tiers: the lounge width stays the same up to 12 people
  const UNIT_SEATS = 2, UNIT_PITCH = 22, UNIT_W = 38;   // the small 2-seat sofa drawn in the lounge (width 38, the armrests inside the width). Separate from the width rule (SOFA·SOFA_SEATS). Seat pitch 22 = the width at which a short tag (T1-A) does not overlap the next seat even at scale 1.75
    // Lounge activity: a resting person picks a facility again for every time slot (seconds). Each person's slots start at a different offset (id hash), and the demo has short slots
  const SLOT_SEC = 150, DEMO_SLOT_SEC = 20, LONG_REST_SEC = 1800;
  const ACT_WEIGHT = { sofa: 35, tea: 20, game: 15, tread: 8, dumb: 7, coffee: 10 };   // gym 15 = treadmill 8 + dumbbells 7. The window side (5) is left out: there is no place to put it
  const ACT_KINDS = Object.keys(ACT_WEIGHT);
  const hash32 = s => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } h ^= h >>> 15; h = Math.imul(h, 2246822519); h ^= h >>> 13; h = Math.imul(h, 3266489917); h ^= h >>> 16; return h >>> 0; };
  const rnd = s => (hash32(s) + 0.5) / 4294967296;      // (0,1): the same text gives the same number
  // ---------- shortening text (rules only, no summarizing model) ----------
  function firstSentence(text, n = 56) {
    let s = String(text || '')
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/^\s*[#>|-].*$/gm, m => m.replace(/^[\s#>|-]+/, ''))
      .replace(/\*\*|__|`/g, '')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
      .replace(/(?:~|\/)(?:[\w.-]+\/)+([\w.-]+\/[\w.-]+)/g, '$1')
      .replace(/\s+/g, ' ').trim();
    const m = s.match(/^(.+?[.!?。])(\s|$)/);
    if (m && m[1].length >= 8) s = m[1];
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }
  // short name of a model (same as the server's naming rule): claude-opus-5-5 → opus5.5, gpt-6.1-sol → sol6.1, gpt-6-astra → astra6
  function modelShort(m) {
    m = String(m || '').replace(/\[1m\]/, '').replace(/-\d{8}$/, '');
    let k = m.match(/^claude-([a-z]+)-(\d+)(?:-(\d+))?$/);
    if (k) return k[1] + k[2] + (k[3] ? '.' + k[3] : '');
    m = m.replace(/^gpt-/, '');
    k = m.match(/^(\d+(?:\.\d+)?)-([a-z]+)$/) || m.match(/^([a-z]+)-(\d+(?:\.\d+)?)$/);
    return k ? (/^\d/.test(k[1]) ? k[2] + k[1] : k[1] + k[2]) : m;
  }
  // over-the-head icon: for Codex use the category (cat) the server attaches to each activity; for Claude pick by tool name as before
  // (the server category treats Agent as msg, but the office has always drawn it with the think icon)
  function toolIcon(lt, a) { const c = (a && a.provider === 'codex' && lt.cat) || catOf(lt.name); return c !== 'other' && ICONS[c] ? c : 'think'; }
  function catOf(name) {
    if (['Read', 'Grep', 'Glob'].includes(name)) return 'read';
    if (['Write', 'Edit', 'NotebookEdit'].includes(name)) return 'write';
    if (name === 'Bash') return 'bash';
    if (['WebFetch', 'WebSearch'].includes(name)) return 'web';
    if (['SendMessage', 'SubagentHandback'].includes(name)) return 'msg';
    return 'think';
  }

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const STYLE = `
  .ag-wrap { position: relative; overflow: hidden; background: #15132a; border-radius: 10px; font-family: 'Galmuri11', 'Galmuri9', ui-monospace, monospace; }
  .ag-wrap canvas { display: block; image-rendering: pixelated; image-rendering: crisp-edges; cursor: pointer; }
  .ag-ov { position: absolute; inset: 0; pointer-events: none; z-index: 1; }
  .ag-wrap canvas.ag-top { position: absolute; left: 0; top: 0; z-index: 2; pointer-events: none; }
  .ag-ov.ag-ov2 { z-index: 3; }
  .ag-tag { position: absolute; z-index: 1; transform: translateX(-50%); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    font-size: 11px; line-height: 1.25; color: #f3f1ff; background: rgba(15,13,32,.86); padding: 1px 5px; border-radius: 3px;
    pointer-events: auto; cursor: pointer; text-align: center; }
  .ag-tag i { display: inline-block; width: 6px; height: 6px; margin-right: 4px; vertical-align: 1px; }
  .ag-plate { position: absolute; z-index: 1; transform: translateX(-50%); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    font-size: 9px; line-height: 1.15; font-weight: 700; color: #2a1a0c; background: #e8cf95; border: 1px solid #b8913f; padding: 0 3px; border-radius: 2px; pointer-events: none; }
  .ag-legend { display: flex; gap: 6px 14px; flex-wrap: wrap; color: #8d96a5; font-size: 12px; margin-top: 8px; }
  .ag-legend span { display: inline-flex; align-items: center; gap: 5px; }
  .ag-legend i { width: 10px; height: 10px; display: inline-block; }
  .ag-legend i.pl { width: auto; height: auto; font-style: normal; font-size: 9px; font-weight: 700; color: #2a1a0c; background: #e8cf95; border: 1px solid #b8913f; padding: 0 3px; border-radius: 2px; }
  .ag-tag i.cx { transform: rotate(45deg) scale(.8); }   /* Codex: the status dot is a ◆ (the width stays the same) */
  .ag-tag.dim { color: #9a98b5; }
  .ag-tag.hl, .ag-tag.hl .nm { font-weight: 700; }   /* the tag of the person highlighted from a conversation card */
  .ag-tag.lg { z-index: 2; }   /* lounge tag: stays above the nameplates and tags of other rooms even when it overlaps them while walking (a reused element comes earlier in DOM order, so z-index protects it). Still below the people layer (canvas.ag-top, z-index 2): .ag-ov is a stacking context at z-index 1 */
  .ag-tag.two { white-space: normal; text-overflow: clip; line-height: 1.2; padding: 1px 2px 2px; word-break: keep-all; overflow-wrap: anywhere;
    font-family: 'Galmuri9', 'Galmuri11', ui-monospace, monospace; font-size: 10px; }
  .ag-tag.two .nm { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ag-tag.two .rl { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; color: #cfcbe8; }
  .ag-board { position: absolute; z-index: 1; color: #262640; font-size: 11px; line-height: 1.3; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .ag-board b { color: #1b1b2a; }
  .ag-board .st { color: #4b4f6b; font-size: 11px; }
  .ag-bubble { position: absolute; z-index: 3; transform: translate(-50%, -100%); max-width: 230px; background: #fffdf5; color: #1b1b2a;
    border: 2px solid #1b1b2a; padding: 4px 7px 5px; font-size: 12px; line-height: 1.35; box-shadow: 3px 3px 0 rgba(0,0,0,.35);
    transition: opacity .6s; word-break: keep-all; overflow-wrap: anywhere; pointer-events: none; }
  .ag-bubble::after { content: ''; position: absolute; left: 50%; bottom: -8px; margin-left: -5px; border: 5px solid transparent; border-top: 5px solid #1b1b2a; }
  .ag-bubble.orch { background: #fff1e3; } .ag-bubble.user { background: #f3eaff; } .ag-bubble.rep { background: #e8fff0; } .ag-bubble.peer { background: #e6f3ff; }
  .ag-bubble .who { font-size: 11px; color: #6b5a3a; display: block; }
  .ag-sign { position: absolute; z-index: 1; font-size: 11px; color: #d8d6f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }   /* the width is the room's (buildLabels) */
  .ag-demo { position: absolute; left: 50%; top: 8px; transform: translateX(-50%); z-index: 5; pointer-events: auto;
    background: #fff4c2; color: #1b1b2a; border: 2px solid #1b1b2a; padding: 4px 10px; font-size: 12px; box-shadow: 3px 3px 0 rgba(0,0,0,.35); width: max-content; max-width: calc(100% - 16px); text-align: center; }   /* a long caption wraps inside the office instead of running past it */
  .ag-demo button { font: inherit; margin-left: 8px; cursor: pointer; }
  .ag-menu { position: absolute; z-index: 60; width: 350px; background: #1b1930; color: #ecebff; border: 2px solid #0d0c1a; box-shadow: 4px 4px 0 rgba(0,0,0,.45);
    font-family: 'Galmuri11', 'Galmuri9', ui-monospace, monospace; font-size: 12px; padding: 4px; }
  .ag-menu .hd { color: #9a98b5; padding: 6px 8px 4px; font-size: 11px; }
  .ag-menu button { display: grid; grid-template-columns: 1fr auto; gap: 2px 8px; width: 100%; text-align: left; background: none; border: 0; color: inherit;
    font: inherit; padding: 7px 8px; cursor: pointer; border-top: 1px solid #2c2a48; }
  .ag-menu button:hover { background: #2c2a48; }
  .ag-menu button span { color: #e3b341; }
  .ag-menu button small { grid-column: 1 / -1; color: #b7b4d6; font-size: 11px; }`;
  let styled = false;
  function ensureStyle() {
    if (styled) return;
    const st = document.createElement('style'); st.textContent = STYLE; document.head.appendChild(st); styled = true;
  }

  function mount(el, opts = {}) {
    ensureStyle();
    el.innerHTML = '';
    const wrap = document.createElement('div'); wrap.className = 'ag-wrap';
    // layers: floor · furniture (cv) < tags · boards (ov) < people · icons · paper (cv2) < speech bubbles (ov2)
    const cv = document.createElement('canvas');
    const ov = document.createElement('div'); ov.className = 'ag-ov';
    const cv2 = document.createElement('canvas'); cv2.className = 'ag-top';
    const ov2 = document.createElement('div'); ov2.className = 'ag-ov ag-ov2';
    wrap.append(cv, ov, cv2, ov2); el.appendChild(wrap);
    const G = {
      el, wrap, cv, cx: cv.getContext('2d'), ov, cv2, cx2: cv2.getContext('2d'), ov2, opts, scale: 2, W: 0, H: 0, sig: '', blocks: [], seats: [], rows: 1,
      ents: new Map(), bubbles: [], papers: [], lastIdx: null, state: null, debate: null, frame: 0,
      visible: true, bg: null, lastAct: new Map(), lastBubble: new Map(), labels: [], tagWc: new Map(), ltags: new Map(),
      hl: null, hlTags: new Map(),   // highlight: the set of person keys (agent ids and 'orch') and their tag elements
    };
    new IntersectionObserver(es => { G.visible = es[0].isIntersecting; }).observe(wrap);
    if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', () => { G.tagWc.clear(); G.loungeNext = 1; G.ov.querySelectorAll('.ag-board').forEach(fitStage); });   // when the font changes, measure the tag widths again and redo the assignment
    let lastW = 0;
    new ResizeObserver(() => { const w = el.clientWidth; if (w === lastW) return; lastW = w; G.sig = ''; if (G.state) relayout(G); }).observe(el);
    if (opts.mode === 'full') addEventListener('resize', () => { G.sig = ''; if (G.state) relayout(G); });
    cv.addEventListener('click', ev => {
      const r = cv.getBoundingClientRect(), x = (ev.clientX - r.left) / G.scale, y = (ev.clientY - r.top) / G.scale;
      for (const e of G.ents.values()) if (e.id && x >= e.x - 2 && x <= e.x + 14 && y >= e.y - 2 && y <= e.y + 18) return opts.onAgent && opts.onAgent(e.id);
    });
    let last = 0;
    (function loop(t) {
      requestAnimationFrame(loop);
      if (!G.visible || document.hidden || !G.state || t - last < 1000 / FPS) return;
      last = t; G.frame++; step(G); draw(G);
    })(0);
    return {
      update: (s, d) => { if (G.demo) { G.demo.real = [s, d]; return; } update(G, s, d); },
      relayout: () => { G.sig = ''; relayout(G); },
      highlight: ids => setHl(G, ids),   // highlight the sender and the receiver while the mouse is over a conversation card (null clears it). Ignored during the demo
      demo: (key, hooks) => typeof key === 'function' ? window.AgentGame._demo.run(G, 'basic', { onEnd: key }) : window.AgentGame._demo.run(G, key, hooks),   // the demo is in game-demo.js
      stopDemo: () => window.AgentGame._demo.stop(G),
      get demoOn() { return !!G.demo; },
      _g: G,   // for tests (tools/regress/lounge_checks.js)
    };
  }

  // ---------- highlight (the people the mouse is over on a conversation card) ----------
  // Draws a bright ring under the feet of the sender and the receiver and makes their tags bold. People who are not in the office (earlier agents etc.) are skipped (they are looked up in ents when drawing).
  // With no highlight (G.hl null) neither a ring nor a class is added: the picture and the tags are as before. Nothing is highlighted during the demo.
  const ringPx = (rx, ry) => { const q = new Set(); for (let i = 0; i < 96; i++) { const a = i / 96 * 2 * Math.PI; q.add(Math.round(Math.cos(a) * rx) + ',' + Math.round(Math.sin(a) * ry)); } return [...q].map(k => k.split(',').map(Number)); };
  const RING = ringPx(9, 3), RING_EDGE = ringPx(10, 4);   // the bright ring and a dark edge outside it (so it shows on a light floor too)
  const hlOn = G => (G.hl && !G.demo ? G.hl : null);
  const setHlCls = (el, on) => { const has = /(^| )hl( |$)/.test(el.className); if (on && !has) el.className += ' hl'; else if (!on && has) el.className = el.className.replace(/ ?\bhl\b/, ''); };
  function applyHl(G) { const hl = hlOn(G); G.hlTags.forEach((el, id) => setHlCls(el, !!hl && hl.has(id) && G.ents.has(id))); }   // even if a seat tag is still there, do not bold it when the person is not in the office (no room in the lounge etc.)
  function setHl(G, ids) { G.hl = ids && ids.length ? new Set(ids) : null; applyHl(G); }
  function drawRings(G, R2, people) {
    const hl = hlOn(G);
    if (!hl) return;
    people.forEach(q => {
      const e = q.e;
      if (!hl.has(e.key)) return;
      const act = e.where === 'lounge' && !e.moving && e.act, h = act ? (act.spot.stand ? 16 : 12) : (q.seated ? 12 : 16);
      const cx = Math.round(e.x) + 6, cy = Math.round(e.y) + h - 1;
      RING_EDGE.forEach(([dx, dy]) => R2('#1b1b2a', cx + dx, cy + dy, 1, 1));
      RING.forEach(([dx, dy]) => R2('#fff4c2', cx + dx, cy + dy, 1, 1));
    });
  }

  // ---------- room layout ----------
  // One row = top wall (WALL) + room floor (ROOM) + corridor (CORR). The HALL-wide strip at the left end is the vertical passage that joins the rows, with the entrance at the very top.
  const RUN = st => st === 'running' || st === 'stalled';
  // Seats beside the launcher: an agent with a parent (a `claude -p` run launched by a sub-agent or by another run) sits right after that parent when the parent is in the same room;
  // the rest keep their order. (The same rule as the agent list on the dashboard: a grandchild follows its parent.)
  function beside(list) {
    const ids = new Set(list.map(a => a.id)), kids = new Map(), out = [], seen = new Set();
    const hung = a => !!a.parent && a.parent !== a.id && ids.has(a.parent);
    list.forEach(a => { if (hung(a)) { if (!kids.has(a.parent)) kids.set(a.parent, []); kids.get(a.parent).push(a); } });
    const walk = a => { if (seen.has(a.id)) return; seen.add(a.id); out.push(a); (kids.get(a.id) || []).forEach(walk); };
    list.forEach(a => { if (!hung(a)) walk(a); });
    list.forEach(walk);                                  // a loop of parents (never expected) still seats everybody
    return out;
  }
  // The desks of the "other work" room keep their people while they work. Somebody new takes the empty end desk, wherever the server lists them (so one more person never moves
  // everybody a desk over), and when somebody leaves the rest close up in the same order. Only the first drawing, with nobody remembered, seats a launched run beside its launcher.
  function deskOrder(G, list) {
    const ids = new Set(list.map(a => a.id)), kept = (G.otherOrder || []).filter(id => ids.has(id)), have = new Set(kept);
    const byId = new Map(list.map(a => [a.id, a]));
    const out = kept.map(id => byId.get(id)).concat(beside(list.filter(a => !have.has(a.id))));
    G.otherOrder = out.map(a => a.id);
    return out;
  }
  const orchProvider = S => (S.orch && S.orch.provider === 'codex' ? 'codex' : 'claude');   // Claude if the field is missing
  function buildBlocks(G) {
    const S = G.state, d = G.debate, blocks = [{ type: 'boss', w: 150 }, { type: 'orch', w: 84 }];
    const byId = new Map(S.agents.map(a => [a.id, a]));
    const inDebate = new Set();
    let idle = 0;
    // A finished topic loses its room, and its people are not put in the lounge either (they count as earlier agents): a topic with nobody working
    // and a final already out, or a room or flat review (no next round to wait for) whose every cell is in (20 minutes of slack after the last activity), or a topic with no participants at all.
    // A topic that finished a round and waits for the next stage (before the final) is kept as it is, and so is a review that has not started or has a cell still open or not handed in
    const nowS = S.now || Date.now() / 1000;
    const topicDone = t => {
      const ids = t.rows.map(r => r.agents[r.agents.length - 1]).filter(Boolean);
      if (ids.some(id => { const a = byId.get(id); return a && RUN(a.status); })) return false;
      if (!t.rows.length) return true;
      if (!(t.final && t.final.exists) && !(noRounds(t) && t.rows.every(r => r.cells.every(c => c.state === 'done')))) return false;
      const lastAct = Math.max(0, ...ids.map(id => (byId.get(id) || {}).last_ts || 0), ...t.rows.flatMap(r => r.cells.map(c => c.mtime || 0)));
      return nowS - lastAct >= 1200;
    };
    (d ? d.topics : []).forEach((t, i) => {
      if (!String(t.dir || '').startsWith('demo/') && topicDone(t)) return;   // demo rooms stay as they are
      const seats = t.rows.map(r => ({ p: r.p, role: r.role, agentId: r.agents[r.agents.length - 1] || null, cells: r.cells }));
      seats.forEach(s => { if (s.agentId) { inDebate.add(s.agentId); const a = byId.get(s.agentId); if (a && !RUN(a.status)) idle++; } });
      blocks.push({ type: 'zone', topic: t, seats, shirt: SHIRTS[i % SHIRTS.length], w: 12 + Math.max(3, seats.length) * SEAT });
    });
    const others = deskOrder(G, S.agents.filter(a => RUN(a.status) && !inDebate.has(a.id)));
    if (others.length) blocks.push({ type: 'zone', other: true, seats: others.map(a => ({ p: a.tag || '·', role: a.title, agentId: a.id })),
      shirt: '#8b8fa8', w: 12 + Math.max(2, others.length) * SEAT });
    // lounge: one sofa per 6 resting people (stacked in 2 tiers, widening sideways past 12 people)
    const sofas = Math.min(MAX_SOFAS, Math.max(1, Math.ceil(idle / SOFA_SEATS)));
    blocks.push({ type: 'lounge', sofas, w: 34 + Math.ceil(sofas / 2) * SOFA });     // sofas are stacked in 2 tiers
    return blocks;
  }
  // ---------- inside the lounge ----------
  // The lounge block width (buildBlocks) stays as it is; inside it the width is split into a left group and a right group. The spare width in the middle is left empty:
  //   left    a sofa at the far left (small 2-seat, two front and back in one column, the third in the next column), and right next to it 1–3 tea tables (small round tables)
  //           tea table = a cross around the round table: top (front view) · bottom (back view) · left (facing right) · right (facing left), all facing the table. If narrow, only top and bottom
  //   right   gathered at the right end (right-aligned, constant column pitch P): coffee (back wall) · 1–4 game machines (back wall, the person in front) · 1–3 treadmills · dumbbell rack
  // When the width runs short, items are dropped from the back of the order below (strict: if an earlier one does not fit, it stops there). Game machines, treadmills and dumbbells are kept even in the narrowest lounge (width 166):
  //   sofa 1 → table 1 (2 chairs) → coffee → game 1 → treadmill 1 → dumbbells → 4 chairs (cross) → table 2 → game 2 → treadmill 2 → sofa 2 → table 3 → game 3 → game 4 → treadmill 3 → sofa 3
  // Each person's tag is one line right below the person (tea table, per seat: top = above the head, left·right = below, on the outer side). The left and right limits (lim) where a tag may sit are measured in advance for each spot. The same width always gives the same layout.
  // spot { kind, key, node, x, y (top left of the picture), dir, stand (16 rows), sy (for the draw order), tag { dx, dy, al ('l'|'r'|none = centered) }, lim [left, right] }, furniture box b.furn { n (node), x, y, w, h }
  const LOUNGE_STEPS = [s => { s.sofa = 1; }, s => { s.tables = 1; }, s => { s.coffee = 1; }, s => { s.game = 1; }, s => { s.tread = 1; }, s => { s.dumb = 1; }, s => { s.chairs4 = true; },
    s => { s.tables = 2; }, s => { s.game = 2; }, s => { s.tread = 2; }, s => { s.sofa = 2; }, s => { s.tables = 3; }, s => { s.game = 3; }, s => { s.game = 4; }, s => { s.tread = 3; }, s => { s.sofa = 3; }];
  function layoutLounge(b, top, sc) {
    const th = Math.ceil(16 / sc), P = 22, UG = 8, GL = 4, TG = 2, MID = 4, TD = 13;         // P column pitch (one tag line), TD round table diameter
    const X0 = b.x + 6, X1 = b.x + b.w - 3, W = X1 - X0, wall = top + 1, foot = top + 18;     // back-wall furniture y, standing person y (the head covers the lower part of the machine)
    const zero = () => ({ sofa: 0, tables: 0, coffee: 0, game: 0, tread: 0, dumb: 0, chairs4: false });
    const lowerOk = 63 + 2 * th <= 84;                                   // do the front and back (upper and lower row) sofas fit in the room including their tags (th 10 = down to scale 1.6). If not, put them side by side in one row
    const sites = lowerOk ? [[0, 0], [0, 1], [1, 0]] : [[0, 0], [1, 0], [2, 0]];   // sofa spots: [column, front/back]. Two per column, front and back, the third in the next column
    const sofaCols = c => c.sofa ? 1 + Math.max(...sites.slice(0, c.sofa).map(q => q[0])) : 0;
    const sofaW = c => sofaCols(c) ? sofaCols(c) * UNIT_W + (sofaCols(c) - 1) * UG : 0, tblW = c => c.chairs4 ? 57 : P;   // cross = body 37 + 20 spare for the outer tags
    const rightW = c => { const z = [c.coffee * P, c.game * P, (c.tread + c.dumb) * P].filter(w => w > 0); return z.reduce((t, w) => t + w, 0); };
    const need = c => sofaW(c) + (c.tables ? GL + c.tables * tblW(c) + (c.tables - 1) * TG : 0) + MID + rightW(c);
    let cfg = zero();
    for (const st of LOUNGE_STEPS) { const nx = Object.assign({}, cfg); st(nx); if (need(nx) > W) break; cfg = nx; }
    const stand = { stand: true, sy: 15, dir: 'up', tag: { dx: 6, dy: 19 } }, sitT = { stand: false, sy: 9, dir: 'down', tag: { dx: 6, dy: 17 } }, sitS = { stand: false, sy: 9, dir: 'down', tag: { dx: 6, dy: 22 } };
    const fac = [], decor = [], fronts = [], furn = [], A = LOUNGE_ART;   // fronts: chair parts drawn in front of a person (chairFront, with the people)
    b.arcs = []; b.treads = [];
    // Left: sofas (2-seat). Two per column, front and back, both facing front (down). The number of sofas actually drawn depends on the number of people using them (assignLounge)
    b.units = []; const seats = [];   // (b.seats is the seat names of a debate room, so the sofa spots are sofaSeats)
    for (let i = 0; i < cfg.sofa; i++) b.units.push({ x: X0 + sites[i][0] * (UNIT_W + UG), y: sites[i][1] ? top + 51 + th : top + 25, w: UNIT_W, row: sites[i][1], col: sites[i][0] });
    b.units.forEach((u, i) => {
      furn.push({ n: 'sofa' + i, x: u.x, y: u.y - 12, w: UNIT_W, h: 23 });
      for (let k = 0; k < UNIT_SEATS; k++) seats.push(Object.assign({}, sitS, { kind: 'sofa', key: 'sofa' + i + '.' + k, node: 'sofa' + i, site: i, k, x: u.x + 2 + k * UNIT_PITCH, y: u.y - 10 }));
    });
    b.sofaSeats = seats;
    // Left: tea tables = a small round table (a TD x TD box, drawn from the front and above) with a chair at each of its four sides (a cross). Everybody sits facing the table.
    for (let t = 0; t < cfg.tables; t++) {
      const w = tblW(cfg), x = X0 + sofaW(cfg) + GL + t * (w + TG), tcx = x + Math.floor(w / 2), ny = top + th + 3, ty = ny + 12, sy = ty + 13, node = 'tea' + t;   // leave one tag line above the head of the top seat
      const seat = (key, bx, by, dir, tag, cup, mouth) => {
        fac.push(Object.assign({}, sitT, { kind: 'tea', key: 'tea.' + t + '.' + key, node, x: bx, y: by, dir, tag, cup, mouth }));
        furn.push({ n: node, x: bx, y: by + 12, w: 12, h: 3 });                                 // chair seat plank
        decor.push(R => A.chair(R, bx, by, dir));                                               // the chair: back, seat, legs (the part behind the person)
        if (dir === 'up') fronts.push({ x: bx, y: by, dir });                                    // seen from behind, the chair back is in front of the person
      };
      seat('N', tcx - 6, ny, 'down', { dx: 6, dy: -(th + 1) }, { x: tcx + 2, y: ty + 1 }, { x: 5, y: 6 });   // top: front view (facing down), the tag above the head
      seat('S', tcx - 6, sy, 'up', { dx: 6, dy: 17 }, { x: tcx - 3, y: ty + 4 });                              // bottom: back view (facing up), the tag below
      if (cfg.chairs4) {
        seat('W', tcx - 18, ty + 1, 'right', { dx: 10, dy: 17, al: 'r' }, { x: tcx - 5, y: ty + 2 }, { x: 8, y: 6 });   // left: facing right, the tag below on the outer (left) side
        seat('E', tcx + 7, ty + 1, 'left', { dx: 1, dy: 17, al: 'l' }, { x: tcx + 4, y: ty + 3 }, { x: 2, y: 6 });      // right: facing left, the tag below on the outer (right) side
      }
      furn.push({ n: node, x: tcx - 6, y: ty, w: TD, h: TD });
      decor.push(R => A.tea(R, tcx - 6, ty));
    }
    // Right end: coffee · games · gym (left → right), constant column pitch P, against the right wall (right-aligned)
    { let x = X1 - rightW(cfg);
      const col = (x0, i) => x0 + P * i + P / 2;
      b.coffee = { x: col(x, 0) - 6, y: wall };
      fac.push(Object.assign({}, stand, { kind: 'coffee', key: 'coffee.0', node: 'coffee', x: b.coffee.x, y: foot }));
      furn.push({ n: 'coffee', x: b.coffee.x, y: wall, w: 12, h: 22 });
      x += P;
      for (let i = 0; i < cfg.game; i++) { const cx = col(x, i);
        fac.push(Object.assign({}, stand, { kind: 'game', key: 'game.' + i, node: 'g' + i, x: cx - 6, y: foot, ph: i * 11 }));
        b.arcs.push({ x: cx - 7, y: wall, key: 'game.' + i, seed: i * 3 });
        furn.push({ n: 'g' + i, x: cx - 7, y: wall, w: 14, h: 22 });
        decor.push(R => A.arcade(R, cx - 7, wall, i));
      }
      x += cfg.game * P;
      const fy = top + 9;
      for (let j = 0; j < cfg.tread; j++) { const cx = col(x, j);
        fac.push(Object.assign({}, stand, { kind: 'tread', key: 'tread.' + j, node: 'tr' + j, x: cx - 6, y: foot }));
        b.treads.push({ x: cx - 9, y: fy, key: 'tread.' + j });
        furn.push({ n: 'tr' + j, x: cx - 9, y: fy, w: 18, h: 27 });
        decor.push(R => A.treadmill(R, cx - 9, fy));
      }
      if (cfg.dumb) { const cx = col(x, cfg.tread);
        fac.push(Object.assign({}, stand, { kind: 'dumb', key: 'dumb.0', node: 'dumb', x: cx - 6, y: foot, dir: 'down' }));
        furn.push({ n: 'dumb', x: cx - 5, y: wall, w: 11, h: 12 });
        decor.push(R => A.rack(R, cx - 5, wall));
      }
    }
    furn.push({ n: 'plant', x: b.x + b.w - 13, y: top + 54, w: 10, h: 18 });                // the plant is in the lower right corner
    b.spots = b.sofaSeats.concat(fac);
    // the left and right limits where a tag (one line right below the person) may sit: between the furniture of other nodes that overlaps the same line and the room walls
    b.spots.forEach(sp => {
      const cx = sp.x + 6, t = sp.y + sp.tag.dy;
      let l = b.x + 2, r = b.x + b.w - 2;
      furn.forEach(f => { if (f.n === sp.node || !(f.y < t + th && f.y + f.h > t)) return; if (f.x + f.w <= cx) l = Math.max(l, f.x + f.w); else if (f.x >= cx) r = Math.min(r, f.x); else { l = cx; r = cx; } });
      sp.lim = [l, r];
    });
    b.furn = furn; b.th = th; b.pitch = P; b.cfg = cfg;
    b.byKind = {}; ACT_KINDS.forEach(k => { b.byKind[k] = b.spots.filter(s => s.kind === k); });
    b.decor = decor; b.chairFronts = fronts; b.sofaN = 1;
  }
  function relayout(G) {
    const blocks = buildBlocks(G);
    const cw = Math.max(320, G.el.clientWidth);
    // sign only the structure (leave out who sits where, so that a new assignment walks in instead of teleporting)
    const sig = cw + '|' + (G.opts.mode === 'full' ? innerHeight : '') + '|' + orchProvider(G.state) + '|' + blocks.map(b => b.type + (b.topic ? b.topic.key : '') + ':' + (b.seats ? b.seats.length : '') + (b.sofas || '')).join(';');
    if (sig === G.sig) {
      G.blocks.forEach((b, i) => { const nb = blocks[i]; if (nb) { b.topic = nb.topic; if (b.seats) b.seats.forEach((s, k) => Object.assign(s, { role: nb.seats[k].role, cells: nb.seats[k].cells, agentId: nb.seats[k].agentId })); } });
      return false;
    }
    G.sig = sig; G.blocks = blocks;
    const flow = W => {
      const rows = []; let cur = [], curW = HALL + 2;
      blocks.forEach(b => { if (cur.length && curW + b.w + 3 > W) { rows.push(cur); cur = []; curW = HALL + 2; } cur.push(b); curW += b.w + 3; });
      rows.push(cur);
      return rows;
    };
    let scale = 2, W = Math.floor(cw / 2), rows = flow(W);
    if (G.opts.mode === 'full') {
      const avail = Math.max(300, innerHeight - 130);
      for (let s = 6; s >= 2; s--) {
        const w = Math.floor(cw / s), r = flow(w);
        if (r.length * ROWH * s <= avail || s === 2) { scale = s; W = w; rows = r; break; }
      }
    } else {
      const need = blocks.reduce((s, b) => s + b.w + 3, HALL + 2);
      for (let s = 3; s >= 2; s--) if (Math.floor(cw / s) >= need) { scale = s; break; }
      // if even 2x does not fit on one row, shrink the scale a little (down to 1.5x) to keep one row
      if (Math.floor(cw / 2) < need && cw / need >= 1.5) scale = Math.floor(cw / need * 100) / 100;
      W = Math.floor(cw / scale); rows = flow(W);
    }
    // share out the spare width so that the rooms fill the row (3-pixel walls between rooms)
    rows.forEach((row, ri) => {
      const used = row.reduce((s, b) => s + b.w, 0) + (row.length - 1) * 3;
      const extra = Math.max(0, W - HALL - 2 - used), add = Math.floor(extra / row.length);
      let x = HALL + 2;
      row.forEach((b, k) => { b.w += add + (k === row.length - 1 ? extra - add * row.length : 0); b.x = x; b.y = ri * ROWH; b.row = ri; x += b.w + 3; });
    });
    G.scale = scale; G.W = W; G.rows = rows.length; G.H = rows.length * ROWH;
    [G.cv, G.cv2].forEach(c => { c.width = W; c.height = G.H; c.style.width = W * scale + 'px'; c.style.height = G.H * scale + 'px'; });
    G.wrap.style.height = G.H * scale + 'px';
    G.seats = [];
    blocks.forEach(b => {
      const top = b.y + WALL;
      b.door = { x: b.x + Math.round(b.w / 2), y: top + ROOM };
      if (b.type === 'boss') {
        b.deskX = b.x + Math.round((b.w - 64) / 2) - 10; b.deskY = top + 42;
        b.seat = { x: b.deskX + 26, y: b.deskY - 12 };
        b.visit = { x: b.deskX + 68, y: b.deskY - 8 };        // stands to the right of the desk, facing the user
      } else if (b.type === 'orch') {
        b.deskX = b.x + Math.round((b.w - 48) / 2); b.deskY = top + 40;      // the monitor and laptop on the left, the person on the right
        b.seat = { x: b.deskX + 30, y: b.deskY - 12 };
      } else if (b.type === 'zone') {
        const n = b.seats.length, start = b.x + Math.round((b.w - n * SEAT) / 2);
        b.seats.forEach((s, i) => {
          s.deskX = start + i * SEAT + 4; s.deskY = top + 40; s.pos = { x: s.deskX + 15, y: s.deskY - 12 }; s.block = b;
          G.seats.push(s);
        });
      } else if (b.type === 'lounge') layoutLounge(b, top, G.scale);
    });
    G.entrance = { x: HX, y: WALL - 20 };
    G.bg = null;
    retarget(G, true);
    return true;
  }

  // ---------- path finding: room → door → corridor → (vertical passage) → door → seat ----------
  const corrY = r => r * ROWH + WALL + ROOM + CORR / 2 - 15;       // top y of a person walking down the middle of the corridor
  const insideY = b => b.y + WALL + ROOM - 5 - 15;                  // just inside the door
  function where(G, x, y) {
    const fx = x + 6, fy = y + 15;
    if (fx < HALL) return { hall: true };
    const row = Math.max(0, Math.min(G.rows - 1, Math.floor(fy / ROWH))), top = row * ROWH;
    if (fy >= top + WALL + ROOM - 2) return { corr: true, row };
    const b = G.blocks.find(q => q.row === row && fx >= q.x && fx < q.x + q.w);
    return b ? { block: b, row } : { corr: true, row };
  }
  function route(G, from, goal) {
    const src = where(G, from.x, from.y), pts = [];
    if (goal.block && src.block === goal.block) return [goal.pt];
    let row = src.row;
    if (src.block) { const dx = src.block.door.x - 6; pts.push({ x: dx, y: insideY(src.block) }, { x: dx, y: corrY(row) }); }
    else if (src.corr) pts.push({ x: from.x, y: corrY(row) });
    const trow = goal.block ? goal.block.row : 0;
    if (src.hall || goal.entrance || trow !== row) {
      if (!src.hall) pts.push({ x: HX, y: corrY(row) });
      if (goal.entrance) { pts.push(goal.pt); return pts; }
      pts.push({ x: HX, y: corrY(trow) });
    }
    const dx2 = goal.block.door.x - 6;
    pts.push({ x: dx2, y: corrY(trow) }, { x: dx2, y: insideY(goal.block) }, goal.pt);
    return pts;
  }
  function setGoal(G, e, goal, instant) {
    const same = e.goal && e.goal.pt.x === goal.pt.x && e.goal.pt.y === goal.pt.y;
    e.goal = goal;
    if (instant) { e.x = goal.pt.x; e.y = goal.pt.y; e.path = []; return; }
    if (same && (e.path.length || (e.x === goal.pt.x && e.y === goal.pt.y))) return;
    e.path = route(G, e, goal);
    let len = 0, px = e.x, py = e.y;
    e.path.forEach(p => { len += Math.hypot(p.x - px, p.y - py); px = p.x; py = p.y; });
    e.speed = Math.max(5, Math.min(16, len / 56));      // based on 20 frames per second: even a long way takes about 3 seconds (about 2.5 times the old speed)
  }

  // ---------- state → person goals ----------
  function look(G, a, seat) {
    const b = seat && seat.block, letter = seat ? seat.p : (a.tag || 'E').slice(-1), codex = a.provider === 'codex';
    let alt = ['A', 'B', 'C', 'D', 'E'][(a.id.charCodeAt(3) || 0) % 5];
    if (codex) {   // the start of a Codex thread id (UUIDv7) is the time, so ids are nearly the same: use the number at the end of the seat name (astra1 → A), else the last character of the id
      const n = +((String(seat ? seat.p : a.tag || '').match(/(\d+)$/) || [])[1] || 0);
      alt = 'ABCDE'[n ? (n - 1) % 5 : (a.id.charCodeAt(a.id.length - 1) || 0) % 5];
    }
    const hair = HAIR[letter] || HAIR[alt];
    return { hair, shirt: (b && b.shirt) || SHIRTS[(a.id.charCodeAt(5) || 0) % SHIRTS.length], provider: codex ? 'codex' : 'claude' };
  }
  function retarget(G, instant) {
    const S = G.state, byId = new Map(S.agents.map(a => [a.id, a]));
    const keep = new Set(['orch']);
    const orchB = G.blocks.find(b => b.type === 'orch'), lounge = G.blocks.find(b => b.type === 'lounge');
    const bossB = G.blocks.find(b => b.type === 'boss');
    let u = G.ents.get('user');
    if (!u) { u = { key: 'user', id: null, name: t('common.you'), look: { hair: '#1a1a1a', shirt: '#bc8cff', vip: true }, x: 0, y: 0, path: [] }; G.ents.set('user', u); }
    u.where = 'sit'; setGoal(G, u, { pt: bossB.seat, block: bossB }, true);
    keep.add('user');
    let o = G.ents.get('orch');
    if (!o) { o = { key: 'orch', id: null, name: t('common.orchestrator'), look: { hair: '#6b6b7a', shirt: '#f0883e', boss: true }, x: 0, y: 0, path: [], errands: [] }; G.ents.set('orch', o); }
    o.look.provider = orchProvider(S);          // changing the session changes the orchestrator's picture too (decided again every time)
    o.working = S.orch.state === 'working'; o.home = { pt: orchB.seat, block: orchB };
    if (instant) {
      if (o.errand) { o.where = 'walk'; setGoal(G, o, { pt: bossB.visit, block: bossB, face: 'left' }, true); }
      else { o.where = 'sit'; o.goingHome = false; setGoal(G, o, o.home, true); }
    } else if (!o.errand && !o.errands.length && !o.path.length && !o.goingHome) { o.where = 'sit'; setGoal(G, o, o.home, false); }
    const seated = new Set(), resting = [];
    G.seats.forEach(s => {
      const a = s.agentId && byId.get(s.agentId);
      if (!a) return;
      seated.add(a.id);
      if (RUN(a.status)) place(G, a, s, { pt: s.pos, block: s.block }, 'sit', instant, keep);
      else resting.push([a, s]);
    });
    resting.sort((p, q) => (q[0].last_ts || 0) - (p[0].last_ts || 0));
    const put = assignLounge(G, lounge, resting, Date.now() / 1000);
    lounge.extra = resting.length - put.length;
    G.loungeNext = put.length ? Math.min(...put.map(o => o.next)) : 0;      // the moment someone's time slot changes (step assigns again then)
    put.forEach(o => place(G, o.a, o.s, { pt: { x: o.spot.x, y: o.spot.y }, block: lounge, face: o.act.dir, act: o.act, tag: o.spot.tag }, 'lounge', instant, keep));
    lounge.old = S.agents.filter(a => !seated.has(a.id) && !RUN(a.status)).length;
    for (const k of [...G.ents.keys()]) if (!keep.has(k)) G.ents.delete(k);
  }
  // tag width (css px). Measured through a function the tests inject (opts.tagWidth) or from the real text width (DOM) and remembered (measured again once the font loads)
  function tagCss(G, name) {
    let w = G.tagWc.get(name);
    if (w === undefined) {
      if (G.opts.tagWidth) w = G.opts.tagWidth(name);
      else { const e = document.createElement('div'); e.className = 'ag-tag'; e.style.visibility = 'hidden'; e.style.left = '-9999px'; e.style.top = '0px'; e.textContent = name; G.ov.appendChild(e); w = e.offsetWidth; e.remove(); }
      G.tagWc.set(name, w);
    }
    return w;
  }
  // Gives lounge spots to the resting people. The same input (person ids and time) gives the same result (nothing jumps when redrawn).
  // Each person has a time slot (SLOT_SEC, the start offset by the id hash), and for each slot a weighted draw by the hash of (id, slot) — if the drawn facility has no spot, the next candidate.
  // Having a spot means: it is free, and the person's tag (one line right below, at the real text width) does not overlap a neighboring tag, a neighboring person or other furniture.
  // The tag is not shortened; the spot is left empty instead: a long name (sol6.1-38) sits only when the seat next to it is free, and if none works there is no spot (the label "+N more").
  // Sofas are filled from the front one (upper row left → right, then the lower row): sofas drawn = sofas used (at least 1). A person who finished more than 30 minutes ago picks the sofa twice as often
  // and dozes well on it. Dozing only on a sofa. Failed, ended etc. sit on the sofa with eyes open as before (they take their spot first).
  function assignLounge(G, L, resting, nowS) {
    const sec = G.demo ? DEMO_SLOT_SEC : SLOT_SEC, sc = G.scale, th = L.th, put = [], placed = [], taken = new Set(), bySpot = new Map();
    const tagR = (sp, w) => { const ax = sp.x + sp.tag.dx, l = sp.tag.al === 'l' ? ax : sp.tag.al === 'r' ? ax - w : ax - w / 2, t = sp.y + sp.tag.dy; return { l, r: l + w, t, b: t + th }; };
    const bodyR = sp => ({ l: sp.x, r: sp.x + 12, t: sp.y, b: sp.y + (sp.stand ? 16 : 12) });
    const ov = (p, q) => p.l < q.r && q.l < p.r && p.t < q.b && q.t < p.b;
    const fits = (sp, w) => {
      const T = tagR(sp, w);
      if (T.l < sp.lim[0] - 0.01 || T.r > sp.lim[1] + 0.01) return false;
      const B = bodyR(sp);
      return placed.every(q => !ov(T, q.T) && !ov(T, q.B) && !ov(B, q.T));
    };
    const order = resting.filter(([a]) => a.status !== 'done').concat(resting.filter(([a]) => a.status === 'done'));
    order.forEach(([a, s]) => {
      const done = a.status === 'done', ph = hash32(a.id + '#ph') % sec, k = Math.floor((nowS + ph) / sec), id = a.id + '|' + k;
      const long = done && a.last_ts > 0 && nowS - a.last_ts > LONG_REST_SEC;
      const prefs = done
        ? ACT_KINDS.map(kind => [-Math.log(rnd(id + '|' + kind)) / (ACT_WEIGHT[kind] * (kind === 'sofa' && long ? 2 : 1)), kind]).sort((p, q) => p[0] - q[0]).map(p => p[1])
        : ['sofa'].concat(ACT_KINDS.filter(kind => kind !== 'sofa'));
      const w = tagCss(G, a.tag || a.title || '') / sc + 0.25;
      for (const kind of prefs) {
        const pool = L.byKind[kind].filter(c => !taken.has(c) && fits(c, w));
        if (!pool.length) continue;
        const spot = kind === 'sofa' ? pool[0] : pool.reduce((best, c) => rnd(a.id + '|' + c.key) < rnd(a.id + '|' + best.key) ? c : best);   // the sofa from the front seat; the others keep liking the same spot even when the slot changes
        taken.add(spot); placed.push({ T: tagR(spot, w), B: bodyR(spot) });
        const u = rnd(id + '|mode');   // on a sofa: doze / just sit / chat with the neighbor
        const mode = !done || kind !== 'sofa' ? 'sit' : u < (long ? 0.65 : 0.3) ? 'doze' : u < (long ? 0.85 : 0.75) ? 'sit' : 'chat';
        const o = { a, s, spot, next: (k + 1) * sec - ph, act: { kind, spot, mode, dir: spot.dir, ph: hash32(a.id + '#p') % 80, speak: 0 } };
        bySpot.set(spot, o); put.push(o);
        break;
      }
    });
    L.sofaN = Math.max(1, ...put.filter(o => o.act.kind === 'sofa').map(o => o.spot.site + 1));
    // Chat only when there is a person (not dozing, status done) in the seat to the right: the two look at each other and speak in turn. With no partner, just sit
    const sofaAt = new Map(L.byKind.sofa.map(sp => [sp.site + '.' + sp.k, sp]));
    L.byKind.sofa.forEach(sp => {
      const o = bySpot.get(sp);
      if (!o || o.act.mode !== 'chat' || o.paired) return;
      const nb = bySpot.get(sofaAt.get(sp.site + '.' + (sp.k + 1)));
      if (nb && !nb.paired && nb.a.status === 'done' && nb.act.mode !== 'doze') { o.paired = nb.paired = true; o.act.dir = 'right'; nb.act.dir = 'left'; nb.act.mode = 'chat'; nb.act.speak = 1; }
      else o.act.mode = 'sit';
    });
    return put;
  }
  function place(G, a, seat, goal, pose, instant, keep) {
    let e = G.ents.get(a.id);
    const fresh = !e;
    if (!e) { e = { key: a.id, id: a.id, x: G.entrance.x, y: G.entrance.y, path: [] }; G.ents.set(a.id, e); }
    Object.assign(e, { name: a.tag || a.title, look: look(G, a, seat), where: pose, seat, agent: a, home: goal, homePose: pose, act: goal.act || null });
    // `instant` is a new layout of the rooms (the desks moved): someone who was already here is put at the new place at once, unless they are on the way somewhere, who walk on
    // to the new goal from where they are. Someone who has just come in always starts at the entrance and walks in; only the first picture (or the first after a server restart) has everybody in place
    const snap = fresh ? G.lastIdx === null : instant && !e.path.length;
    if (e.visit && Date.now() < e.visit.until) { e.where = 'walk'; setGoal(G, e, e.visit, false); }
    else { e.visit = null; setGoal(G, e, goal, snap); }
    keep.add(a.id);
  }

  // ---------- new event → speech bubble · flying paper ----------
  function bubble(G, key, text, kind = '') {
    if (!text) return;
    G.bubbles = G.bubbles.filter(b => { if (b.key === key) { b.el.remove(); return false; } return true; });
    const el = document.createElement('div');
    el.className = 'ag-bubble ' + kind;
    el.textContent = text;
    el.style.visibility = 'hidden';
    G.ov2.appendChild(el);
    G.bubbles.push({ key, el, until: Date.now() + (kind === 'orch' || kind === 'user' ? 9000 : 8000) });
    while (G.bubbles.length > 4) G.bubbles.shift().el.remove();
    G.lastBubble.set(key, Date.now());
  }
  function paper(G, fromKey, toKey, kind) {
    const f = G.ents.get(fromKey), t = G.ents.get(toKey);
    if (!f || !t) return;
    G.papers.push({ f: { x: f.x + 6, y: f.y }, toKey, kind, t0: Date.now(), dur: 1300 });
  }
  // Titles the server makes (the default "Message" when the sender gave none, the run title of a claude -p child) come with title_i18n { key, params } and title_is_default.
  // A response without these fields (an old server) has the Korean text only: the default title is recognised by its text.
  // A time (epoch seconds) as the page words it: the time of day, with the date when it is not today
  const clock = ts => {
    const d = new Date(ts * 1000), tm = I18N.date(ts, 'time');
    return d.toDateString() === new Date().toDateString() ? tm : t('time.dayTime', { date: t('time.monthDay', { m: d.getMonth() + 1, d: d.getDate(), mon: I18N.date(ts, 'monthShort') }), time: tm });
  };
  const timeWords = p => { const o = Object.assign({}, p); if ('at' in o) o.at = o.at ? clock(o.at) : '-'; return o; };       // `at` (epoch seconds) is the reset time of a usage limit
  const madeTitle = e => (e.title_i18n && e.title_i18n.key && I18N.has(e.title_i18n.key) ? I18N.t(e.title_i18n.key, timeWords(e.title_i18n.params)) : e.title);
  const ownTitle = e => (e.title_is_default != null ? !e.title_is_default : e.title !== '메시지') && madeTitle(e);   // i18n-ok: the old default title
  // The first question of a choice request: questions[0].question, else (old response) the first line of the text without its **header**
  const askText = e => {
    const q = Array.isArray(e.questions) && e.questions[0] && typeof e.questions[0].question === 'string' ? e.questions[0].question : '';
    return q || String(e.text || '').split('\n')[0].replace(/^\*\*[^*]+\*\*\s*/, '');
  };
  function onEvents(G) {
    const S = G.state, feed = S.feed || [];
    const maxIdx = feed.length ? feed[feed.length - 1].idx : -1;
    if (G.lastIdx === null) {           // when first opened, do not replay the past
      G.lastIdx = maxIdx;
      if (S.orch.last_say) bubble(G, 'orch', firstSentence(S.orch.last_say), 'orch');
      return;
    }
    const name = id => { const a = S.agents.find(x => x.id === id); return a ? (a.tag || a.title) : String(id || '').slice(0, 6); };
    feed.filter(e => e.idx > G.lastIdx).forEach(e => {
      if (e.kind === 'orch_msg') {
        const sum = ownTitle(e) || firstSentence(e.text, 40);
        bubble(G, 'orch', `@${name(e.agent)} ${sum}`, 'orch');
        paper(G, 'orch', e.agent, 'mail');
      } else if (e.kind === 'spawn') {
        bubble(G, 'orch', e.agent ? t('office.bubble.spawn', { name: name(e.agent), title: firstSentence(madeTitle(e), 40) }) : t('office.bubble.spawnAnon', { title: firstSentence(madeTitle(e), 40) }), 'orch');
      } else if (e.kind === 'handback') {
        bubble(G, e.agent, firstSentence(e.text), 'rep');
        paper(G, e.agent, 'orch', 'doc');
      } else if (e.kind === 'xread') {             // cross review: a document from the author → to the reader
        paper(G, e.from, e.to, 'doc');
        if (Date.now() - (G.lastBubble.get(e.to) || 0) > 15000) bubble(G, e.to, t('office.bubble.xread', { from: name(e.from), doc: e.title }), 'peer');
      } else if (e.kind === 'agent_msg') {         // a message an agent sent
        const sum = ownTitle(e) || firstSentence(e.text, 40);
        const to = e.peer || 'orch';
        bubble(G, e.from, `@${to === 'orch' ? t('common.orchestrator') : name(to)} ${sum}`, 'peer');
        const me = G.ents.get(e.from), you = G.ents.get(to);
        if (me && you && to !== 'orch' && you.goal && me.home) {
          // walks to the other's seat, delivers it, and returns to its own seat 12 seconds later
          me.visit = { pt: { x: you.goal.pt.x + 14, y: you.goal.pt.y + 2 }, block: you.goal.block, face: 'left', until: Date.now() + 12000 };   // beside the other's desk, on the right
          me.where = 'walk';
          setGoal(G, me, me.visit, false);
          setTimeout(() => paper(G, e.from, to, 'mail'), 2500);
        } else paper(G, e.from, to, 'mail');
      } else if (e.kind === 'orch_say') {          // report: the orchestrator goes to the user's office and speaks
        const o = G.ents.get('orch'), txt = firstSentence(e.text);
        const pend = o.errands.find(x => x.kind === 'report');
        if (pend) pend.text = txt;                                  // a report that arrives on the way is replaced by the latest one
        else if (o.errand && o.errand.kind === 'report' && o.errand.shownAt) { o.errand.shownAt = Date.now(); bubble(G, 'orch', txt, 'orch'); }
        else o.errands.push({ kind: 'report', text: txt });
      } else if (e.kind === 'orch_ask') {          // choice question: the orchestrator goes to the user and asks
        G.ents.get('orch').errands.push({ kind: 'ask', text: t('office.errand.ask', { text: firstSentence(askText(e), 48) }) });
      } else if (e.kind === 'user_answer') {       // the answer the user chose: the orchestrator goes and listens
        G.userCalling = true;
        G.ents.get('orch').errands.push({ kind: 'listen', text: t('office.errand.answer', { text: firstSentence(String(e.text || '').replace(/\*\*[^*]+\*\*\s*/g, '').replace(/\n/g, ' / '), 50) }) });
      } else if (e.kind === 'user_say') {          // request: the user calls and the orchestrator goes and listens
        G.userCalling = true;
        G.ents.get('orch').errands.push({ kind: 'listen', text: firstSentence(e.text) });
      } else if (e.kind === 'sys' && e.sys && e.sys.code === 'limit') {      // the orchestrator hit a usage limit: it says so from its desk
        bubble(G, 'orch', madeTitle(e), 'orch');
      }
    });
    G.lastIdx = maxIdx;
    S.agents.forEach(a => {                 // an agent speaks at most once per 25 seconds
      const c = a.current;
      if (!c || !RUN(a.status) || !G.ents.has(a.id)) return;
      const seen = G.lastAct.get(a.id);
      G.lastAct.set(a.id, c.ts);
      if (seen === undefined || seen === c.ts) return;
      if (c.kind === 'text' && Date.now() - (G.lastBubble.get(a.id) || 0) > 25000) bubble(G, a.id, firstSentence(c.text));
    });
  }

  function update(G, state, debate) {
    // If the server has restarted the event numbers may differ: treat it as if just opened (do not replay past events)
    if (state.boot && G.boot && state.boot !== G.boot && !G.demo) {
      G.lastIdx = null;
      G.bubbles.forEach(b => b.el.remove()); G.bubbles = []; G.papers = [];
      const o = G.ents.get('orch'); if (o) { o.errands = []; o.errand = null; }
      G.userCalling = false;
    }
    if (state.boot) G.boot = state.boot;
    G.state = state; G.debate = debate || (state.debates || []).find(d => d.current) || null;
    if (!relayout(G)) retarget(G, false);
    onEvents(G);
    buildLabels(G);
    placeBubbles(G);
  }

  // ---------- text layer (tags · boards) ----------
  // The stage line has to fit the board. Below scale 2 a long wording gets fewer pixels than it needs, so it steps down in size (the board's ellipsis is the last resort).
  function fitStage(el) {
    const st = el.lastElementChild, lim = parseFloat(el.style.maxWidth);   // the .st span (last child of the board)
    if (!st || !lim) return el;
    st.style.fontSize = '';
    for (const px of [10, 9]) { if (!(st.getBoundingClientRect().width > lim + .5)) break; st.style.fontSize = px + 'px'; }
    return el;
  }
  // The orchestrator waits on a usage limit (orch.state 'limit_wait', with resets_at and auto): the sentence for mouse-over, and the short words for the sign on the wall
  const limitText = o => t('event.sys.limit' + (o.resets_at ? '' : '.noTime') + (o.auto ? '.auto' : ''), { at: o.resets_at ? clock(o.resets_at) : '' });
  const limitSign = o => t('office.sign.orchLimit' + (o.resets_at ? (o.auto ? '.auto' : '') : '.noTime'), { time: o.resets_at ? clock(o.resets_at) : '' });
  function buildLabels(G) {
    const s = G.scale, S = G.state;
    const prevTags = G.ltags, keepEls = new Set([...prevTags.values()].map(t => t.el));   // lounge tags reuse their elements (if the people, names and descriptions are the same, only the position changes)
    G.labels.forEach(l => { if (!keepEls.has(l)) l.remove(); }); G.labels = []; G.ltags = new Map(); G.hlTags = new Map();
    const add = (html, x, y, cls, w, onclick, title) => {
      const el = document.createElement('div'); el.className = cls; el.innerHTML = html;
      el.style.left = x * s + 'px'; el.style.top = y * s + 'px';
      if (w) el.style[cls.includes('two') ? 'width' : 'maxWidth'] = w * s + 'px';
      if (title) el.title = title;
      if (onclick) el.onclick = onclick; else el.style.pointerEvents = 'none';
      G.ov.appendChild(el); G.labels.push(el);
      return el;
    };
    const statusCol = st => ({ running: C.ok, stalled: C.amber, interrupted: C.amber, done: C.work, failed: C.bad, killed: C.bad }[st] || '#8b8fa8');
    const codexNote = a => (a.provider === 'codex' ? ` · Codex ${a.model || ''}`.trimEnd() : '');   // only in the text shown on mouse-over
    G.blocks.forEach(b => {
      if (b.type === 'orch') {
        const cxo = orchProvider(S) === 'codex';   // Codex orchestrator (a standalone Codex session): dot ◆, Codex on the sign
        const lw = S.orch.state === 'limit_wait', lt = lw ? limitText(S.orch) : '';   // resting on a usage limit: an amber dot, the reset time on the sign, the whole sentence on mouse-over
        G.hlTags.set('orch', add(`<i${cxo ? ' class="cx"' : ''} style="background:${lw ? C.amber : S.orch.state === 'working' ? C.ok : '#f0883e'}"></i>${esc(t('office.tag.orch'))}`, b.deskX + 24, b.deskY + 15, 'ag-tag', 72, null, [cxo ? `Codex ${S.orch.model || ''}`.trim() : '', lt].filter(Boolean).join(' — ')));
        add(esc(lw ? limitSign(S.orch) : t(cxo ? 'office.sign.orchCodex' : 'office.sign.orch')), b.x + 4, b.y + WALL + 2, 'ag-sign', b.w - 8, null, lt);
      } else if (b.type === 'boss') {
        add(esc(t('office.sign.user')), b.x + 40, b.y + WALL + 2, 'ag-sign', b.w - 44);
        add(`<i style="background:#bc8cff"></i>${esc(t('office.tag.user'))}`, b.deskX + 32, b.deskY + 16, 'ag-tag', 60);
      } else if (b.type === 'zone') {
        const tp = b.topic, bx = b.x + Math.round((b.w - 88) / 2);   // (t is the translation function: do not shadow it here)
        const m = tp ? (tp.title || '').match(/^(T\d+)\s+(.*)$/) : null;
        const title = tp ? (m ? `<b>${esc(m[1])}</b> ${esc(m[2])}` : esc(tp.title)) : `<b>${esc(t('office.board.other'))}</b>`;
        const stage = tp ? stageText(tp, S) : t('office.board.working', { count: b.seats.length });
        fitStage(add(`${title}<br><span class="st">${esc(stage)}</span>`, bx + 4, b.y + 7, 'ag-board', 88 - 10 - (tp ? roundKeys(tp).length : 2) * 6, null, tp ? tp.title : ''));
        b.seats.forEach(st => {
          const a = st.agentId && S.agents.find(x => x.id === st.agentId);
          // The seat letter (A·B·C, astra1) goes on the nameplate on the front of the desk; the first line of the tag is status · model (opus5.5, sol6.1), the next line (at most two lines) is the role.
          // If the name is already a model name (sol6.1-2, opus5.5-13), that name is used and there is no nameplate. The role name (T1-A) shows on mouse-over.
          const ms = a ? modelShort(a.model) : '', same = a && st.p === a.tag && (!ms || st.p.startsWith(ms));
          if (!same) add(esc(st.p), st.deskX + 15, st.deskY + 5, 'ag-plate', 28, null, st.p);
          const html = a
            ? `<span class="nm"><i${a.provider === 'codex' ? ' class="cx"' : ''} style="background:${statusCol(a.status)}"></i>${esc(same ? st.p : ms || a.tag || a.title)}</span><span class="rl">${esc(st.role || a.title)}</span>`
            : `<span class="nm">${esc(t('office.tag.empty'))}</span><span class="rl">${esc(st.role || '')}</span>`;
          const tag = add(html, st.deskX + 15, st.deskY + 14, 'ag-tag two' + (a ? '' : ' dim'), SEAT - 1, a ? () => G.opts.onAgent && G.opts.onAgent(a.id) : null, a ? `${a.tag || ''} ${a.title} — ${st.role || ''}${codexNote(a)}` : st.role);
          if (a) G.hlTags.set(a.id, tag);
        });
      } else if (b.type === 'lounge') {
        const more = (b.extra || 0), old = b.old || 0;
        const parts = [t('office.sign.lounge')];
        if (b.sofaN > 1) parts.push(t('office.sign.lounge.sofas', { count: b.sofaN }));
        if (more) parts.push(t('office.sign.lounge.more', { count: more }));
        if (old) parts.push(t('office.sign.lounge.earlier', { count: old }));
        add(esc(parts.join(' · ')), b.x + 4, b.y + WALL - 12, 'ag-sign', b.w - 8);   // on the wall above the room so that it does not overlap the facility strip
      }
    });
    for (const e of G.ents.values()) {
      if (e.where === 'lounge' && e.goal) {   // The tag is one line right below the person. assignLounge guarantees that tags do not overlap at any spot. It is shown at the same width used for the assignment, so there is no width cap (a long name is not cut off)
        const tg = e.goal.tag || { dx: 6, dy: 22 }, title = e.agent && ((e.agent.title || '') + codexNote(e.agent) + holdNote(e.agent)), old = prevTags.get(e.id);
        const t = { e, dx: tg.dx, dy: tg.dy, al: tg.al, w: tagCss(G, e.name) / s, name: e.name, title, el: null, l: 0, t: 0 }, xy = loungeTagXY(G, t);
        if (old && old.name === e.name && old.title === title) {          // same person, same text: keep the element and only change its position
          t.el = old.el; prevTags.delete(e.id); G.labels.push(t.el);
          t.el.style.left = xy[0] * s + 'px'; t.el.style.top = xy[1] * s + 'px';
        } else t.el = add(esc(e.name), xy[0], xy[1], 'ag-tag lg', 0, () => G.opts.onAgent && G.opts.onAgent(e.id), title);
        t.l = xy[0] * s; t.t = xy[1] * s;
        G.ltags.set(e.id, t); G.hlTags.set(e.id, t.el);
      }
    }
    prevTags.forEach(t => t.el.remove());                              // the tags of people who are no longer resting
    applyHl(G);                                                        // restore the highlight on the rebuilt tags too
  }
  // A lounge tag sticks to its person. While walking it follows the person from the default place right below them (centered, 3 pixels below the feet), and on arrival it becomes the tag position of the assigned spot
  // (anchor al: for 'l'·'r' it is shifted by half the text width). Either way it is confined to the visible area (the strip · the /game canvas): it is not cut off at the sides or the bottom while passing the corridor or the entrance either.
  // What a resting agent that is not over says on mouse-over: when its usage limit resets, or why it stopped
  const holdNote = a => {
    if (a.status === 'interrupted' && a.reason === 'limit' && a.resets_at) return ' — ' + t('event.sys.limit', { at: clock(a.resets_at) });
    const k = a.reason ? 'status.why.' + a.status + '.' + a.reason : '';
    return k && I18N.has(k) ? ' — ' + t(k) : a.status === 'unknown' && I18N.has('status.why.unknown') ? ' — ' + t('status.why.unknown') : '';
  };
  const TAG_CSS_H = 16;   // tag height (css px, measured 16)
  const loungeTagXY = (G, t) => {
    const e = t.e, mv = !!(e.path && e.path.length), dx = mv ? 6 : t.dx, dy = mv ? 19 : t.dy, al = mv ? null : t.al, hw = t.w / 2, h = TAG_CSS_H / G.scale;
    let x = e.x + dx + (al === 'l' ? hw : al === 'r' ? -hw : 0), y = e.y + dy;
    x = hw * 2 >= G.W ? G.W / 2 : Math.max(hw, Math.min(G.W - hw, x));
    y = Math.max(0, Math.min(G.H - h, y));
    return [x, y];
  };
  // move the labels without rebuilding them (only the tags that moved get a style write)
  function moveLoungeTags(G) {
    const s = G.scale;
    for (const t of G.ltags.values()) {
      const xy = loungeTagXY(G, t), l = xy[0] * s, tp = xy[1] * s;
      if (Math.abs(l - t.l) > 0.01 || Math.abs(tp - t.t) > 0.01) { t.el.style.left = l + 'px'; t.el.style.top = tp + 'px'; t.l = l; t.t = tp; }
    }
  }
  // A topic with no round folders (a room, a flat review whose cells carry no round) has one result per seat and no next round to wait for; board.js keeps its own copy of these two.
  const noRounds = tp => !!tp.room || tp.kind === 'flat';
  const roundKeys = tp => tp.room === 'members' ? [] : tp.kind === 'flat' ? [null] : tp.room ? [1] : Array.from({ length: Math.max(2, ...tp.rounds) }, (_, i) => i + 1);
  function stageText(tp, S) {   // (tp, not t: t is the translation function)
    const cells = tp.rows.flatMap(r => r.cells);
    const working = tp.rows.some(r => r.agents.some(id => { const a = S.agents.find(x => x.id === id); return a && RUN(a.status); }));
    if (tp.final && tp.final.exists) return t('office.board.final');
    if (tp.room === 'members') return working ? t('office.board.members', { count: tp.rows.length }) : t('office.board.membersEnded');
    if (!cells.some(c => c.agent) && !cells.some(c => c.state === 'done')) return tp.deps ? t('office.board.after', { deps: tp.deps }) : t('office.board.pending');
    if (noRounds(tp)) {                                 // submitted, still being written, or stopped; complete once every seat is in and nobody works
      const rc = tp.rows.map(row => row.cells[0]).filter(Boolean);
      const done = rc.filter(c => c.state === 'done').length;
      if (rc.some(c => ['writing', 'draft', 'paused', 'unknown'].includes(c.state)) || (done && done < rc.length)) return t('office.board.room', { done, total: rc.length });
      return !done ? t('office.board.pending') : working ? t('office.board.roomDone', { total: rc.length }) : t('office.board.complete');
    }
    for (const r of tp.rounds) {
      const rc = tp.rows.map(row => row.cells.find(c => c.round === r)).filter(Boolean);
      if (rc.some(c => ['writing', 'draft', 'paused', 'unknown'].includes(c.state))) {
        const done = rc.filter(c => c.state === 'done').length;
        return t('office.board.round', { round: r, done, total: rc.length });
      }
    }
    const last = Math.max(0, ...tp.rounds.filter(r => tp.rows.some(row => row.cells.find(c => c.round === r && c.state === 'done'))));
    return last ? t('office.board.done', { round: last }) : t('office.board.pending');
  }

  // ---------- movement ----------
  function step(G) {
    const o = G.ents.get('orch'), bossB = G.blocks.find(b => b.type === 'boss'), t = Date.now();
    // lounge: when someone's time slot changes, assign again (here, because no update comes while the state stays the same). They walk to the new spot
    if (G.loungeNext && t / 1000 >= G.loungeNext) { retarget(G, false); buildLabels(G); placeBubbles(G); }
    if (o && bossB && o.errands) {
      if (!o.errand && o.errands.length) {                       // next errand: go to the user's office
        o.errand = o.errands.shift(); o.goingHome = false; o.where = 'walk';
        setGoal(G, o, { pt: bossB.visit, block: bossB, face: 'left' }, false);
      }
      if (o.errand && !o.path.length && !o.errand.shownAt) {       // arrived: listen to the request or report
        o.errand.shownAt = t;
        if (o.errand.kind === 'listen') { G.userCalling = false; bubble(G, 'user', o.errand.text, 'user'); }
        else bubble(G, 'orch', o.errand.text, 'orch');
      }
      if (o.errand && o.errand.shownAt && t - o.errand.shownAt > ERRAND_STAY) {
        o.errand = null;
        if (!o.errands.length && o.home) { o.goingHome = true; setGoal(G, o, o.home, false); }
      }
      if (o.goingHome && !o.path.length) { o.goingHome = false; o.where = 'sit'; }
    }
    for (const e of G.ents.values()) {
      if (e.visit && Date.now() > e.visit.until) { e.visit = null; e.where = e.homePose; if (e.home) setGoal(G, e, e.home, false); }
      if (!e.path || !e.path.length) { e.moving = false; e.dir = (e.goal && e.goal.face) || 'down'; continue; }
      const p = e.path[0], dx = p.x - e.x, dy = p.y - e.y, d = Math.hypot(dx, dy), sp = e.speed || 4;
      if (d > 0.5) e.dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up');
      if (d <= sp) { e.x = p.x; e.y = p.y; e.path.shift(); }
      else { e.x += dx / d * sp; e.y += dy / d * sp; }
      e.moving = e.path.length > 0;
      if (!e.moving) e.dir = (e.goal && e.goal.face) || 'down';
    }
    if (G.ltags.size) moveLoungeTags(G);
    const now = Date.now();
    G.bubbles = G.bubbles.filter(b => {
      if (now > b.until + 700) { b.el.remove(); return false; }
      if (now > b.until) b.el.style.opacity = '0';
      return true;
    });
    G.papers = G.papers.filter(p => now - p.t0 < p.dur);
  }

  // ---------- drawing ----------
  function drawBg(G) {
    const cv = document.createElement('canvas'); cv.width = G.W; cv.height = G.H;
    const x = cv.getContext('2d'), R = (c, a, b, w, h) => { x.fillStyle = c; x.fillRect(a, b, w, h); };
    for (let r = 0; r < G.rows; r++) {
      const y0 = r * ROWH, cy0 = y0 + WALL + ROOM;
      R(C.wall, 0, y0, G.W, WALL); R(C.wall2, 0, y0 + WALL - 6, G.W, 4); R(C.base, 0, y0 + WALL - 2, G.W, 2);
      for (let wx = HALL + 14; wx < G.W - 20; wx += 72) {        // windows
        R(C.win, wx, y0 + 6, 22, 18); R(C.glass, wx + 2, y0 + 8, 8, 14); R(C.glass, wx + 12, y0 + 8, 8, 14);
        R(C.glass2, wx + 3, y0 + 9, 2, 5); R(C.glass2, wx + 13, y0 + 9, 2, 5);
      }
      for (let fy = y0 + WALL; fy < cy0; fy += 6) {             // room floor (wooden)
        R((fy / 6) % 2 ? C.floor : C.floor2, HALL, fy, G.W - HALL, 6);
        for (let fx = HALL + ((fy / 6) % 3) * 13; fx < G.W; fx += 40) R(C.seam, fx, fy, 1, 6);
      }
      for (let fx = 0; fx < G.W; fx += 8) for (let fy = cy0; fy < y0 + ROWH; fy += 8)   // corridor (stone tiles)
        R(((fx + fy) / 8) % 2 ? C.corr : C.corr2, fx, fy, 8, Math.min(8, y0 + ROWH - fy));
      R(C.corrLine, 0, y0 + ROWH - 1, G.W, 1);
    }
    // the vertical passage (joins the rows) and the entrance
    for (let fy = 0; fy < G.H; fy += 8) for (let fx = 0; fx < HALL; fx += 8) R(((fx + fy) / 8) % 2 ? C.corr : C.corr2, fx, fy, Math.min(8, HALL - fx), 8);
    R(C.base, HALL, 0, 2, G.H);                                            // wall between the passage and the rooms
    for (let r = 0; r < G.rows; r++) R(C.corr, HALL, r * ROWH + WALL + ROOM, 2, CORR);   // the corridors and the passage are joined
    R(C.door2, 1, 2, HALL - 2, WALL - 4); R(C.door, 3, 4, HALL - 6, WALL - 6); R('#e3b341', HALL - 6, WALL / 2, 2, 2);
    G.blocks.forEach(b => {
      const top = b.y + WALL, bot = top + ROOM;
      // room floor: orchestrator room = rug, boss room = red carpet, lounge = green carpet, debate and work rooms = beige carpet tiles
      if (b.type === 'orch') R(orchProvider(G.state) === 'codex' ? C.orchRugCodex : C.orchRug, b.x + 2, top + 2, b.w - 4, ROOM - 6);
      if (b.type === 'boss') {
        R('#7a1f2b', b.x + 2, top + 2, b.w - 4, ROOM - 6);                                   // red carpet
        R('#e3b341', b.x + 5, top + 5, b.w - 10, 1); R('#e3b341', b.x + 5, top + ROOM - 8, b.w - 10, 1);
        R('#e3b341', b.x + 5, top + 5, 1, ROOM - 12); R('#e3b341', b.x + b.w - 6, top + 5, 1, ROOM - 12);
        const bx = b.x + 9, by = top + 7;                                                      // bookshelf
        R('#5e3a1f', bx, by, 26, 22); R('#3a2412', bx + 2, by + 2, 22, 8); R('#3a2412', bx + 2, by + 12, 22, 8);
        ['#d65a5a', '#5aa9ff', '#3fb56b', '#e3b341', '#9b6cf0', '#f0883e', '#1abcb0'].forEach((c, i) => { R(c, bx + 3 + i * 3, by + 3, 2, 7); R(c, bx + 4 + ((i * 5) % 20), by + 13, 2, 7); });
        R('#6b1f2a', b.seat.x - 2, b.deskY - 22, 16, 18); R('#8e2a38', b.seat.x, b.deskY - 20, 12, 3);  // tall leather chair
        const sx = b.x + b.w - 36, sy = top + 17;                                              // leather sofa (upper right, facing front) and a low table
        R('#4a2c17', sx, sy - 10, 26, 9); R('#5e3a1f', sx + 1, sy - 9, 24, 2);                 // backrest
        R('#8a5a34', sx, sy - 1, 26, 6); R('#a06a3e', sx + 1, sy - 1, 11, 2); R('#a06a3e', sx + 14, sy - 1, 11, 2);   // two cushions
        R('#4a2c17', sx, sy + 5, 26, 2); R('#4a2c17', sx - 3, sy - 6, 4, 13); R('#4a2c17', sx + 25, sy - 6, 4, 13);   // front panel · armrests
        const cx0 = sx + 4, cy0 = sy + 11;
        R('#3a2412', cx0, cy0, 18, 4); R('#e3b341', cx0, cy0, 18, 1); R('#2a180c', cx0 + 1, cy0 + 4, 2, 2); R('#2a180c', cx0 + 15, cy0 + 4, 2, 2);
        R('#f4f4fa', cx0 + 4, cy0 - 2, 3, 2); R('#c9ccd6', cx0 + 7, cy0 - 1, 1, 1);             // teacup
        R('#d65a5a', cx0 + 12, cy0 - 3, 2, 2); R('#3fb56b', cx0 + 12, cy0 - 1, 2, 1);           // small flower
        const tx = b.x + b.w - 32;                                                             // trophy cabinet (lower right)
        R('#5e3a1f', tx, top + 56, 12, 16); R('#e3b341', tx + 3, top + 48, 6, 5); R('#e3b341', tx + 5, top + 53, 2, 3); R('#e3b341', tx + 3, top + 55, 6, 1);
        R('#fff4c2', tx + 4, top + 49, 1, 2);
        R(C.pot, b.x + 8, top + 58, 8, 8); R(C.leaf, b.x + 7, top + 48, 10, 10); R(C.leaf2, b.x + 10, top + 44, 4, 6);  // two plants
        R(C.pot, b.x + b.w - 16, top + 58, 8, 8); R(C.leaf, b.x + b.w - 17, top + 48, 10, 10); R(C.leaf2, b.x + b.w - 14, top + 44, 4, 6);
        const px = b.x + Math.round(b.w / 2) - 17;                                              // gilt-framed picture on the wall
        R('#e3b341', px, b.y + 5, 34, 24); R('#2f5a8a', px + 2, b.y + 7, 30, 20); R('#3fa85a', px + 2, b.y + 19, 30, 8);
        R('#6b8fbf', px + 8, b.y + 12, 8, 7); R('#f5f0d0', px + 24, b.y + 9, 4, 4);
      }
      if (b.type === 'lounge') R('#2f5a46', b.x + 2, top + 2, b.w - 4, ROOM - 6);
      if (b.type === 'zone') {                                         // work room: light beige carpet tiles (20 cells, a seam you can hardly see)
        for (let ty = top; ty < bot; ty += 20) for (let tx = b.x; tx < b.x + b.w; tx += 20) {
          const w = Math.min(20, b.x + b.w - tx), h = Math.min(20, bot - ty);
          R(((tx - b.x) / 20 + (ty - top) / 20) % 2 ? C.tile2 : C.tile, tx, ty, w, h);
          R(C.grout, tx, ty, w, 1); R(C.grout, tx, ty, 1, h);
        }
      }
      R(C.base, b.x + b.w, top - 4, 3, ROOM + 4);                      // right partition wall
      R(C.wall2, b.x + b.w, top - 4, 3, 2);
      if (b.type === 'zone') {                                         // board
        const bx = b.x + Math.round((b.w - 88) / 2);
        R(C.boardF, bx, b.y + 4, 88, 28); R(C.board, bx + 2, b.y + 6, 84, 24);
        b.seats.forEach(s => {                                         // office chair: round back, seat, post
          const cx0 = s.deskX + 16, cy0 = s.deskY - 17;
          R(C.chairRim, cx0 + 1, cy0, 8, 1); R(C.chairBack, cx0, cy0 + 1, 10, 7); R(C.chairRim, cx0 + 1, cy0 + 1, 8, 1);
          R(C.chairSeat, cx0 - 1, cy0 + 8, 12, 3); R(C.chairBack, cx0 + 4, cy0 + 11, 2, 2);
        });
      }
      if (b.type === 'orch') {
        R(C.chair, b.deskX + 31, b.deskY - 17, 12, 13);
        R(C.pot, b.x + 6, top + 50, 8, 8); R(C.leaf, b.x + 5, top + 40, 10, 10); R(C.leaf2, b.x + 8, top + 36, 4, 6);
        R('#d9d9e6', b.x + b.w - 20, b.y + 8, 13, 13); R(C.ink, b.x + b.w - 19, b.y + 9, 11, 11); R('#f4f4fa', b.x + b.w - 18, b.y + 10, 9, 9);  // wall clock face
      }
      if (b.type === 'lounge') {
        const cf = b.coffee;                                                // coffee machine: against the back wall (12×22)
        R('#444a5c', cf.x, cf.y, 12, 22); R('#2b2f3c', cf.x + 2, cf.y + 3, 8, 6); R(C.bad, cf.x + 8, cf.y + 1, 2, 1); LOUNGE_ART.coffeeSpot(R, cf.x, cf.y);
        R(C.pot, b.x + b.w - 12, top + 64, 8, 8); R(C.leaf, b.x + b.w - 13, top + 54, 10, 10);   // plant: lower right corner
        b.decor.forEach(f => f(R));                                          // the static furniture of the facilities (tea table · games · treadmills · dumbbell rack). The sofas, screens, belts, steam and cups are drawn by draw every frame
      }
    });
    G.bg = cv;
  }
  function draw(G) {
    const x = G.cx, f = Math.floor(G.frame / 2);   // typing, walking and blinking keep the 10 frames per second beat as before
    if (!G.bg) drawBg(G);
    x.imageSmoothingEnabled = false;
    x.drawImage(G.bg, 0, 0);
    const R = (c, a, b, w, h) => { x.fillStyle = c; x.fillRect(Math.round(a), Math.round(b), w, h); };
    // wall clock hands
    const ob = G.blocks.find(b => b.type === 'orch');
    if (ob) {
      const d = new Date(), cx0 = ob.x + ob.w - 14, cy0 = ob.y + 14;
      const hand = (ang, len, col) => { for (let i = 0; i <= len; i++) R(col, cx0 + Math.round(Math.sin(ang) * i), cy0 - Math.round(Math.cos(ang) * i), 1, 1); };
      hand((d.getHours() % 12 + d.getMinutes() / 60) / 12 * 2 * Math.PI, 2, C.ink);
      hand(d.getMinutes() / 60 * 2 * Math.PI, 3, '#d63a3a');
    }
    // the round cells on the board
    G.blocks.forEach(b => {
      if (b.type !== 'zone' || !b.topic) return;
      const t = b.topic, bx = b.x + Math.round((b.w - 88) / 2) + 84 - 3;
      const keys = roundKeys(t), rounds = keys.length;      // a room has one round (or none: participants only), a flat review one square, a debate at least two squares
      t.rows.forEach((row, i) => {
        for (let r = 1; r <= rounds; r++) {
          const c = row.cells.find(c => c.round === keys[r - 1]), st = c ? c.state : 'waiting';
          const ca = c && (st === 'draft' || st === 'writing') && c.agent && G.state.agents.find(x => x.id === c.agent);       // the agent behind the cell is stopped or not known: the cell does not blink
          const held = !!ca && (ca.status === 'interrupted' || ca.status === 'unknown');
          const col = st === 'done' ? C.ok : st === 'draft' || st === 'writing' ? (held ? C.amber : f % 10 < 6 ? C.work : '#9ccaff') : st === 'missing' ? C.bad : st === 'paused' || st === 'unknown' ? C.amber : '#c3c6d6';
          R(col, bx - (rounds - r + 1) * 6, b.y + 9 + i * 6, 5, 5);
        }
      });
    });
    // the moving parts of the lounge facilities (game screens · treadmill belts): they move when someone is using them
    const lb = G.blocks.find(b => b.type === 'lounge');
    if (lb) {
      const use = new Set(); for (const e of G.ents.values()) if (e.where === 'lounge' && !e.moving && e.act) use.add(e.act.spot.key);
      lb.arcs.forEach(m => LOUNGE_ART.arcadeScreen(R, m.x, m.y, f, use.has(m.key), m.seed));
      lb.treads.forEach(t => LOUNGE_ART.treadmillBelt(R, t.x, t.y, f, use.has(t.key)));
    }
    // draw in y order: desks · sofas · the front wall of the room (door) · people
    const items = [];
    const near = (b) => { for (const e of G.ents.values()) if (Math.abs(e.x + 6 - b.door.x) < 10 && Math.abs(e.y + 15 - b.door.y) < 13) return true; return false; };
    G.blocks.forEach(b => {
      if (b.type === 'boss') items.push({ y: b.deskY + 6, d: () => bossDesk(R, b, f) });
      if (b.type === 'orch') { const oe = G.ents.get('orch'); items.push({ y: b.deskY + 6, d: () => desk(x, R, b.deskX, b.deskY, 48, oe && oe.where === 'sit' && !oe.moving ? oe : null, f, true, orchProvider(G.state)) }); }
      if (b.type === 'zone') b.seats.forEach(s => {
        const e = s.agentId && G.ents.get(s.agentId);
        const on = e && e.where === 'sit' && !e.moving && e.agent && RUN(e.agent.status);
        // the monitor logo is the seat owner's provider: it stays even when the owner went to rest and the seat is empty. An empty seat (before assignment) has no logo
        const owner = s.agentId && G.state.agents.find(q => q.id === s.agentId);
        items.push({ y: s.deskY + 6, d: () => desk(x, R, s.deskX, s.deskY, 30, on ? e : null, f, false, owner ? owner.provider || 'claude' : null) });
      });
      if (b.type === 'lounge') b.units.slice(0, b.sofaN).forEach(sf => items.push({ y: sf.y + 5, d: () => {   // draw only the sofas in use (small 2-seaters)
        R(C.sofa2, sf.x, sf.y - 12, sf.w, 10); R(C.sofa, sf.x, sf.y, sf.w, 8); R(C.sofa2, sf.x, sf.y + 8, sf.w, 3); R(C.sofa2, sf.x, sf.y - 6, 2, 17); R(C.sofa2, sf.x + sf.w - 2, sf.y - 6, 2, 17);
      } }));
      items.push({ y: b.door.y - 1, d: () => frontWall(R, b, near(b)) });
    });
    items.sort((a, b) => a.y - b.y).forEach(i => i.d());
    // people, icons and paper are drawn on a layer (cv2) above the tags
    const x2 = G.cx2;
    x2.clearRect(0, 0, G.W, G.H); x2.imageSmoothingEnabled = false;
    const R2 = (c, a, b, w, h) => { x2.fillStyle = c; x2.fillRect(Math.round(a), Math.round(b), w, h); };
    const people = [...G.ents.values()].map(e => {
      const seated = !e.moving && (e.where === 'sit' || e.where === 'lounge');
      return { e, seated, y: seated ? (e.where === 'lounge' ? e.y + (e.act ? e.act.spot.sy : 9) : e.y + 11) : e.y + 15 };
    }).sort((a, b) => a.y - b.y);
    drawRings(G, R2, people);
    // The chair backs seen from behind go with the people in y order: right after whoever sits in them, before anyone who walks in front
    const parts = people.map(q => ({ y: q.y, d: () => person(G, x2, R2, q.e, f, q.seated) }));
    if (lb) lb.chairFronts.forEach(c => parts.push({ y: c.y + 9.5, d: () => LOUNGE_ART.chairFront(R2, c.x, c.y, c.dir) }));
    parts.sort((a, b) => a.y - b.y).forEach(q => q.d());
    // flying paper
    const now = Date.now();
    G.papers.forEach(p => {
      const t = G.ents.get(p.toKey); if (!t) return;
      const k = Math.min(1, (now - p.t0) / p.dur), tx = t.x + 6, ty = t.y;
      const px = p.f.x + (tx - p.f.x) * k, py = p.f.y + (ty - p.f.y) * k - Math.sin(Math.PI * k) * 24;
      x2.drawImage(icon(p.kind === 'mail' ? 'mail' : 'doc'), Math.round(px - 4), Math.round(py - 5));
    });
    placeBubbles(G);
  }
  function bossDesk(R, b, f) {
    // the user's desk: big dark solid wood with gilt trim, a laptop, a lamp and a nameplate
    const dx = b.deskX, dy = b.deskY, w = 64;
    R('#c9ccd6', dx + 6, dy - 8, 13, 8); R('#5aa9ff', dx + 7, dy - 7, 11, 5); R('#9aa0b5', dx + 4, dy - 1, 17, 1);   // laptop
    R('#e3b341', dx + w - 10, dy - 11, 1, 10); R('#3fa85a', dx + w - 13, dy - 13, 7, 3); R('#fff4c2', dx + w - 12, dy - 10, 5, 1);  // lamp
    R('#7a3b1f', dx, dy, w, 4); R('#e3b341', dx, dy + 4, w, 1); R('#5e2c16', dx, dy + 5, w, 8); R('#e3b341', dx, dy + 13, w, 1);
    R('#e3b341', dx + w / 2 - 6, dy + 7, 12, 4); R('#7a3b1f', dx + w / 2 - 5, dy + 8, 10, 2);               // nameplate
    R('#3a1a0c', dx + 2, dy + 14, 3, 2); R('#3a1a0c', dx + w - 5, dy + 14, 3, 2);
  }
  function frontWall(R, b, open) {
    // room front wall (top face light, front face dark) and the door in the middle
    const y = b.door.y - 4, dl = b.door.x - 7, dr = b.door.x + 7;
    R(C.wallTop, b.x, y, dl - b.x, 2); R(C.base, b.x, y + 2, dl - b.x, 3);
    R(C.wallTop, dr, y, b.x + b.w - dr, 2); R(C.base, dr, y + 2, b.x + b.w - dr, 3);
    R(C.doorFrame, dl - 1, y - 1, 1, 6); R(C.doorFrame, dr, y - 1, 1, 6);
    if (open) { R(C.door2, dl, y - 9, 2, 12); }                 // open door leaf (stood up on the hinge side)
    else { R(C.door, dl, y, 14, 5); R(C.door2, dl, y + 3, 14, 2); R('#e3b341', dr - 3, y + 2, 1, 1); }
  }
  function placeBubbles(G) {
    const s = G.scale, placed = [];
    G.bubbles.forEach(b => {
      const e = G.ents.get(b.key);
      if (!e) { b.el.style.display = 'none'; return; }
      const w = b.el.offsetWidth || 120, h = b.el.offsetHeight || 30;
      let cx = Math.min(G.W * s - w / 2 - 4, Math.max(w / 2 + 4, (e.x + 6) * s));
      let top = Math.max(h + 4, (e.y - 6) * s);
      // when they overlap, move up and stack (if past the top, go below)
      for (let tries = 0; tries < 6; tries++) {
        const hit = placed.find(p => Math.abs(p.cx - cx) < (p.w + w) / 2 + 4 && Math.abs(p.top - top) < (p.h + h) / 2 + 4);
        if (!hit) break;
        top = hit.top - hit.h - 6;
        if (top < h + 2) top = hit.top + h + 6;
      }
      placed.push({ cx, top, w, h });
      b.el.style.left = cx + 'px'; b.el.style.top = top + 'px'; b.el.style.visibility = 'visible';
    });
  }
  function monitor(x, R, mx, dy, on, f, L, k) {
    R(C.mon, mx, dy - 13, 15, 12); R(C.stand, mx + 7, dy - 1, 1, 1); R(on ? (f % 6 < 3 ? '#15241f' : '#13211c') : C.monOff, mx + 1, dy - 12, 13, 9); 
    if (L) {
      x.globalAlpha = on ? 0.62 : 0.75;
      L.map.forEach((row, j) => { for (let i = 0; i < row.length; i++) if (row[i] === 'x') R(L.col, mx + 3 + i, dy - 12 + j, 1, 1); });
      x.globalAlpha = 1;
    }
    // working: code lines flow up one cell at a time (4 lines, the length changes every frame)
    if (on) {
      x.globalAlpha = 0.8;
      for (let i = 0; i < 4; i++) {
        const n = f + i + k * 3, y = dy - 11 + ((i * 2 + 8 - (f % 8)) % 8);
        R('#5ee3a1', mx + 2, y, 2 + (n * 7) % 9, 1);
      }
      x.globalAlpha = 1;
    }
  }
  function desk(x, R, dx, dy, w, e, f, boss, prov) {
    const on = !!e && ((e.agent && RUN(e.agent.status)) || (boss && e.working)), L = LOGO[prov];
    monitor(x, R, dx, dy, on, f, L, 0);
    if (boss) {   // orchestrator: a laptop beside the monitor
      R('#c9ccd6', dx + 16, dy - 8, 12, 8); R(on ? '#15241f' : C.monOff, dx + 17, dy - 7, 10, 5); R('#9aa0b5', dx + 15, dy - 1, 14, 1);
      if (on) for (let i = 0; i < 2; i++) R('#5ee3a1', dx + 18, dy - 6 + i * 2, 2 + ((f + i * 4) % 7), 1);
    }
    R(C.desk, dx, dy, w, 4); R(C.desk2, dx, dy + 4, w, 7); R(C.deskLeg, dx + 1, dy + 11, 2, 3); R(C.deskLeg, dx + w - 3, dy + 11, 2, 3);
    R(C.stand, dx + 4, dy, 7, 1);                                    // monitor stand (on the desk top)
  }
  // Lounge activity pose: each facility has its own frame, direction and height (seated 12 rows, standing 16 rows). While walking, always the walking picture (the old path of person)
  function actSprite(e, act, f) {
    let pose = 'stand', fr = 0, dir = act.dir, dy = 0, asleep = false;
    switch (act.kind) {
      case 'sofa': if (act.mode === 'doze' && f % 40 < 30) { pose = 'sleep'; asleep = true; } break;   // dozing: eyes closed for 3 seconds, open for 1
      case 'game': dy = (f + act.ph) % 23 < 2 ? -1 : 0; break;                                        // the body bobs now and then
      case 'tread': pose = 'walk'; fr = Math.floor(f / 2); dy = f % 2 ? -1 : 0; break;                // walking in place
      case 'dumb': pose = 'lift'; fr = Math.floor(f / 4); break;                                      // dumbbells: arms down ↔ up
      case 'coffee': if ((f + act.ph) % 80 >= 50) dir = 'down'; break;                                // while the cup fills they look at the machine, and when it is full they turn around and drink
    }
    return { img: charSprite(e.look, pose, fr, dir), h: act.spot.stand ? 16 : 12, dy, asleep };
  }
  // held items (cup · steam · dumbbells). Not drawn when the person is not at the spot = when they go back to work the props vanish too
  function actProps(R, e, act, f, dy) {
    const A = LOUNGE_ART, sp = act.spot, ex = Math.round(e.x), ey = Math.round(e.y) + dy;
    if (act.kind === 'tea') {                                                     // each one's cup is on the table in front of them, a sip every 6 seconds (0.8 seconds)
      if (sp.mouth && (f + act.ph) % 60 < 8) A.cup(R, ex + sp.mouth.x, ey + sp.mouth.y, f, false);
      else A.cup(R, sp.cup.x, sp.cup.y, f, true);
    } else if (act.kind === 'dumb') {
      const hy = Math.floor(f / 4) % 2 ? 6 : 10;                                  // around the hand height (arms up row 7 / down row 10)
      A.dumbbell(R, ex, ey + hy - 1); A.dumbbell(R, ex + 9, ey + hy - 1);
    } else if (act.kind === 'coffee') {
      const c = (f + act.ph) % 80;
      if (c < 50) A.coffeeCup(R, sp.x, sp.y - 17, Math.min(3, Math.floor(c / 14)), c < 42);   // the cup on the machine tray fills up
      else A.cup(R, ex + 5, ey + 6, f, true);                                                   // when it is full they hold it and drink
    }
  }
  function person(G, x, R, e, f, seated) {
    const a = e.agent, st = a ? a.status : null;
    const act = e.where === 'lounge' && !e.moving && e.act;      // arrived at a lounge spot and doing an activity
    let pose = seated ? 'sit' : 'walk';
    const lounging = e.where === 'lounge' && !e.moving;
    if (lounging && !act && st === 'done' && f % 40 < 30) pose = 'sleep';
    const resting = e.key === 'orch' && G.state.orch.state === 'limit_wait' && seated && !e.moving && !e.errand;   // the orchestrator naps at its desk until the usage limit resets
    if (resting && f % 40 < 30) pose = 'sleep';
    const typing = seated && !lounging && ((a && st === 'running') || (e.key === 'orch' && e.working));
    let img, bob, h;
    if (act) { const s = actSprite(e, act, f); img = s.img; bob = s.dy; h = s.h; if (s.asleep) pose = 'sleep'; }
    else {
      img = charSprite(e.look, pose === 'sit' && !typing ? 'stand' : pose, typing || e.moving ? Math.floor(f / 2) : 0, seated ? 'down' : (e.dir || 'down'));
      bob = e.moving && f % 2 ? -1 : 0;
      h = seated ? 12 : 16;
    }
    x.drawImage(img, 0, 0, 12, h, Math.round(e.x), Math.round(e.y) + bob, 12, h);
    if (act) actProps(R, e, act, f, bob);
    let ic = null;
    if (a && (st === 'stalled' || st === 'unknown')) ic = 'stall';
    else if (a && st === 'interrupted') ic = 'pause';
    else if (a && (st === 'failed' || st === 'killed')) ic = 'fail';
    else if (a && st === 'running' && !lounging && !e.moving) {
      const lt = a.last_tool, cur = a.current;
      ic = cur && cur.kind === 'msg' ? 'msg' : lt && Date.now() / 1000 - lt.ts < 90 ? toolIcon(lt, a) : 'think';
    } else if (e.key === 'orch' && e.errand) ic = e.errand.kind === 'listen' ? 'think' : e.errand.kind === 'ask' ? 'stall' : 'doc';
    else if (e.key === 'orch' && !e.working && !e.moving && !resting && G.state.agents.some(q => RUN(q.status))) ic = 'think';
    else if (e.key === 'user' && G.userCalling) ic = 'call';
    else if (act && act.kind === 'sofa' && act.mode === 'chat' && (f + act.speak * 50) % 100 < 25) ic = 'think';   // chat: … above the head while the two speak in turn
    if (e.look && e.look.vip) x.drawImage(icon('crown'), Math.round(e.x + 1), Math.round(e.y) - 4);
    if (ic) x.drawImage(icon(ic), Math.round(e.x + (ic === 'call' ? 7 : 2)), Math.round(e.y) - (ic === 'call' ? 13 : 9) + (f % 8 < 4 ? 0 : -1));
    if ((lounging || resting) && pose === 'sleep') {
      const zy = (f % 20);
      x.globalAlpha = 1 - zy / 20; x.drawImage(icon('z'), Math.round(e.x + 10), Math.round(e.y - 3 - zy / 2)); x.globalAlpha = 1;
    }
  }

  // legend: /game and the dashboard's office card use the same text
  function legendHtml(codex) {
    const L = k => esc(t('office.legend.' + k));
    return `<span><i style="background:#3fb950"></i>${L('submitted')}</span><span><i style="background:#58a6ff"></i>${L('writing')}</span><span><i style="background:#c3c6d6"></i>${L('pending')}</span>` +
      `<span>${L('icons')}</span>` +
      `<span>${L('places')}</span>` +
      `<span><i class="pl">A</i>${L('plate')}</span>` +
      `<span>${L('bubbles')}</span>` +
      `<span>${L('claude')}</span>` + (codex ? `<span>${L('codex')}</span>` : '');
  }
  window.AgentGame = { mount, legendHtml, firstSentence, modelShort, _sprite: charSprite,   // _sprite: for checking the pixel pictures
    _core: { update, RUN, esc, ensureStyle } };   // what game-demo.js uses
})();
