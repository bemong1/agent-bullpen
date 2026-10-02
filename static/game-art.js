/* Pixel-art data and sprite building for the agent office (used by game.js).
 * Palette, hair and shirt colors, the picture rows of the people and the over-the-head icons, monitor logos, the sprite cache. Layout, drawing and events are in game.js.
 * Loading builds no DOM (a sprite is built on first use). Loaded before game.js.
 *   window.AgentGameArt = { SHIRTS, HAIR, C, ICONS, LOGO, charSprite, icon, LOUNGE_ART }
 * LOUNGE_ART: functions that draw the lounge facilities (arcade, treadmill, dumbbell rack, tea table, coffee spot) with R(color,x,y,w,h).
 */
(function () {
  'use strict';

  const SHIRTS = ['#4a90e2', '#3fb56b', '#f0a02a', '#e0559b', '#9b6cf0', '#1abcb0'];
  const HAIR = { A: '#2b1d0e', B: '#8b4a1c', C: '#e3be4a', D: '#b83a2c', E: '#3a3a55' };
  const C = {
    wall: '#2d2a4a', wall2: '#26233f', base: '#1b1930', win: '#6b6f9e', glass: '#7fc8f5', glass2: '#a9dcfa',
    floor: '#8a5a3c', floor2: '#7d5136', seam: '#6b4329', desk: '#c8894f', desk2: '#a86b3a', deskLeg: '#6e4526',
    mon: '#2b2b35', monOff: '#1b1d27', board: '#e9e9f1', boardF: '#9aa0b5',
    chair: '#4a4e69', sofa: '#b04a5a', sofa2: '#8e3a48', pot: '#a0522d', leaf: '#3fa85a', leaf2: '#2e7d43',
    door: '#5b3a29', door2: '#47291c', ok: '#3fb950', work: '#58a6ff',
    bad: '#f85149', amber: '#e3b341', white: '#ffffff', ink: '#1b1b2a', skin: '#f2c79a', mouth: '#c0645a', pants: '#3a3f5c',
    corr: '#5b5d70', corr2: '#52546a', corrLine: '#3d3f52', wallTop: '#5a5680', doorFrame: '#2a1a12',
    orchRug: '#5a2f3a', orchRugCodex: '#2e4257',                                           // command room carpet (blue-grey for the Codex orchestrator)
    tile: '#e4d7bf', tile2: '#e0d2b9', grout: '#d8c9ae',                                    // work room floor: light beige carpet tiles
    chairBack: '#3f4556', chairRim: '#596074', chairSeat: '#4b5163', stand: '#a7adba',   // office chair, monitor stand
  };

  // ---------- pixel art (one letter = 1 pixel) ----------
  const BODY = [
    '...kkkkkk...',
    '..khhhhhhk..',
    '.khhhhhhhhk.',
    '.khsssssshk.',
    '.kswesswesk.',
    '.kssssssssk.',
    '..kssrrssk..',
    '...kssssk...',
    '..kcccccck..',
    '.kcccccccck.',
    'ksccccccccsk',
    '.kcccccccck.',
    '.kppppppppk.',
    '.kppk..kppk.',
    '.kppk..kppk.',
    '.kkkk..kkkk.',
  ];
  const LEGS_B = ['..kppkkppk..', '..kpk..kpk..', '.kkk....kkk.'];
  const EYES_SHUT = '.kskksskksk.';
  // back view: the back of the head instead of a face
  const BACK = [
    '...kkkkkk...',
    '..khhhhhhk..',
    '.khhhhhhhhk.',
    '.khhhhhhhhk.',
    '.khhhhhhhhk.',
    '.kshhhhhhsk.',
    '..kshhhhsk..',
    '...kssssk...',
    '..kcccccck..',
    '.kcccccccck.',
    'ksccccccccsk',
    '.kcccccccck.',
    '.kppppppppk.',
    '.kppk..kppk.',
    '.kppk..kppk.',
    '.kkkk..kkkk.',
  ];
  // side view (facing right). Left is this flipped horizontally
  const SIDE = [
    '...kkkkkk...',
    '..khhhhhhk..',
    '.khhhhhhhhk.',
    '.khhhhsssk..',
    '.khhhsssesk.',
    '.khhhssssssk',
    '..khhssrsk..',
    '...kssssk...',
    '...kcccck...',
    '..kcccccck..',
    '..kccsscck..',
    '..kcccccck..',
    '..kppppppk..',
    '..kppkkppk..',
    '.kppk..kppk.',
    '.kkk....kkk.',
  ];
  const SIDE_LEGS_B = ['...kppppk...', '...kpkkpk...', '...kkkkkk..'.padEnd(12, '.')];
  // Codex agent: the same character with black thick-rimmed glasses (G). Front view: a rim around both eyes; side view: one lens and a temple arm going to the ear;
  // back view: only the temple arms at the sides of the head show. Hair color, clothes and body are the same as the Claude picture
  function glasses(rows, dir) {
    const put = (y, xs) => { const a = [...rows[y]]; xs.forEach(i => { a[i] = 'G'; }); rows[y] = a.join(''); };
    if (dir === 'up') { put(4, [2, 9]); return rows; }
    if (dir === 'left' || dir === 'right') {   // a small ring lens at the eye (a white glint in the middle) and a temple arm going to the ear
      put(3, [8]); put(4, [5, 6, 7, 9]); put(5, [8]);
      const a = [...rows[4]]; a[8] = 'w'; rows[4] = a.join('');
      return rows;
    }
    put(3, [3, 4, 7, 8]); put(4, [2, 5, 6, 9]); put(5, [3, 4, 7, 8]); return rows;
  }
  const flip = rows => rows.map(r => [...r].reverse().join(''));
  // dumbbell lift (front view): the arms-down frame is BODY as it is; in the raised frame the hands go up to shoulder height (row 7). game.js draws the dumbbell over the hand position
  const LIFT_UP = { 7: '.s.kssssk.s.', 8: '.kkcccccckk.', 9: '.kcccccccck.', 10: '.kcccccccck.', 11: '.kcccccccck.' };
  const liftUp = rows => rows.map((r, y) => LIFT_UP[y] || r);
  const ICONS = {
    read: ['.bb.bb.', 'bwwbwwb', 'bwwbwwb', 'bwwbwwb', '.bbbbb.'],
    write: ['.....yk', '....yyk', '...yy..', '..yy...', '.oy....', 'o......'],
    bash: ['kkkkkkk', 'kgkkkkk', 'kkgkkkk', 'kgkkggk', 'kkkkkkk'],
    web: ['..bbb..', '.bgbgb.', 'bggbggb', '.bgbgb.', '..bbb..'],
    msg: ['ooooooo', 'owwwwwo', 'oowwwoo', 'owowowo', 'ooooooo'],
    think: ['.......', 'w.w.w..', '.......'],
    stall: ['.yyy.', 'y...y', '...y.', '..y..', '.....', '..y..'],
    fail: ['.r.', '.r.', '.r.', '...', '.r.'],
    pause: ['yy.yy', 'yy.yy', 'yy.yy', 'yy.yy', 'yy.yy'],            // two bars: interrupted (a limit, an API error), not over
    mail: ['wwwwwww', 'wkwwwkw', 'wwkwkww', 'wwwkwww', 'wwwwwww'],
    doc: ['wwww.', 'wkkww', 'wwwww', 'wkkkw', 'wwwww', 'wkkkw', 'wwwww'],
    z: ['www', '..w', '.w.', 'w..', 'www'],
    crown: ['y..y..y', 'yy.y.yy', 'yyyyyyy'],
    call: ['kkkkkkk', 'kwwrwwk', 'kwwrwwk', 'kwwrwwk', 'kwwwwwk', 'kwwrwwk', 'kkkkkkk', '.kk....'],   // a red ! in a white speech bubble
  };
  const ICON_COL = { b: '#5aa9ff', w: '#ffffff', y: '#f5c542', k: '#1b1b2a', o: '#f0883e', g: '#3fe07a', r: '#f85149' };

  const spriteCache = new Map();
  function sprite(rows, colors, key) {
    if (spriteCache.has(key)) return spriteCache.get(key);
    const cv = document.createElement('canvas');
    cv.width = rows[0].length; cv.height = rows.length;
    const cx = cv.getContext('2d');
    rows.forEach((r, y) => [...r].forEach((ch, x) => {
      const col = colors[ch];
      if (col) { cx.fillStyle = col; cx.fillRect(x, y, 1, 1); }
    }));
    spriteCache.set(key, cv);
    return cv;
  }
  function charSprite(look, pose, frame, dir = 'down') {
    // pose: stand / walk / sit / sleep, dir: down (front) / up (back) / left / right (side). A seated figure always faces front
    if (pose === 'sit' || pose === 'sleep') dir = 'down';
    const side = dir === 'left' || dir === 'right', X = look.provider === 'codex';   // X: Codex (glasses)
    let rows = (dir === 'up' ? BACK : side ? SIDE : BODY).slice();
    if (pose === 'walk' && frame % 2) rows = rows.slice(0, 13).concat(side ? SIDE_LEGS_B : LEGS_B);
    if (pose === 'lift' && dir === 'down' && frame % 2) rows = liftUp(rows);   // dumbbell lift: arms raised (odd frames)
    if (pose === 'sleep') rows[4] = EYES_SHUT;
    if (X) rows = glasses(rows, dir === 'left' ? 'right' : dir);
    if (pose === 'sit' && frame % 2) rows[10] = '.kcscccsck..'.padEnd(12, '.');
    if (look.boss) {           // headset (at the ears) and necktie (front view only)
      rows = rows.map((r, y) => {
        const a = [...r];
        if (side) { if (y >= 3 && y <= 5) a[3] = 'g'; }
        else {
          if (y >= 3 && y <= 5) { a[0] = 'g'; a[11] = 'g'; }
          if (dir === 'down') {
            if (y === 6) a[1] = 'g';
            if (y >= 8 && y <= 11) a[5] = a[6] = 't';
          }
        }
        return a.join('');
      });
    }
    if (dir === 'left') rows = flip(rows);
    const colors = { k: C.ink, h: look.hair, s: C.skin, w: C.white, e: C.ink, r: C.mouth, c: look.shirt, p: C.pants, g: '#8b8fa8', t: '#d63a3a', G: C.ink };
    return sprite(rows, colors, `${look.hair}|${look.shirt}|${look.boss ? 1 : 0}|${pose}|${frame % 2}|${dir}|${X ? 'x' : ''}`);
  }
  // over-the-head icon: a 1-pixel dark outline (so the white dots and the yellow show on a light floor too). The picture grows by 1 cell all around → subtract 1 when drawing
  function icon(name) {
    const key = 'iconO:' + name;
    if (spriteCache.has(key)) return spriteCache.get(key);
    const rows = ICONS[name], w = rows[0].length, h = rows.length;
    const cv = document.createElement('canvas'); cv.width = w + 2; cv.height = h + 2;
    const cx = cv.getContext('2d');
    cx.fillStyle = 'rgba(20,18,36,.88)';
    rows.forEach((r, y) => [...r].forEach((ch, x) => { if (ICON_COL[ch]) cx.fillRect(x, y, 3, 3); }));
    rows.forEach((r, y) => [...r].forEach((ch, x) => { const c = ICON_COL[ch]; if (c) { cx.fillStyle = c; cx.fillRect(x + 1, y + 1, 1, 1); } }));
    spriteCache.set(key, cv);
    return cv;
  }

  // monitor logo (a neutral shape that does not imitate a trademark): Claude Code = orange >_, Codex = white ◆ (same as the Codex mark on the dashboard).
  // Laid over the whole screen translucently; while working, code lines flow over it.
  const LOGO = {
    claude: { col: '#e8845f', map: ['.........', 'xx.......', '.xx......', '..xx.....', '...xx....', '..xx.....', '.xx......', 'xx..xxxxx', '.........'] },
    codex: { col: '#f2f4f8', map: ['....x....', '...xxx...', '..xxxxx..', '.xxxxxxx.', 'xxxxxxxxx', '.xxxxxxx.', '..xxxxx..', '...xxx...', '....x....'] },
  };

  // ---------- lounge facility art ----------
  // All are functions that draw with R(color, x, y, w, h). The static part (furniture) is drawn once onto the background by game.js; the moving parts (screen, belt, steam, cup) are drawn every frame.
  // Coordinates are relative to the top left of each facility. Sizes: arcade 14×22, treadmill 18×27, tea table 13×13, dumbbell rack 11×12.
  const ARCADE = [
    '..kkkkkkkkkk..', '.kmmmmmmmmmmk.', '.kmwmwwmwwmmk.', '.kmmmmmmmmmmk.', '.kbbbbbbbbbbk.', '.kbkkkkkkkkbk.',
    '.kbksssssskbk.', '.kbksssssskbk.', '.kbksssssskbk.', '.kbksssssskbk.', '.kbksssssskbk.', '.kbksssssskbk.',
    '.kbkkkkkkkkbk.', '.kbbbbbbbbbbk.', '.kbppppppppbk.', '.kbpjppprgpbk.', '.kbppppppppbk.', '.kbbbbbbbbbbk.',
    '.kbddddddddbk.', '.kbddddddddbk.', '.kbddddddddbk.', '.kkkkkkkkkkkk.',
  ];
  const ARCADE_COL = [{ m: '#e0503c', b: '#b8402f', d: '#8a2f22' }, { m: '#3c8ce0', b: '#2f6fb0', d: '#22507f' }];   // two machines: red and blue
  const ARCADE_PAL = { k: '#15121f', w: '#ffe9a8', s: '#0d1226', p: '#2b2f45', j: '#f5c542', r: '#f85149', g: '#3fb950' };
  const blit = (R, rows, pal, ox, oy) => rows.forEach((row, j) => { for (let i = 0; i < row.length; i++) { const c = pal[row[i]]; if (c) R(c, ox + i, oy + j, 1, 1); } });
  const TABLE_W = [10, 14, 18, 18, 18, 18, 18, 14, 10];   // the top of a round table (centered), 9 rows
  const LOUNGE_ART = {
    // arcade machine (cab 0·1). The screen area (4,6)–(9,11) is left empty; arcadeScreen draws it
    arcade(R, x, y, cab) { blit(R, ARCADE, Object.assign({}, ARCADE_PAL, ARCADE_COL[cab % 2]), x, y); R(ARCADE_PAL.s, x + 4, y + 6, 6, 6); },
    // screen: when on (play), colored dots move and the background flickers; when empty, one dot blinks slowly on a dark screen
    arcadeScreen(R, x, y, f, play, seed) {
      R(play && f % 7 < 2 ? '#16204a' : '#0d1226', x + 4, y + 6, 6, 6);
      if (!play) { if ((f >> 3) % 2) R('#3a4a7a', x + 5 + seed % 3, y + 8, 2, 1); return; }
      const cols = ['#ff5a5a', '#5aff9d', '#ffe45a', '#5ab8ff', '#ff8cf0'];
      for (let i = 0; i < 4; i++) R(cols[(i + (f >> 2) + seed) % 5], x + 4 + ((f + i * 5 + seed) * (i + 2)) % 5, y + 6 + (i * 2 + (f >> 1)) % 5, 1 + i % 2, 1);
    },
    // treadmill: the display and the handrail posts on top, the belt below
    treadmill(R, x, y) {
      R('#2b2e3a', x + 3, y, 12, 5); R('#0e2a1c', x + 4, y + 1, 10, 3); R('#3fe07a', x + 5, y + 2, 4, 1); R('#f85149', x + 11, y + 2, 2, 1);
      R('#8b8fa8', x + 2, y + 5, 2, 14); R('#8b8fa8', x + 14, y + 5, 2, 14); R('#a7adba', x + 2, y + 5, 14, 1);
      R('#2b2e3a', x, y + 19, 18, 8); R('#1b1d27', x, y + 25, 18, 2);
    },
    // top of the belt: when on, the stripes flow from top to bottom (toward the person)
    treadmillBelt(R, x, y, f, on) {
      R('#4a4f60', x + 1, y + 19, 16, 6);
      for (let i = 0; i < 3; i++) R('#3a3e4d', x + 1, y + 19 + (on ? (i * 2 + f) % 6 : i * 2), 16, 1);
    },
    // dumbbell rack: two tiers, six dumbbells
    rack(R, x, y) {
      R('#5b6072', x, y, 1, 12); R('#5b6072', x + 10, y, 1, 12); R('#5b6072', x, y + 5, 11, 1); R('#5b6072', x, y + 11, 11, 1);
      [['#8b8fa8', 1], ['#5aa9ff', 4], ['#f0883e', 7]].forEach(([c, i]) => { R('#1b1d27', x + i, y + 3, 3, 2); R(c, x + i + 1, y + 3, 1, 2); R('#1b1d27', x + i, y + 9, 3, 2); R(c, x + i + 1, y + 9, 1, 2); });
    },
    // Tea table, seen from the front and above like the desks and the sofas (a 13x13 box at x, y; the layout keeps its box and the seats against it):
    // a flat oval top (light wood, darker rim) with a teapot, the front thickness (dark band), a short column, a floor base and a translucent shadow on the carpet.
    // Rows 0-6 top, 5-8 band (the same oval 2 rows lower, seen below the top), 9-10 column, 11-12 base; the shadow spans rows 9-12.
    tea(R, x, y) {
      const OVAL = [7, 11, 13, 13, 13, 11, 7], line = (c, j, w, dx = 0) => R(c, x + dx + Math.floor((13 - w) / 2), y + j, w, 1);
      [[9, 7], [10, 11], [11, 13], [12, 9]].forEach(([j, w]) => line('rgba(0,0,0,.26)', j, w));                   // floor shadow
      line('#5c3d24', 12, 9); line('#7a5232', 11, 7); R('#8d6038', x + 4, y + 11, 3, 1);                           // base: the lit upper face and the front edge
      R('#7a5232', x + 5, y + 9, 3, 2); R('#8d6038', x + 5, y + 9, 1, 2); R('#5c3d24', x + 7, y + 9, 1, 2);        // column: lit left side, shaded right side
      OVAL.forEach((w, j) => { line('#7a5232', j + 2, w); });                                                      // the band: the oval again, 2 rows lower
      line('#5c3d24', 8, 7); R('#5c3d24', x + 1, y + 7, 1, 1); R('#5c3d24', x + 11, y + 7, 1, 1);                // its lower outline
      OVAL.forEach((w, j) => {                                                                                      // the top: rim, light face
        line('#a86b3a', j, w);
        if (j > 0 && j < 6) line('#d9a066', j, w - (j === 1 || j === 5 ? 6 : 2));
      });
      R('#e6b27a', x + 3, y + 1, 3, 1); R('#e6b27a', x + 1, y + 2, 2, 1);                                          // light from the upper left
      R('#5a8fd6', x + 5, y + 2, 3, 2); R('#3f6db0', x + 5, y + 3, 3, 1); R('#cfe0f7', x + 6, y + 1, 1, 1);        // teapot at the centre of the top (blue, apart from the white cups)
    },
    // Small wooden chair for the person whose 12x12 body is at x, y (dir = the way they face, toward the table). The seat plank is at rows 12-13 under the body.
    // This is the part drawn with the floor (behind the person); chairFront draws what is in front of a person seen from behind.
    chair(R, x, y, dir) {
      const WD = '#7a5232', WL = '#8d6038', WK = '#5c3d24';
      if (dir === 'down') {                                                  // faces the viewer: the chair back rises behind the shoulders, the legs are behind the table
        R(WD, x - 1, y + 7, 14, 2); R(WL, x - 1, y + 7, 14, 1); R(WK, x - 1, y + 9, 1, 4); R(WK, x + 12, y + 9, 1, 4);
        R(WK, x - 1, y + 5, 1, 2); R(WK, x + 12, y + 5, 1, 2); R(WD, x - 1, y + 12, 14, 2); R(WK, x - 1, y + 13, 14, 1);
      } else if (dir === 'up') {                                             // seen from behind: the seat and the legs here, the back in front of the person
        R(WL, x - 1, y + 12, 14, 1); R(WD, x - 1, y + 13, 14, 1); R(WK, x, y + 14, 2, 3); R(WK, x + 10, y + 14, 2, 3);
      } else {                                                               // side view: the back post on the far side, the seat toward the table
        const r = dir === 'right', bx = r ? x - 1 : x + 11, sx = r ? x - 1 : x, fx = r ? x + 8 : x + 1, rx = r ? x : x + 10;
        R(WD, bx, y + 6, 2, 8); R(WL, bx, y + 6, 1, 8); R(WL, sx, y + 12, 13, 1); R(WD, sx, y + 13, 13, 1); R(WK, rx, y + 14, 2, 3); R(WK, fx, y + 14, 2, 3);
      }
    },
    chairFront(R, x, y, dir) {                                               // the back of a chair seen from behind covers the lower back of whoever sits in it
      if (dir !== 'up') return;
      R('#8d6038', x - 1, y + 9, 14, 1); R('#5c3d24', x - 1, y + 7, 1, 7); R('#5c3d24', x + 12, y + 7, 1, 7);
    },
    // held items (the moving part): the cup is a white 2×2, the steam is 1 pixel
    cup(R, x, y, f, steam) { R('#f4f4fa', x, y, 2, 2); if (steam) R('rgba(223,230,238,.85)', x + ((f >> 2) % 2), y - 1 - ((f >> 1) % 2), 1, 1); },
    dumbbell(R, x, y) { R('#1b1d27', x - 1, y, 1, 3); R('#8b8fa8', x, y + 1, 3, 1); R('#1b1d27', x + 3, y, 1, 3); },
    // in front of the coffee machine (back wall, 12×22): the spout and the tray, the cup on the tray fills up (fill 0–3), the stream that pours. The machine picture is in game.js. A person stands from y+17 at the top of the machine
    coffeeSpot(R, x, y) { R('#1b1d27', x + 5, y + 10, 2, 2); R('#1b1d27', x + 2, y + 16, 8, 1); },
    coffeeCup(R, x, y, fill, pour) {
      R('#f4f4fa', x + 4, y + 12, 4, 4);
      if (fill) R('#5a3418', x + 5, y + 15 - fill, 2, fill);
      if (pour) R('#6b3f1d', x + 6, y + 12, 1, 3 - fill);
    },
  };

  window.AgentGameArt = { SHIRTS, HAIR, C, ICONS, LOGO, charSprite, icon, LOUNGE_ART };
})();
