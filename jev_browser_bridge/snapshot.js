// Read a page without asking the layout engine anything.
//
// Moli keeps page structure and interaction state in memory and lays it out
// only when something needs a picture. Geometry is therefore a snapshot from
// the last render while the DOM moves on -- measured on Google Flights, 140 of
// 146 interactive elements report a zero-sized box and innerText returns 25
// characters where textContent returns 89,091.
//
// So nothing here calls getBoundingClientRect, checkVisibility, elementFromPoint
// or innerText. Liveness comes from semantics, text comes from textContent, and
// every element keeps a stable id so actions can address it directly.
(() => {
  if (!document.body) return null;

  const cache = window.__jevBridge ||= {ids: new WeakMap(), nodes: new Map(), next: 1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e, cache.next++);
    const id = cache.ids.get(e);
    cache.nodes.set(id, e);
    return id;
  };
  for (const [id, e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);

  const safe = e => !['password', 'file', 'hidden'].includes(e.type);

  // Liveness by semantics. A zero-sized box is not evidence of absence when
  // the box was measured two renders ago.
  //
  //
  // Asked once, natively, not once per element. What is dead is found with a
  // single selector query -- every hidden, inert or aria-disabled element and
  // everything under it -- and each element is then looked up in that set.
  // Walking ancestors in script was half the snapshot's cost on a slow engine:
  // Cloudflare's Kitesurf runs this inside a per-page CPU budget and ran out
  // of it on long articles, while its selector matching is native.
  // An inline `display: none` is an attribute like any other -- it is how
  // scripts hide and show things -- so it counts, without asking layout.
  const DEAD = ['[aria-hidden="true"]', '[inert]', '[hidden]', '[aria-disabled="true"]',
                '[style*="display: none"]', '[style*="display:none"]'];
  const dead = new Set(document.querySelectorAll(DEAD.flatMap(d => [d, `${d} *`]).join(',')));
  const live = e => !dead.has(e) && !e.matches(':disabled');
  const liveHere = e => !dead.has(e) && e.disabled !== true;

  // A dialog that has not been opened is still in the DOM and still passes the
  // liveness test above -- `aria-hidden` is often only set once it opens. The
  // viewport cull used to remove these for free. Without it, Google Flights
  // offers "Enter your origin" (the title button of a closed dialog) alongside
  // the real "Where from?" field, and a chooser cannot tell them apart.
  const PANELS = 'dialog,[role="dialog"],[role="listbox"],[role="menu"],[popover]';
  const anyPanel = document.querySelector(PANELS) !== null;
  const shut = e => {
    if (!anyPanel) return false;            // most pages have none: skip the walk
    const panel = e.closest(PANELS);
    if (!panel) return false;
    if (panel.tagName === 'DIALOG') return !panel.open;
    if (panel.hasAttribute('popover')) return !panel.matches(':popover-open');
    // An id may legally contain quotes or brackets; interpolating it raw
    // throws a SyntaxError that takes the whole snapshot down, and the caller
    // reads that as "the page is navigating".
    let owner = null;
    if (panel.id) {
      try {
        const id = CSS.escape(panel.id);
        owner = document.querySelector(`[aria-controls="${id}"],[aria-owns="${id}"]`);
      } catch { owner = null; }
    }
    if (owner) return owner.getAttribute('aria-expanded') === 'false';
    return false;
  };

  const name = (e, seen = new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => name(document.getElementById(id), seen)).filter(Boolean).join(' ');
    return (referenced
      || e.getAttribute('aria-label')
      || [...(e.labels || [])].map(l => name(l, seen)).filter(Boolean).join(' ')
      || (['button', 'submit', 'reset'].includes(e.type) ? e.value : '')
      || e.getAttribute('alt')
      || (e.tagName === 'INPUT' ? '' : [...e.childNodes].map(n =>
           n.nodeType === 3 ? n.textContent
         : n.nodeType === 1 && n.getAttribute('aria-hidden') !== 'true' ? name(n, seen)
         : '').join(' '))
      || e.getAttribute('title')
      || e.getAttribute('placeholder')
      || '').trim().replace(/\s+/g, ' ');
  };

  // A field with no accessible name is still a field. Forms often put the
  // label beside the input without tying them together -- `<label>Username
  // </label><input>`, no `for` -- and a text box on its own may carry nothing
  // but an id. Dropping such a field left a goal like "enter X and press
  // Submit" with nothing to type into, and the agent gave up on step one.
  // So name it by the text just before it, then by its own name or id, and
  // failing all of that by what it is.
  const FIELD_ROLES = ['textbox', 'searchbox', 'spinbutton', 'combobox', 'checkbox', 'radio'];
  const FORMATS = {date: 'YYYY-MM-DD', month: 'YYYY-MM', week: 'YYYY-Www', time: 'HH:MM',
                   number: 'a number', email: 'an email address', url: 'a URL'};
  // Text in the nearest sibling that has any, walking one way.
  const beside = (e, step) => {
    for (let p = e[step]; p; p = p[step]) {
      if (p.nodeType !== 3 && p.nodeType !== 1) continue;
      if (p.nodeType === 1 && p.matches('input,textarea,select,button')) return '';
      const text = (p.textContent || '').replace(/\s+/g, ' ').trim();
      if (text) return text.replace(/[:*]\s*$/, '').slice(0, 60);
    }
    return '';
  };
  const fieldHint = (e, rname) => {
    if (!FIELD_ROLES.includes(rname)) return '';
    // A box's label comes after it; a text field's comes before.
    const text = ['checkbox', 'radio'].includes(rname)
      ? beside(e, 'nextSibling') || beside(e, 'previousSibling')
      : beside(e, 'previousSibling');
    return text || `${rname} ${e.getAttribute('name') || e.id || ''}`.trim();
  };

  const ROLES = ['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem',
                 'menuitemcheckbox', 'menuitemradio', 'option', 'gridcell', 'combobox',
                 'textbox', 'searchbox', 'spinbutton', 'slider'];

  // Role-derived selectors miss a whole class of control: a calendar day in
  // Google Flights is a bare <div> whose only marking is
  // aria-label="Sunday, September 27, 2026" -- no role, no tabindex, no
  // handler attribute. If it has a name and reacts to a click, it is a control.
  const SELECTOR = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],'
    + '[onclick],[tabindex]:not([tabindex="-1"]),[aria-label]:not([aria-label=""]),'
    + ROLES.map(r => `[role="${r}"]`).join(',');

  const role = e => {
    const explicit = e.getAttribute('role');
    if (ROLES.includes(explicit)) return explicit;
    if (e.tagName === 'BUTTON' || e.tagName === 'SUMMARY') return 'button';
    if (e.tagName === 'A') return 'link';
    if (e.tagName === 'SELECT') return 'combobox';
    if (e.tagName === 'TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName === 'INPUT') {
      if (['checkbox', 'radio'].includes(e.type)) return e.type;
      if (['button', 'submit', 'reset', 'image'].includes(e.type)) return 'button';
      if (e.type === 'search') return 'searchbox';
      if (e.type === 'number') return 'spinbutton';
      if (['text', 'email', 'url', 'tel', 'date', 'month', 'week', 'time'].includes(e.type))
        return 'textbox';
    }
    if (e.hasAttribute('onclick') || e.hasAttribute('tabindex')) return 'button';
    if ((e.getAttribute('aria-label') || '').trim()) return 'button';
    return null;
  };

  // Where an element sits, named by its nearest landmark. This is what tells
  // four buttons all called "Done" apart once geometry is gone -- see
  // disambiguate() below.
  const region = e => {
    const scope = e.closest('dialog,[role="dialog"],[role="listbox"],[role="menu"],'
      + '[role="grid"],[role="tabpanel"],form,nav,header,footer,aside,[role="region"]');
    if (!scope) return '';
    return (scope.getAttribute('aria-label') || scope.getAttribute('title')
      || scope.getAttribute('role') || scope.tagName.toLowerCase()).trim().slice(0, 40);
  };

  const actions = [];
  const seen = new Set();
  for (const e of document.querySelectorAll(SELECTOR)) {
    if (seen.has(e) || !safe(e) || !live(e)) continue;
    const rname = role(e);
    if (!rname) continue;
    const label = name(e) || fieldHint(e, rname);
    if (!label) continue;                      // an unnamed control cannot be chosen
    seen.add(e);

    // A gridcell that merely wraps a button is not the button.
    if (rname === 'gridcell' && e.querySelector('button,[role="button"]')) continue;

    // Where a control sits (region, depth) only matters when its name is
    // shared, so disambiguate() works it out for those alone.
    const base = {node: identity(e), role: rname, label, closed: shut(e),
                  href: e.tagName === 'A' ? e.href : ''};
    for (const key of ['checked', 'selected', 'expanded']) {
      const value = e.getAttribute('aria-' + key);
      if (value !== null) base[key] = value;
    }
    if (['checkbox', 'radio'].includes(e.type)) base.checked = String(e.checked);

    if (e.tagName === 'SELECT') {
      for (const o of e.options) {
        if (o.selected || o.disabled || o.closest('optgroup[disabled]')) continue;
        actions.push({...base, kind: 'select', value: o.value,
                      current_value: [...e.selectedOptions].map(x => x.label).join(', '),
                      label: `${base.label} → ${o.label}`});
      }
    } else {
      const editable = !e.readOnly && e.getAttribute('aria-readonly') !== 'true'
        && (['textbox', 'searchbox', 'spinbutton'].includes(rname)
            || (rname === 'combobox' && ['INPUT', 'TEXTAREA'].includes(e.tagName)));
      const value = 'value' in e ? String(e.value)
        : e.isContentEditable || rname === 'combobox' ? (e.textContent || '').trim() : '';
      // A typed field that parses its value says how: a date input takes
      // 2011-12-15 and silently rejects 12/15/2011.
      if (editable && e.tagName === 'INPUT' && FORMATS[e.type]) base.format = FORMATS[e.type];
      actions.push({...base, kind: editable ? 'fill' : 'click', value});
      if (editable) actions.push({...base, kind: 'click', value, label: `Open ${base.label}`});
    }
  }

  // Controls that only a script knows about. A <span> with a click listener
  // bound by addEventListener or jQuery has no role, no href, no onclick
  // attribute -- nothing in the markup says it does anything. The page's own
  // stylesheet usually does: it gives the thing `cursor: pointer`. So read the
  // rules, not the rendering -- selector text is there whether or not anything
  // was ever laid out -- and offer what they point at.
  //
  // Measured on MiniWoB++: links, "Mute" and "Reply" buttons and a trash icon
  // were all such spans, and the page offered no control at all.
  //
  // Not every engine has a CSS object model: Lightpanda's styleSheets is
  // empty and cannot even be iterated. There, read the rules from the <style>
  // elements' own text. Either way nothing here may take the snapshot down.
  const pointerSelectors = [];
  // `.x:hover { cursor: pointer }` still means .x is the thing to click.
  const plain = sel => sel.replace(/::?(hover|active|focus(-within|-visible)?)\b/g, '').trim();
  try {
    const sheets = document.styleSheets;
    for (let i = 0; i < (sheets?.length || 0); i++) {
      let rules;
      try { rules = sheets[i].cssRules; } catch { continue; }   // cross-origin sheet
      for (let j = 0; j < (rules?.length || 0); j++) {
        const rule = rules[j];
        if (rule.style?.cursor === 'pointer' && rule.selectorText) {
          pointerSelectors.push(plain(rule.selectorText));
        }
      }
    }
    if (!pointerSelectors.length) {
      for (const style of document.querySelectorAll('style')) {
        const css = (style.textContent || '').replace(/\/\*[\s\S]*?\*\//g, '');
        for (const m of css.matchAll(/([^{}@]+)\{([^{}]*)\}/g)) {
          if (/(^|;)\s*cursor\s*:\s*pointer/.test(m[2])) pointerSelectors.push(plain(m[1]));
        }
      }
    }
  } catch { /* no rules to read: offer what the markup says, and no more */ }
  pointerSelectors.push('[style*="cursor: pointer"]', '[style*="cursor:pointer"]');
  // Inside a control already offered, a pointer is that control's own
  // styling. Inside another pointer element it may well be a separate target
  // (a trash icon on a clickable email row), so only the first pass counts.
  const semantic = new Set(seen);
  const underControl = e => {
    for (let p = e; p; p = p.parentElement) if (semantic.has(p)) return true;
    return false;
  };
  const iconName = e => [...e.classList].filter(c => !/^(ui|js|is|has)-/.test(c))
    .slice(0, 3).join(' ').replace(/[-_]+/g, ' ');
  for (const selector of pointerSelectors) {
    let found;
    try { found = document.querySelectorAll(selector); } catch { continue; }
    for (const e of found) {
      if (seen.has(e) || e === document.body || e === document.documentElement
          || !live(e) || underControl(e)
          || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(e.tagName)) continue;
      // Only a short name is a control's name; a long one is a container
      // whose whole text happens to be clickable, and gets the start of it.
      const label = name(e) || iconName(e);
      if (!label) continue;
      seen.add(e);
      // A clickable list -- a menu whose <ul> carries the pointer -- reads as
      // one control named "Reply Retweet Like Share via DM Mute ...". Clicking
      // the list does nothing; its items are the controls. Offer those.
      const items = [...e.children].filter(c => live(c) && !semantic.has(c)
        && (c.textContent || '').trim() && (c.textContent || '').trim().length <= 40);
      if (items.length >= 2) {
        for (const c of items) {
          const itemLabel = name(c) || iconName(c);
          if (!itemLabel || seen.has(c)) continue;
          seen.add(c);
          actions.push({node: identity(c), role: 'button', label: itemLabel,
                        closed: shut(c), href: '', kind: 'click', value: ''});
        }
        continue;
      }
      actions.push({node: identity(e), role: 'button', label: label.slice(0, 80),
                    closed: shut(e), href: '', kind: 'click', value: ''});
    }
  }

  // Geometry was doing a second, undocumented job: disambiguation. The viewport
  // cull left exactly one candidate on screen, so a chooser never had to tell
  // duplicates apart. Google's date picker has four <button>s that all read
  // "Done"; only one carries "Done. Search for one-way flights, departing on
  // September 27, 2026", and only that one commits the date.
  //
  // Replace the cull with an explicit rule: same name and same kind means the
  // label must say where the control lives, or how deep it sits. Nothing is
  // dropped -- a hidden duplicate today is a live control after the next click.
  // With no landmark to name, a repeated control is told apart by the words
  // around it: the trash icon on each email row sits next to that email's
  // sender, the "Mute" in each post's menu next to that post's author.
  // Climb until an ancestor says something besides the control's own name.
  // Only words that are not themselves controls count: a row of buttons
  // "Odd" "Even" says nothing about which row it is.
  const plainText = p => {
    let out = '';
    const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
    for (let t = walker.nextNode(); t && out.length < 80; t = walker.nextNode()) {
      let q = t.parentElement, inside = false;
      for (; q && q !== p; q = q.parentElement) if (seen.has(q)) { inside = true; break; }
      if (!inside) out += ' ' + t.nodeValue;
    }
    return out.replace(/\s+/g, ' ').trim();
  };
  const context = (e, label) => {
    for (let p = e?.parentElement, n = 0; p && p !== document.body && n < 6;
         p = p.parentElement, n++) {
      const text = plainText(p);
      if (text && text.toLowerCase() !== label.toLowerCase()) return text.slice(0, 40);
    }
    return '';
  };
  const disambiguate = list => {
    const groups = new Map();
    for (const a of list) {
      const key = `${a.kind}|${a.label.toLowerCase()}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(a);
    }
    for (const group of groups.values()) {
      if (group.length < 2) continue;
      // Links that share a name and a destination are one control repeated,
      // not several controls that need telling apart. Wikipedia links the
      // same article from the lede, the body and two navboxes; numbering them
      // "Berne Convention · 1 of 5" ... "· 5 of 5" made the one unnumbered
      // near-match -- a citation pointing off-site -- look like the cleanest
      // choice, and the model took it every time.
      const hrefs = new Set(group.map(a => a.href || ''));
      if (hrefs.size === 1 && !hrefs.has('')) continue;
      for (const a of group) {
        const e = cache.nodes.get(a.node);
        a.region = e ? region(e) : '';
        let d = 0, p = e;
        while (p && (p = p.parentElement)) d++;
        a.depth = d;
      }
      // Order is document order by depth, so the suffix is stable between
      // observations even when the page re-renders around the control.
      group.sort((x, y) => x.depth - y.depth);
      group.forEach((a, i) => {
        const where = a.region || context(cache.nodes.get(a.node), a.label)
          || `${i + 1} of ${group.length}`;
        a.label = `${a.label} · ${where}`;
      });
    }
    return list;
  };
  disambiguate(actions);

  // The page's words, cut into retrievable units, and all of them.
  //
  // This used to be the first 6,000 characters of text. That is what a
  // viewport-bound reader produces anyway -- it never sees more than a screen
  // -- but for a reader that has the whole document it throws away what it
  // just read. Measured on four long articles, the sentence carrying the
  // answer was on the page every time and outside the first 6,000 characters
  // every time. Choosing which rows a decision gets to see is a retrieval
  // problem, so it happens on the Python side (evidence.py), with the goal in
  // hand; this only has to hand over every row, in order.
  //
  // A block contributes its OWN text -- text nodes and inline descendants,
  // with nested blocks left to speak for themselves. Taking each block's whole
  // subtree would repeat every paragraph once per ancestor. A table row is the
  // exception: its cells are joined into one row, "Elevation | 8,848.86 m",
  // because a label and its value split into separate rows can no longer be
  // paired. textContent throughout, never innerText: innerText is a rendered
  // view, and on Moli it reports 25 characters where textContent reports 89,091.
  const BLOCK = new Set(['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'CAPTION', 'DD',
    'DIV', 'DL', 'DT', 'FIELDSET', 'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM', 'H1', 'H2',
    'H3', 'H4', 'H5', 'H6', 'HEADER', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION',
    'TABLE', 'TBODY', 'TD', 'TH', 'THEAD', 'TFOOT', 'TR', 'UL', 'DETAILS', 'SUMMARY']);
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'svg']);
  const squash = s => s.replace(/\s+/g, ' ').trim();
  const own = block => {
    let out = '';
    for (const child of block.childNodes) {
      if (child.nodeType === 3) { out += child.nodeValue; continue; }
      if (child.nodeType !== 1 || SKIP.has(child.tagName) || BLOCK.has(child.tagName)) continue;
      if (!liveHere(child)) continue;
      out += ' ' + child.textContent + ' ';
    }
    return squash(out);
  };
  const rows = [];
  const walk = parent => {
    for (const child of parent.children) {
      // Descending only through live elements is what lets liveHere() stand
      // in for live(): an ancestor walk per element was half the snapshot's
      // cost on a slow engine.
      if (SKIP.has(child.tagName) || !liveHere(child)) continue;
      if (child.tagName === 'TR') {
        const cells = [...child.children].map(c => squash(c.textContent)).filter(Boolean);
        if (cells.length) rows.push(cells.join(' | '));
        continue;
      }
      if (BLOCK.has(child.tagName)) {
        const line = own(child);
        if (line.length > 1) rows.push(line);   // single characters are bullets
      }
      walk(child);
    }
  };
  if (live(document.body)) walk(document.body);

  const marker = actions.map(a => `${a.node}:${a.kind}:${a.label}`).join('|');
  return {url: location.href, title: document.title, rows, actions, marker};
})()
