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

  const cache = window.__jevNoLayout ||= {ids: new WeakMap(), nodes: new Map(), next: 1};
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
  const live = e =>
       !e.closest('[aria-hidden="true"],[inert],[hidden]')
    && !e.hasAttribute('hidden')
    && e.getAttribute('aria-hidden') !== 'true'
    && !e.matches(':disabled')
    && !e.closest('[aria-disabled="true"]');

  // A dialog that has not been opened is still in the DOM and still passes the
  // liveness test above -- `aria-hidden` is often only set once it opens. The
  // viewport cull used to remove these for free. Without it, Google Flights
  // offers "Enter your origin" (the title button of a closed dialog) alongside
  // the real "Where from?" field, and a chooser cannot tell them apart.
  const shut = e => {
    const panel = e.closest('dialog,[role="dialog"],[role="listbox"],[role="menu"],[popover]');
    if (!panel) return false;
    if (panel.tagName === 'DIALOG') return !panel.open;
    if (panel.hasAttribute('popover')) return !panel.matches(':popover-open');
    const owner = panel.id && document.querySelector(`[aria-controls="${panel.id}"],[aria-owns="${panel.id}"]`);
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
    const label = name(e);
    if (!label) continue;                      // an unnamed control cannot be chosen
    seen.add(e);

    // A gridcell that merely wraps a button is not the button.
    if (rname === 'gridcell' && e.querySelector('button,[role="button"]')) continue;

    const base = {node: identity(e), role: rname, label, closed: shut(e),
                  region: region(e), depth: (() => {
                    let d = 0, p = e;
                    while ((p = p.parentElement)) d++;
                    return d;
                  })()};
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
      actions.push({...base, kind: editable ? 'fill' : 'click', value});
      if (editable) actions.push({...base, kind: 'click', value, label: `Open ${base.label}`});
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
  const disambiguate = list => {
    const groups = new Map();
    for (const a of list) {
      const key = `${a.kind}|${a.label.toLowerCase()}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(a);
    }
    for (const group of groups.values()) {
      if (group.length < 2) continue;
      // Order is document order by depth, so the suffix is stable between
      // observations even when the page re-renders around the control.
      group.sort((x, y) => x.depth - y.depth);
      group.forEach((a, i) => {
        const where = a.region || `${i + 1} of ${group.length}`;
        a.label = `${a.label} · ${where}`;
      });
    }
    return list;
  };
  disambiguate(actions);

  // textContent, not innerText: innerText is a rendered view, and on Moli it
  // reports 25 characters where textContent reports 89,091.
  const words = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node, length = 0;
  while ((node = walker.nextNode()) && length < 6000) {
    const value = node.textContent.trim();
    const parent = node.parentElement;
    if (!value || !parent) continue;
    if (parent.closest('script,style,noscript,template')) continue;
    if (!live(parent)) continue;
    words.push(value);
    length += value.length;
  }
  const text = words.join(' ').slice(0, 6000);

  const marker = actions.map(a => `${a.node}:${a.kind}:${a.label}`).join('|');
  return {url: location.href, title: document.title, text, actions, marker};
})()
