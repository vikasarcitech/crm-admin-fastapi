/* global window, document */
/**
 * UI primitives.
 *
 * Everything renders through h(), which sets text via textContent and
 * attributes via setAttribute. No innerHTML anywhere in the admin, so a
 * lead whose name is `<img onerror=…>` is inert by construction.
 */
(function () {
  'use strict';

  /**
   * h('div.panel', { onclick: fn }, [children])
   * Tag supports .class and #id shorthand.
   */
  function h(tag, props, children) {
    const [name, ...classes] = String(tag).split('.');
    const [tagName, id] = name.split('#');
    const el = document.createElement(tagName || 'div');
    if (id) el.id = id;
    if (classes.length) el.className = classes.join(' ');

    Object.entries(props || {}).forEach(([key, value]) => {
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') el.className = `${el.className} ${value}`.trim();
      else if (key === 'text') el.textContent = value;
      else if (key === 'html') el.innerHTML = value;           // trusted markup only (icons)
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === 'value') el.value = value;
      else if (key === 'checked' || key === 'disabled' || key === 'selected') el[key] = Boolean(value);
      else el.setAttribute(key, value);
    });

    const list = Array.isArray(children) ? children : (children === undefined ? [] : [children]);
    list.flat().forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return el;
  }

  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };
  const mount = (node, children) => {
    clear(node);
    (Array.isArray(children) ? children : [children]).flat()
      .filter(Boolean)
      .forEach((c) => node.appendChild(c));
  };

  // -------------------------------------------------------- formatters
  const STATUS_LABELS = {
    new: 'New', contacted: 'Contacted', qualified: 'Qualified',
    proposal: 'Proposal', won: 'Won', lost: 'Lost',
  };

  function formatDate(value, withTime) {
    if (!value) return '—';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return '—';
    const opts = withTime
      ? { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }
      : { day: '2-digit', month: 'short', year: 'numeric' };
    return d.toLocaleString(undefined, opts);
  }

  function relativeTime(value) {
    if (!value) return '—';
    const diff = Date.now() - new Date(value).getTime();
    const mins = Math.round(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.round(hrs / 24);
    return days < 30 ? `${days}d ago` : formatDate(value);
  }

  const statusPill = (status) =>
    h(`span.pill.s-${status}`, { text: STATUS_LABELS[status] || status });

  // ------------------------------------------------------------ toasts
  function toast(message, kind = 'info') {
    const host = document.getElementById('toast-host');
    if (!host) return;
    const node = h('div.toast', { text: message, dataset: { kind } });
    host.appendChild(node);
    setTimeout(() => node.remove(), kind === 'error' ? 6000 : 3200);
  }

  // ------------------------------------------------------------ drawer
  let closeDrawerFn = null;

  function openDrawer({ title, subtitle, body, actions }) {
    closeDrawer();
    const host = document.getElementById('drawer-host');

    const backdrop = h('div.drawer-backdrop', { onclick: closeDrawer });
    const panel = h('aside.drawer', { role: 'dialog', 'aria-modal': 'true', 'aria-label': title }, [
      h('div.drawer-head', {}, [
        h('div', {}, [
          h('h2', { text: title }),
          subtitle ? h('p.page-sub', { text: subtitle }) : null,
        ]),
        h('div.spacer'),
        actions || null,
        h('button.icon-btn', { type: 'button', 'aria-label': 'Close', text: '\u00d7', onclick: closeDrawer }),
      ]),
      h('div.drawer-body', {}, body),
    ]);

    mount(host, [backdrop, panel]);
    document.body.style.overflow = 'hidden';

    const onKey = (e) => { if (e.key === 'Escape') closeDrawer(); };
    document.addEventListener('keydown', onKey);
    panel.querySelector('button, input, select, textarea, a')?.focus();

    closeDrawerFn = () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = '';
      clear(host);
      closeDrawerFn = null;
    };
    return { close: closeDrawer, body: panel.querySelector('.drawer-body') };
  }

  function closeDrawer() { if (closeDrawerFn) closeDrawerFn(); }

  // ------------------------------------------------------- small parts
  const spinner = () => h('div.empty', { text: 'Loading…' });

  const emptyState = (heading, note, action) =>
    h('div.empty', {}, [h('h3', { text: heading }), h('p', { text: note }), action || null]);

  const field = (label, control) => h('label.field', {}, [h('span', { text: label }), control]);

  const select = (options, value, onchange, attrs = {}) =>
    h('select', { onchange, ...attrs },
      options.map(([val, label]) =>
        h('option', { value: val, selected: String(val) === String(value ?? ''), text: label })));

  window.ui = {
    h, clear, mount, toast, openDrawer, closeDrawer, spinner, emptyState,
    field, select, statusPill, formatDate, relativeTime, STATUS_LABELS,
  };
}());
