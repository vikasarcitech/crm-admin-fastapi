/* global window, document, api, ui */
/**
 * View kit — the parts every platform screen repeats.
 *
 * The original views.js builds each table and drawer by hand, which is
 * fine for six screens. With twenty, the boilerplate is where the bugs
 * live, so the shared shapes are here: tabs, a data table, a save
 * drawer, a filter toolbar, inline confirmation.
 *
 * Everything still renders through ui.h(), so no user data ever reaches
 * innerHTML.
 */
(function () {
  'use strict';

  const { h, mount, toast, openDrawer, closeDrawer, select, formatDate, relativeTime } = ui;

  // ------------------------------------------------------------- layout
  /** Tab strip that drives a hash query param, so tabs are linkable. */
  function tabs(ctx, items, active, param = 'tab') {
    return h('div.tabs', {}, items.map(([key, label, badge]) =>
      h('button', {
        type: 'button',
        class: key === active ? 'is-current' : '',
        'aria-current': key === active ? 'true' : null,
        onclick: () => {
          const params = { ...ctx.params, [param]: key };
          delete params.open;
          ctx.navigate(`#/${ctx.path}${api.qs(params)}`);
        },
      }, [label, badge ? h('span.count', { text: String(badge) }) : null])));
  }

  const panel = (title, body, actions) =>
    h('section.panel', {}, [
      title
        ? h('div.panel-head', {}, [
          h('h2', { text: title }),
          h('div.spacer'),
          actions || null,
        ])
        : null,
      body,
    ]);

  const panelBody = (children) => h('div.panel-body', {}, children);

  const kpi = (label, value, note) =>
    h('div.kpi', {}, [
      h('div.kpi-label', { text: label }),
      h('div.kpi-value', { text: value === null || value === undefined ? '—' : String(value) }),
      note ? h('div.kpi-note', { text: note }) : null,
    ]);

  /**
   * Data table.
   *   columns: [{ label, cell(row), class?, width? }]
   * `onRow` makes the whole row clickable; `empty` is shown when there
   * are no rows at all.
   */
  function table(columns, rows, opts = {}) {
    if (!rows.length) {
      return panelBody(h('p.muted', { text: opts.empty || 'Nothing here yet.' }));
    }
    return h('table.list', {}, [
      h('thead', {}, h('tr', {}, columns.map((c) =>
        h('th', { text: c.label || '', style: c.width ? `width:${c.width}` : null })))),
      h('tbody', {}, rows.map((row) => h('tr', {
        onclick: opts.onRow ? () => opts.onRow(row) : null,
        style: opts.onRow ? 'cursor:pointer' : null,
      }, columns.map((c) => {
        const value = c.cell(row);
        return h(`td${c.class ? `.${c.class}` : ''}`, {},
          value instanceof Node || Array.isArray(value)
            ? value
            : [document.createTextNode(value === null || value === undefined ? '—' : String(value))]);
      })))),
    ]);
  }

  const toolbar = (children) => h('div.toolbar', {}, children);

  const search = (value, onchange, placeholder = 'Search…') =>
    h('input', {
      type: 'search',
      value: value || '',
      placeholder,
      // Enter rather than input: a request per keystroke is a request
      // per keystroke, and these lists are server-filtered.
      onchange: (e) => onchange(e.target.value.trim()),
    });

  const badge = (text, kind) => h('span.pill', { text, dataset: { kind: kind || 'neutral' } });

  const bool = (value, yes = 'Yes', no = 'No') =>
    badge(value ? yes : no, value ? 'ok' : 'off');

  function bytes(n) {
    if (!n) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  const number = (n) => (n === null || n === undefined ? '—' : Number(n).toLocaleString());

  const percent = (n, digits = 1) =>
    n === null || n === undefined ? '—' : `${Number(n).toFixed(digits)}%`;

  // ------------------------------------------------------------ controls
  const textInput = (name, value, attrs = {}) =>
    h('input', { name, type: 'text', value: value ?? '', ...attrs });

  const textarea = (name, value, attrs = {}) =>
    h('textarea', { name, rows: attrs.rows || 4, ...attrs }, [String(value ?? '')]);

  const checkbox = (name, checked, label) =>
    h('label.check', {}, [
      h('input', { name, type: 'checkbox', checked: Boolean(checked) }),
      h('span', { text: label }),
    ]);

  /** Character counter, for the SEO length indicators. */
  function counted(input, min, max, initial) {
    const readout = h('span.count-hint');
    const paint = (length) => {
      readout.textContent = `${length} / ${max}`;
      readout.dataset.state = length === 0 ? 'empty' : length < min ? 'short' : length > max ? 'long' : 'ok';
    };
    paint(String(initial ?? '').length);
    input.addEventListener('input', (e) => paint(e.target.value.length));
    return h('div.counted', {}, [input, readout]);
  }

  // ------------------------------------------------------------- drawer
  /**
   * Drawer with a form and a Save button.
   *   fields: [{ name, label, control, help }] — control is a Node
   *   onSave(values) — values keyed by field name; throw to keep it open
   */
  function formDrawer({ title, subtitle, fields, onSave, saveLabel = 'Save', extra, danger }) {
    const form = h('form.drawer-form', { onsubmit: (e) => e.preventDefault() },
      fields.filter(Boolean).map((f) =>
        h('label.field', {}, [
          h('span', { text: f.label }),
          f.control,
          f.help ? h('small.muted', { text: f.help }) : null,
        ])));

    const saveButton = h('button.btn.btn-primary', { type: 'button', text: saveLabel });
    // Assigned below, before any click can happen.
    let handle = null;

    saveButton.addEventListener('click', async () => {
      const values = {};
      fields.filter(Boolean).forEach((f) => {
        const control = f.control.querySelector
          ? (f.control.matches('input,select,textarea') ? f.control
            : f.control.querySelector('input,select,textarea'))
          : f.control;
        if (!control || !f.name) return;
        values[f.name] = control.type === 'checkbox' ? control.checked : control.value;
      });

      saveButton.disabled = true;
      saveButton.textContent = 'Saving…';
      try {
        await onSave(values);
        // Close this layer specifically: onSave may have opened another
        // drawer on top (a shown-once secret, recovery codes), and
        // closing "the top one" would dismiss that instead.
        handle?.close();
      } catch (err) {
        toast(err.message, 'error');
        saveButton.disabled = false;
        saveButton.textContent = saveLabel;
      }
    });

    handle = openDrawer({
      title,
      subtitle,
      body: [form, extra || null, danger || null],
      actions: saveButton,
    });
    return handle;
  }

  /**
   * Two-step confirmation on the button itself.
   *
   * A native confirm() is blocked in some embedded contexts and reads
   * as a browser dialog rather than part of the app; making the button
   * ask again keeps the destructive action in one place.
   */
  function confirmButton(label, onConfirm, opts = {}) {
    const button = h(`button.btn${opts.danger === false ? '' : '.btn-danger'}${opts.small ? '.btn-sm' : ''}`, {
      type: 'button',
      text: label,
    });
    let armed = false;
    let timer = null;

    button.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!armed) {
        armed = true;
        button.textContent = opts.confirmLabel || 'Click again to confirm';
        button.dataset.armed = '1';
        timer = setTimeout(() => {
          armed = false;
          button.textContent = label;
          delete button.dataset.armed;
        }, 4000);
        return;
      }
      clearTimeout(timer);
      button.disabled = true;
      button.textContent = 'Working…';
      try {
        await onConfirm();
      } catch (err) {
        toast(err.message, 'error');
        button.disabled = false;
        armed = false;
        button.textContent = label;
        delete button.dataset.armed;
      }
    });
    return button;
  }

  const actionButton = (label, onclick, opts = {}) =>
    h(`button.btn${opts.primary ? '.btn-primary' : ''}${opts.small ? '.btn-sm' : ''}`, {
      type: 'button',
      text: label,
      onclick: async (e) => {
        e.stopPropagation();
        const node = e.currentTarget;
        node.disabled = true;
        try {
          await onclick();
        } catch (err) {
          toast(err.message, 'error');
        } finally {
          node.disabled = false;
          node.textContent = label;
        }
      },
    });

  /** Pager driven by the hash route. */
  function pager(ctx, page, pages) {
    if (pages <= 1) return null;
    const go = (n) => ctx.navigate(`#/${ctx.path}${api.qs({ ...ctx.params, page: n })}`);
    return h('div.pagination', {}, [
      h('button.btn.btn-sm', { type: 'button', text: 'Previous', disabled: page <= 1, onclick: () => go(page - 1) }),
      h('span.muted', { text: `Page ${page} of ${pages}` }),
      h('button.btn.btn-sm', { type: 'button', text: 'Next', disabled: page >= pages, onclick: () => go(page + 1) }),
    ]);
  }

  const notice = (text, kind = 'info') =>
    text ? h('div.notice', { text, dataset: { kind } }) : null;

  /** Guard a screen behind a permission the API also enforces. */
  const can = (ctx, permission) =>
    (ctx.session.permissions || []).includes(permission);

  const denied = (permission) =>
    ui.emptyState(
      'You do not have access to this',
      `This screen needs the “${permission}” permission. Ask an owner or admin.`,
    );

  window.kit = {
    tabs, panel, panelBody, kpi, table, toolbar, search, badge, bool, bytes,
    number, percent, textInput, textarea, checkbox, counted, formDrawer,
    confirmButton, actionButton, pager, notice, can, denied,
    // Re-exported so views import from one place.
    h, mount, toast, select, formatDate, relativeTime, openDrawer, closeDrawer,
    closeAllDrawers: ui.closeAllDrawers,
  };
}());
