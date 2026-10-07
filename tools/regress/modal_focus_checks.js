// Focus-capable synthetic DOM layered over the page harness. Run by codex_checks.js
// in both languages, against the actual page scripts and modal HTML.
module.exports = async function modalFocusChecks(P, html, check) {
  const doc = P.ctx.document, nodes = [], query = doc.querySelector;
  const within = (root, node) => { for (let n = node; n; n = n.parentElement) if (n === root) return true; return false; };
  const matches = (node, selector) => selector.split(',').some(s => {
    s = s.trim();
    if (s.startsWith('#')) return node.id === s.slice(1);
    if (s === '[data-idx]') return node.dataset.idx != null;
    if (s === '[hidden]') return node.hidden;
    if (s === '[inert]') return node.inert;
    return false;
  });
  function node(id, parent, attrs = {}) {
    const n = P.el('#' + id);
    Object.assign(n, { id, parentElement: parent, attrs, tabIndex: attrs.tabindex == null ? 0 : +attrs.tabindex, disabled: false, hidden: false, isConnected: true });
    n.contains = other => within(n, other);
    n.closest = selector => { for (let a = n; a; a = a.parentElement) if (matches(a, selector)) return a; return null; };
    n.getAttribute = name => n.attrs[name] ?? null;
    n.setAttribute = (name, value) => { n.attrs[name] = value; if (name === 'tabindex') n.tabIndex = +value; };
    n.getClientRects = () => n.isConnected && !n.closest('[hidden], [inert]') && (!within(dialog, n) || bg.classList.contains('open')) ? [{}] : [];
    n.focus = () => { if (!n.disabled && n.getClientRects().length) doc.activeElement = n; };
    nodes.push(n);
    return n;
  }
  const bg = P.el('#modalBg');
  bg.id = 'modalBg';
  const opening = html.match(/<div\b[^>]*\bid="modalDialog"[^>]*>/)?.[0] || '';
  const attrs = Object.fromEntries([...opening.matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
  const dialog = node('modalDialog', bg, attrs), close = node('mClose', dialog), body = node('mBody', dialog, { tabindex: '-1' });
  const feed = node('feedItems'), talk = node('atalkList'), big = node('atalkBig'), session = node('sessionSel'), filter = node('focus-feed-filter');
  dialog.querySelectorAll = () => nodes.filter(n => n !== dialog && n !== body && within(dialog, n) && n.isConnected);
  // innerHTML replacement really detaches the modeled descendants, so a removed
  // opener cannot accidentally pass the focus-return assertion.
  for (const root of [body, feed, talk]) {
    const prop = Object.getOwnPropertyDescriptor(root, 'innerHTML');
    Object.defineProperty(root, 'innerHTML', { ...prop, set(value) {
      for (const n of nodes) if (n !== root && within(root, n)) n.isConnected = false;
      prop.set(value);
    } });
  }
  doc.querySelector = selector => {
    const match = selector.match(/^#(feedItems|atalkList|talkList) \[data-(encrypted-idx|idx)="(\d+)"\]$/);
    if (match) return nodes.find(n => n.isConnected && n.closest('#' + match[1]) && n.dataset[match[2] === 'encrypted-idx' ? 'encryptedIdx' : 'idx'] === match[3]) || null;
    if (selector === '#feedFilter .on') return filter;
    return query(selector);
  };
  const press = (key, shiftKey = false) => {
    const event = { key, shiftKey, prevented: false, preventDefault() { this.prevented = true; } };
    for (const listener of P.listeners.keydown || []) listener(event);
    return event;
  };
  const event = { idx: 980, ts: P.run('S.now'), kind: 'handback', from: P.run('S.agents[0].id'), to: 'orch', title: 'Synthetic report', text: 'hidden', full_len: 10000, text_i18n: { key: 'event.encrypted.text', params: {} } };
  P.ctx.focusEvent = event;
  P.run('globalThis.focusSavedFeed = S.feed; globalThis.focusSavedTalk = ui.atalk; S.feed = [focusEvent]; ui.atalk = [focusEvent]; renderFeed(); renderAtalk({ force: true })');
  const button = (id, parent) => { const n = node(id, parent); n.dataset.encryptedIdx = String(event.idx); return n; };
  const open = n => {
    P.ctx.focusButton = n;
    P.run('wireEncryptedNotes({ querySelectorAll: () => [focusButton] }, [focusEvent])');
    n.focus(); n.onclick({ stopPropagation() {} });
  };

  const trigger = button('focus-feed-trigger', feed);
  open(trigger);
  check('the explanation has a named modal dialog and moves focus to its close button',
    dialog.getAttribute('role') === 'dialog' && dialog.getAttribute('aria-modal') === 'true' && dialog.getAttribute('aria-labelledby') === 'mTitle'
    && !!P.text('#mTitle') && doc.activeElement === close && bg.classList.contains('open'));
  check('Tab and Shift+Tab stay inside a dialog with only its close button', press('Tab').prevented && doc.activeElement === close && press('Tab', true).prevented && doc.activeElement === close);
  const link = node('focus-body-link', body), hidden = node('focus-hidden-link', body), disabled = node('focus-disabled-link', body), skipped = node('focus-skipped-link', body, { tabindex: '-1' });
  hidden.hidden = true; disabled.disabled = true;
  check('Tab within the dialog keeps the native order', !press('Tab').prevented);
  link.focus();
  check('Tab from the last visible enabled control wraps to the first', press('Tab').prevented && doc.activeElement === close);
  check('Shift+Tab wraps to the last visible enabled control, skipping hidden, disabled and negative tabindex elements', press('Tab', true).prevented && doc.activeElement === link && doc.activeElement !== skipped);
  trigger.focus();  // A focus jump to the background is contained on the next Tab.
  check('Tab from outside the dialog moves back inside', press('Tab').prevented && doc.activeElement === close);
  check('Escape closes the explanation and restores the opening button', press('Escape').prevented && !bg.classList.contains('open') && doc.activeElement === trigger);
  check('Tab is left alone after closing the dialog', !press('Tab').prevented);

  const talkTrigger = button('focus-talk-trigger', talk);
  open(talkTrigger); close.onclick();
  check('the close button restores an agent-talk opener', doc.activeElement === talkTrigger && !bg.classList.contains('open'));
  open(trigger);
  P.run('renderFeed()');  // The real renderer replaces the original trigger.
  const replacement = button('focus-feed-replacement', feed);
  close.onclick();
  check('a redraw restores the equivalent event button instead of a detached opener', !trigger.isConnected && P.html('#feedItems').includes('data-encrypted-idx="980"') && doc.activeElement === replacement);
  open(replacement); P.run('S.feed = []; renderFeed()'); close.onclick();
  check('a filtered-away event returns focus to its pane filter', doc.activeElement === filter);

  big.focus(); big.onclick();
  const nested = button('focus-nested-trigger', body);
  open(nested); close.onclick();
  check('a whole-conversation button removed by body replacement returns to the same message in the list', !nested.isConnected && doc.activeElement === talkTrigger);
  big.focus(); big.onclick();
  const missing = button('focus-nested-missing', body);
  P.run('ui.atalk = []; renderAtalk({ force: true })');
  open(missing); press('Escape');
  check('without a list counterpart, closing returns to the whole-conversation opener', !missing.isConnected && doc.activeElement === big);

  const fileTrigger = node('focus-file-trigger', null, { tabindex: '-1' });
  P.ctx.fileTrigger = fileTrigger;
  await P.run('openFile("/synthetic/focus.md", fileTrigger)');
  check('the shared file dialog moves focus inside and keeps its loaded content', doc.activeElement === close && P.html('#mBody').includes('Synthetic file.'));
  close.onclick();
  check('closing a file dialog returns focus to its clickable row', doc.activeElement === fileTrigger);
  const diagTrigger = node('focus-diag-trigger');
  P.ctx.diagTrigger = diagTrigger;
  await P.run('openDiag(diagTrigger)');
  check('the shared diagnostics dialog moves focus inside', doc.activeElement === close && P.run('ui.modalKind') === 'diag');
  bg.onclick({ target: bg });
  check('backdrop dismissal returns focus to the diagnostics opener', doc.activeElement === diagTrigger && !bg.classList.contains('open'));
  P.ctx.focusButton = null;
  P.run('showModal("Synthetic", "", "", focusButton)');
  close.onclick();
  check('a modal without an opener has a stable session-selector fallback', doc.activeElement === session);
  P.run('S.feed = focusSavedFeed; ui.atalk = focusSavedTalk; renderFeed(); renderAtalk({ force: true })');
  doc.querySelector = query;
};
