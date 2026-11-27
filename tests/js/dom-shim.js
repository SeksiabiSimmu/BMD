'use strict';
// A tiny DOM shim used by popup.test.js and dashboard.test.js to load the real UI
// scripts (app/extension/popup.js, dashboard.js) in Node and drive them with events.
// It implements only the small slice of the DOM those two files actually use, so the
// tests exercise the real branching, validation, rendering and chrome-runtime contract
// rather than a hand-rolled stub of the logic.

function makeElement(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    id: '',
    hidden: false,
    disabled: false,
    checked: false,
    indeterminate: false,
    type: '',
    title: '',
    value: '',
    className: '',
    dataset: {},
    children: [],
    parentNode: null,
    isConnected: true,
    _listeners: {},
    style: {},
    _text: '',
  };
  // textContent behaves like the DOM: setting it replaces children with a text node; reading it
  // concatenates the element's own text plus all descendant text. Tests read card text built via
  // append(), so the getter must aggregate descendants rather than return only _text.
  Object.defineProperty(el, 'textContent', {
    configurable: true,
    get() { return (el._text || '') + el.children.map((c) => c.textContent).join(''); },
    set(v) { el._text = String(v ?? ''); },
  });
  el.classList = {
    _set: new Set(),
    add(c) { this._set.add(c); },
    remove(c) { this._set.delete(c); },
    toggle(c, force) {
      const has = this._set.has(c);
      const next = force === undefined ? !has : !!force;
      if (next) this._set.add(c); else this._set.delete(c);
      return next;
    },
    contains(c) { return this._set.has(c); },
    toString() { return [...this._set].join(' '); },
  };
  el.setAttribute = (name, value) => { el[name] = value; };
  el.getAttribute = (name) => el[name];
  el.append = (...nodes) => {
    for (const node of nodes) {
      if (node && typeof node === 'object') {
        node.parentNode = el;
        el.children.push(node);
      }
    }
  };
  el.appendChild = (node) => { node.parentNode = el; el.children.push(node); return node; };
  el.removeChild = (node) => {
    const i = el.children.indexOf(node);
    if (i !== -1) { el.children.splice(i, 1); node.parentNode = null; }
    return node;
  };
  el.remove = () => { if (el.parentNode) el.parentNode.removeChild(el); };
  el.replaceChildren = (...nodes) => {
    for (const child of el.children) if (child.parentNode === el) child.parentNode = null;
    el.children = [];
    el.append(...nodes);
  };
  el.addEventListener = (type, fn) => {
    (el._listeners[type] = el._listeners[type] || []).push(fn);
    if (type === 'click') el._click = fn;       // popup/dashboard only ever bind click
  };
  el.removeEventListener = () => {};
  // The scripts call el('checkHost').click() programmatically; mirror a real click.
  el.click = (event) => {
    const ev = { preventDefault() {}, stopPropagation() {}, ...event, target: el };
    return el._click ? el._click(ev) : undefined;
  };
  el.querySelectorAll = (sel) => {
    // Used for '.tab' / '.nav' / '.filter' class sweeps; return all children that match.
    const cls = sel.startsWith('.') ? sel.slice(1) : null;
    const out = [];
    const walk = (node) => {
      for (const child of node.children || []) {
        if (cls ? child.classList.contains(cls) : true) out.push(child);
        walk(child);
      }
    };
    walk(el);
    return out;
  };
  return el;
}

// Build a document whose registry holds every id the UI scripts reference. Tab/nav/filter
// buttons are created standalone (the real HTML has no id on them, only class+data-*), so
// querySelectorAll('.tab'/'.nav'/'.filter') returns them and openTab()/page()/filter handlers
// bind as in the browser.
function buildDocument(ids, options = {}) {
  const byId = {};
  const floating = [];   // elements with no id (tab/nav/filter buttons) that querySelectorAll must see
  const document = {
    hidden: false,
    _visListeners: [],
    addEventListener: (type, fn) => { if (type === 'visibilitychange') document._visListeners.push(fn); },
    getElementById: (id) => byId[id],
    createElement: makeElement,
    querySelectorAll: (sel) => {
      const cls = sel.startsWith('.') ? sel.slice(1) : null;
      const out = [];
      const collect = (el) => { if (cls ? el.classList.contains(cls) : true) out.push(el); };
      for (const id of Object.keys(byId)) collect(byId[id]);
      for (const el of floating) collect(el);
      return out;
    },
  };
  for (const id of ids) byId[id] = makeElement('div');
  const wire = (list, key, cls) => {
    if (!list) return;
    for (const name of list) {
      const btn = makeElement('button');
      btn.dataset[key] = name;
      btn.classList.add(cls);
      floating.push(btn);
    }
  };
  wire(options.tabs, 'tab', 'tab');
  wire(options.nav, 'page', 'nav');
  wire(options.filters, 'filter', 'filter');
  // Convenience: look up a floating button by its dataset value.
  document.tab = (name) => floating.find((e) => e.classList.contains('tab') && e.dataset.tab === name);
  document.nav = (name) => floating.find((e) => e.classList.contains('nav') && e.dataset.page === name);
  document.filter = (name) => floating.find((e) => e.classList.contains('filter') && e.dataset.filter === name);
  // Trigger a click on a registered element, returning whatever the listener resolves to.
  document.click = async (id, event = {}) => {
    const el = byId[id];
    if (!el || !el._click) return undefined;
    let resolved;
    const ev = { preventDefault() {}, stopPropagation() {}, ...event, target: el };
    const maybe = el._click(ev);
    if (maybe && typeof maybe.then === 'function') resolved = await maybe;
    return resolved;
  };
  return { document, byId, floating };
}

module.exports = { makeElement, buildDocument };
