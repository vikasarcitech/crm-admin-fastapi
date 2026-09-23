/* global window, document, api, ui */
/**
 * Views. Each export is `async (ctx) => void` where ctx provides:
 *   ctx.el        container element to render into
 *   ctx.params    parsed query params from the hash route
 *   ctx.setHead   (title, subtitle, actionsNode)
 *   ctx.navigate  (hash) push a new route
 *   ctx.session   { user, counts }
 */
(function () {
  'use strict';

  const { h, mount, toast, openDrawer, closeDrawer, spinner, emptyState,
    field, select, statusPill, formatDate, relativeTime, STATUS_LABELS } = ui;

  const STATUS_OPTIONS = Object.entries(STATUS_LABELS);
  const canEdit = (user) => ['owner', 'admin', 'agent'].includes(user.role);

  // =====================================================================
  // Dashboard
  // =====================================================================
  async function dashboard(ctx) {
    ctx.setHead('Dashboard', 'Where leads are coming from and what needs work.');
    mount(ctx.el, spinner());

    const days = Number(ctx.params.days) || 30;
    const data = await api.get(`/api/dashboard${api.qs({ days })}`);

    const peak = Math.max(1, ...data.byDay.map((d) => d.n));
    const ledger = h('div.ledger', {}, data.byDay.map((d) =>
      h('div.ledger-bar', {
        title: `${d.day}: ${d.n} lead${d.n === 1 ? '' : 's'}`,
        dataset: { empty: d.n === 0 ? '1' : '0' },
        style: `height:${Math.max(2, Math.round((d.n / peak) * 100))}%`,
      })));

    const sourceRows = data.bySource.length
      ? data.bySource.map((s) => {
        const share = Math.round((s.n / data.bySource.reduce((a, b) => a + b.n, 0)) * 100);
        return h('tr', {}, [
          h('td', { text: s.source }),
          h('td.cell-mono', { text: String(s.n) }),
          h('td.cell-mono', { text: `${share}%` }),
        ]);
      })
      : [h('tr', {}, h('td.muted', { colspan: 3, text: 'No attribution data yet.' }))];

    // The lead-desk KPI row (win rate, won value, unworked) is gone with
    // the Leads screens; what is left is the volume chart and where it
    // comes from.
    mount(ctx.el, [
      h('div.split', {}, [
        h('div.stack', {}, [
          h('section.panel', {}, [
            h('div.panel-head', {}, [
              h('h2', { text: 'Lead volume' }),
              h('div.spacer'),
              // Plain links: the export is a GET on the session cookie,
              // so the browser downloads it like any file.
              h('a.btn.btn-sm', {
                href: `/api/dashboard/export?days=${days}&format=csv`, text: 'CSV',
                title: 'Download lead volume as CSV',
              }),
              h('a.btn.btn-sm', {
                href: `/api/dashboard/export?days=${days}&format=xlsx`, text: 'Excel',
                title: 'Download lead volume as an Excel workbook',
              }),
              select([[7, 'Last 7 days'], [30, 'Last 30 days'], [90, 'Last 90 days']], days,
                (e) => ctx.navigate(`#/dashboard?days=${e.target.value}`)),
            ]),
            h('div.panel-body', {}, [
              ledger,
              h('div.ledger-axis', {}, [
                h('span', { text: data.byDay[0]?.day || '' }),
                h('span', { text: `peak ${peak}/day` }),
                h('span', { text: data.byDay[data.byDay.length - 1]?.day || '' }),
              ]),
            ]),
          ]),

          h('section.panel', {}, [
            h('div.panel-head', {}, [h('h2', { text: 'Latest leads' })]),
            data.recent.length
              ? h('table.list', {}, [
                // No click target: the Leads screen this used to open is
                // not in the admin any more, so the row is just a row.
                h('tbody', {}, data.recent.map((l) => h('tr', {}, [
                  h('td.rail', { class: `rail-${l.status}` }),
                  h('td', {}, [
                    h('div.cell-name', { text: l.full_name }),
                    h('div.cell-meta', { text: l.company || l.utm_source || 'direct' }),
                  ]),
                  h('td.cell-mono', { text: relativeTime(l.created_at) }),
                ]))),
              ])
              : h('div.panel-body', {}, h('p.muted', { text: 'No leads yet. Point a form at the intake endpoint to start.' })),
          ]),
        ]),

        h('div.stack', {}, [
          h('section.panel', {}, [
            h('div.panel-head', {}, [h('h2', { text: 'Traffic sources' })]),
            h('table.list', {}, [
              h('thead', {}, h('tr', {}, [
                h('th', { text: 'Source' }), h('th', { text: 'Leads' }), h('th', { text: 'Share' }),
              ])),
              h('tbody', {}, sourceRows),
            ]),
          ]),

          h('section.panel', {}, [
            h('div.panel-head', {}, [h('h2', { text: 'Follow-ups due' })]),
            data.followUps.length
              ? h('table.list', {}, h('tbody', {}, data.followUps.map((l) => h('tr', {}, [
                h('td', {}, [
                  h('div.cell-name', { text: l.full_name }),
                  h('div.cell-meta', { text: STATUS_LABELS[l.status] }),
                ]),
                h('td.cell-mono', { text: formatDate(l.follow_up_on) }),
              ]))))
              : h('div.panel-body', {}, h('p.muted', { text: 'Nothing scheduled in the next 7 days.' })),
          ]),
        ]),
      ]),
    ]);
  }

  // =====================================================================
  // Leads
  // =====================================================================
  async function leads(ctx) {
    const p = ctx.params;
    const filters = {
      status: p.status || '',
      q: p.q || '',
      assigned_to: p.assigned_to || '',
      utm_source: p.utm_source || '',
      from: p.from || '',
      to: p.to || '',
      spam: p.spam || '',
      sort: p.sort || 'created_at',
      dir: p.dir || 'desc',
      page: p.page || 1,
      per_page: p.per_page || 25,
    };

    const exportBtn = h('a.btn', {
      href: `/api/leads/export/csv${api.qs(filters)}`,
      text: 'Export CSV',
    });
    ctx.setHead('Leads', 'Every enquiry, with the campaign that produced it.',
      canEdit(ctx.session.user) ? exportBtn : null);
    mount(ctx.el, spinner());

    const [list, counts, users] = await Promise.all([
      api.get(`/api/leads${api.qs(filters)}`),
      api.get('/api/leads/counts'),
      api.get('/api/users'),
    ]);
    ctx.session.counts = counts;

    const go = (patch) => {
      const next = { ...filters, ...patch };
      if (!('page' in patch)) next.page = 1;
      ctx.navigate(`#/leads${api.qs(next)}`);
    };

    // ---- status tabs
    const tabs = h('div.tabs', { role: 'tablist' }, [
      ['', 'All', counts.all],
      ...STATUS_OPTIONS.map(([value, label]) => [value, label, counts[value]]),
    ].map(([value, label, n]) => h('button', {
      type: 'button',
      role: 'tab',
      'aria-selected': String(filters.status === value),
      onclick: () => go({ status: value }),
    }, [label, ' ', h('span.n', { text: `(${n ?? 0})` })])));

    // ---- filter bar
    const searchInput = h('input', {
      type: 'search', value: filters.q, placeholder: 'Search name, email, phone, message',
      'aria-label': 'Search leads',
      onkeydown: (e) => { if (e.key === 'Enter') go({ q: e.target.value }); },
    });

    const toolbar = h('div.toolbar', {}, [
      h('div.inline-filters', {}, [
        searchInput,
        select([['', 'Anyone'], ['unassigned', 'Unassigned'],
          ...users.users.map((u) => [u.id, u.display_name])],
        filters.assigned_to, (e) => go({ assigned_to: e.target.value }),
        { 'aria-label': 'Filter by owner' }),
        h('input', {
          type: 'date', value: filters.from, 'aria-label': 'From date',
          onchange: (e) => go({ from: e.target.value }),
        }),
        h('input', {
          type: 'date', value: filters.to, 'aria-label': 'To date',
          onchange: (e) => go({ to: e.target.value }),
        }),
        filters.spam === '1'
          ? h('button.btn.btn-sm', { type: 'button', text: 'Hide spam', onclick: () => go({ spam: '' }) })
          : h('button.btn.btn-sm', { type: 'button', text: 'Show spam', onclick: () => go({ spam: '1' }) }),
        (filters.q || filters.assigned_to || filters.from || filters.to)
          ? h('button.btn.btn-sm', {
            type: 'button', text: 'Clear filters',
            onclick: () => go({ q: '', assigned_to: '', from: '', to: '', utm_source: '' }),
          })
          : null,
      ]),
    ]);

    if (!list.leads.length) {
      mount(ctx.el, [tabs, toolbar, emptyState(
        'No leads match this view',
        filters.q || filters.status
          ? 'Try a wider filter, or clear the search.'
          : 'Submissions from your site forms land here the moment they arrive.',
      )]);
      return;
    }

    // ---- bulk actions
    const selected = new Set();
    const bulkBar = h('div.toolbar.hidden', {}, []);

    function renderBulkBar() {
      const n = selected.size;
      bulkBar.classList.toggle('hidden', n === 0);
      if (!n) return;
      mount(bulkBar, [
        h('strong.mono', { text: `${n} selected` }),
        select([['', 'Move to…'], ...STATUS_OPTIONS], '', async (e) => {
          if (!e.target.value) return;
          await runBulk('status', e.target.value);
        }),
        select([['', 'Assign to…'], ['0', 'Nobody'],
          ...users.users.map((u) => [u.id, u.display_name])], '', async (e) => {
          if (e.target.value === '') return;
          await runBulk('assign', e.target.value === '0' ? null : e.target.value);
        }),
        h('button.btn.btn-sm', { type: 'button', text: 'Mark as spam', onclick: () => runBulk('spam') }),
        ['owner', 'admin'].includes(ctx.session.user.role)
          ? h('button.btn.btn-sm.btn-danger', {
            type: 'button', text: 'Delete',
            onclick: () => {
              // eslint-disable-next-line no-alert
              if (window.confirm(`Delete ${n} lead(s)? This cannot be undone.`)) runBulk('delete');
            },
          })
          : null,
      ]);
    }

    async function runBulk(action, value) {
      try {
        const res = await api.post('/api/leads/bulk', { ids: [...selected], action, value });
        toast(`${res.updated} lead${res.updated === 1 ? '' : 's'} updated`);
        ctx.reload();
      } catch (err) { toast(err.message, 'error'); }
    }

    const sortHeader = (key, label) => h('th', {}, h('button', {
      type: 'button',
      text: filters.sort === key ? `${label} ${filters.dir === 'asc' ? '↑' : '↓'}` : label,
      onclick: () => go({ sort: key, dir: filters.sort === key && filters.dir === 'desc' ? 'asc' : 'desc' }),
    }));

    const selectAll = h('input', {
      type: 'checkbox', 'aria-label': 'Select all leads on this page',
      onchange: (e) => {
        selected.clear();
        if (e.target.checked) list.leads.forEach((l) => selected.add(l.id));
        ctx.el.querySelectorAll('input[data-lead]').forEach((box) => { box.checked = e.target.checked; });
        renderBulkBar();
      },
    });

    const rows = list.leads.map((l) => h('tr', {}, [
      h('td.rail', { class: `rail-${l.status}` }),
      h('td.checkcol', {}, h('input', {
        type: 'checkbox', dataset: { lead: l.id }, 'aria-label': `Select ${l.full_name}`,
        onchange: (e) => {
          if (e.target.checked) selected.add(l.id); else selected.delete(l.id);
          renderBulkBar();
        },
      })),
      h('td', {}, [
        h('a', {
          href: `#/leads?open=${l.id}`, class: 'cell-name', text: l.full_name,
          onclick: (e) => { e.preventDefault(); openLead(ctx, l.id); },
        }),
        h('div.cell-meta', { text: [l.company, l.email].filter(Boolean).join(' · ') || '—' }),
      ]),
      h('td', {}, statusPill(l.status)),
      h('td.hide-sm', {}, [
        h('div', { text: l.assignee_name || 'Unassigned', class: l.assignee_name ? '' : 'muted' }),
        l.follow_up_on ? h('div.cell-meta', { text: `Follow up ${formatDate(l.follow_up_on)}` }) : null,
      ]),
      h('td.hide-sm', {}, [
        h('div.cell-mono', { text: l.utm_source || 'direct' }),
        l.utm_campaign ? h('div.cell-meta', { text: l.utm_campaign }) : null,
      ]),
      h('td.cell-mono', { title: formatDate(l.created_at, true), text: relativeTime(l.created_at) }),
    ]));

    const { page, pages, total, perPage } = list.pagination;
    const pager = h('div.pagination', {}, [
      h('span', { text: `${total} lead${total === 1 ? '' : 's'}` }),
      h('div.spacer'),
      select([[10, '10 / page'], [25, '25 / page'], [50, '50 / page'], [100, '100 / page']],
        perPage, (e) => go({ per_page: e.target.value }), { 'aria-label': 'Rows per page' }),
      h('button.btn.btn-sm', {
        type: 'button', text: 'Previous', disabled: page <= 1,
        onclick: () => go({ page: page - 1 }),
      }),
      h('span.mono', { text: `${page} / ${pages || 1}` }),
      h('button.btn.btn-sm', {
        type: 'button', text: 'Next', disabled: page >= pages,
        onclick: () => go({ page: page + 1 }),
      }),
    ]);

    mount(ctx.el, [
      tabs,
      toolbar,
      bulkBar,
      h('table.list', {}, [
        h('thead', {}, h('tr', {}, [
          h('th', { class: 'rail' }),
          h('th.checkcol', {}, selectAll),
          sortHeader('full_name', 'Lead'),
          sortHeader('status', 'Stage'),
          h('th', { class: 'hide-sm', text: 'Owner' }),
          h('th', { class: 'hide-sm', text: 'Source' }),
          sortHeader('created_at', 'Received'),
        ])),
        h('tbody', {}, rows),
      ]),
      pager,
    ]);

    if (p.open) openLead(ctx, p.open);
  }

  // ------------------------------------------------------- lead drawer
  async function openLead(ctx, id) {
    const drawer = openDrawer({ title: 'Loading lead…', body: spinner() });
    let data;
    try {
      data = await api.get(`/api/leads/${id}`);
    } catch (err) {
      toast(err.message, 'error');
      return closeDrawer();
    }

    const lead = data.lead;
    const users = (await api.get('/api/users')).users;
    const editable = canEdit(ctx.session.user);

    const save = async (patch) => {
      try {
        await api.patch(`/api/leads/${id}`, patch);
        toast('Saved');
        ctx.reload({ keepDrawer: true });
      } catch (err) { toast(err.message, 'error'); }
    };

    const pipeline = h('section.panel', {}, [
      h('div.panel-head', {}, [h('h2', { text: 'Pipeline' })]),
      h('div.panel-body', {}, [
        field('Stage', select(STATUS_OPTIONS, lead.status,
          (e) => save({ status: e.target.value }), { disabled: !editable })),
        field('Owner', select([['', 'Unassigned'], ...users.map((u) => [u.id, u.display_name])],
          lead.assigned_to || '', (e) => save({ assigned_to: e.target.value || null }),
          { disabled: !editable })),
        field('Follow up on', h('input', {
          type: 'date', value: lead.follow_up_on ? String(lead.follow_up_on).slice(0, 10) : '',
          disabled: !editable,
          onchange: (e) => save({ follow_up_on: e.target.value || null }),
        })),
        field('Deal value', h('input', {
          type: 'number', min: '0', step: '100', value: lead.value_amount || '',
          disabled: !editable,
          onchange: (e) => save({ value_amount: e.target.value === '' ? null : e.target.value }),
        })),
      ]),
    ]);

    const enquiry = h('section.panel', {}, [
      h('div.panel-head', {}, [h('h2', { text: 'Enquiry' })]),
      h('div.panel-body', {}, [
        h('dl.meta', {}, [
          h('dt', { text: 'Email' }),
          h('dd', {}, lead.email ? h('a', { href: `mailto:${lead.email}`, text: lead.email }) : '—'),
          h('dt', { text: 'Phone' }),
          h('dd', {}, lead.phone ? h('a', { href: `tel:${lead.phone}`, text: lead.phone }) : '—'),
          h('dt', { text: 'Company' }), h('dd', { text: lead.company || '—' }),
          h('dt', { text: 'Form' }), h('dd', { text: lead.form_name || '—' }),
        ]),
        lead.message
          ? h('p', { text: lead.message, style: 'margin:12px 0 0;white-space:pre-wrap' })
          : null,
      ]),
    ]);

    const attribution = h('section.panel', {}, [
      h('div.panel-head', {}, [h('h2', { text: 'Attribution' })]),
      h('div.panel-body', {}, h('dl.meta', {}, [
        h('dt', { text: 'Landing page' }), h('dd', { class: 'mono', text: lead.source_page || '—' }),
        h('dt', { text: 'Referrer' }), h('dd', { class: 'mono', text: lead.referrer || 'direct' }),
        h('dt', { text: 'Campaign' }),
        h('dd', {
          class: 'mono',
          text: [lead.utm_source, lead.utm_medium, lead.utm_campaign].filter(Boolean).join(' / ') || '—',
        }),
        h('dt', { text: 'Term / content' }),
        h('dd', { class: 'mono', text: [lead.utm_term, lead.utm_content].filter(Boolean).join(' / ') || '—' }),
        h('dt', { text: 'Received' }), h('dd', { text: formatDate(lead.created_at, true) }),
        h('dt', { text: 'IP' }), h('dd', { class: 'mono', text: lead.ip || '—' }),
      ])),
    ]);

    const noteList = h('div', {}, data.notes.map((n) => h('div.note', {}, [
      h('div', { text: n.body, style: 'white-space:pre-wrap' }),
      h('div.note-meta', { text: `${n.author || 'system'} · ${formatDate(n.created_at, true)}` }),
    ])));

    const noteInput = h('textarea', { placeholder: 'What happened on the call?', disabled: !editable });
    const notes = h('section.panel', {}, [
      h('div.panel-head', {}, [h('h2', { text: 'Notes' })]),
      h('div.panel-body', {}, [
        editable ? h('div', {}, [
          noteInput,
          h('button.btn.btn-primary.btn-sm', {
            type: 'button', text: 'Add note', style: 'margin-top:8px',
            onclick: async (e) => {
              const body = noteInput.value.trim();
              if (!body) return;
              e.target.disabled = true;
              try {
                const res = await api.post(`/api/leads/${id}/notes`, { body });
                noteInput.value = '';
                noteList.prepend(h('div.note', {}, [
                  h('div', { text: res.note.body, style: 'white-space:pre-wrap' }),
                  h('div.note-meta', { text: `${res.note.author} · just now` }),
                ]));
                toast('Note added');
              } catch (err) { toast(err.message, 'error'); }
              e.target.disabled = false;
            },
          }),
        ]) : null,
        data.notes.length || editable ? noteList : h('p.muted', { text: 'No notes yet.' }),
      ]),
    ]);

    mount(drawer.body, [pipeline, enquiry, attribution, notes]);
    document.querySelector('.drawer-head h2').textContent = lead.full_name;
    const sub = document.querySelector('.drawer-head .page-sub');
    const subText = lead.company || lead.email || '—';
    if (sub) sub.textContent = subText;
    else document.querySelector('.drawer-head h2').after(h('p.page-sub', { text: subText }));
  }

  // =====================================================================
  // Users
  // =====================================================================
  async function users(ctx) {
    const isAdmin = ['owner', 'admin'].includes(ctx.session.user.role);

    const addBtn = h('button.btn.btn-primary', {
      type: 'button', text: 'Add user', onclick: () => userForm(ctx),
    });
    ctx.setHead('Users', 'Who can see the pipeline, and what they can change.',
      isAdmin ? addBtn : null);
    mount(ctx.el, spinner());

    const { users: list } = await api.get('/api/users');
    mount(ctx.el, h('table.list', {}, [
      h('thead', {}, h('tr', {}, [
        h('th', { text: 'Name' }), h('th', { text: 'Role' }),
        h('th', { class: 'hide-sm', text: 'Open leads' }),
        h('th', { class: 'hide-sm', text: 'Last signed in' }),
        h('th', { text: '' }),
      ])),
      h('tbody', {}, list.map((u) => h('tr', {}, [
        h('td', {}, [
          h('div.cell-name', { text: u.display_name }),
          h('div.cell-meta', { text: u.email }),
        ]),
        h('td', {}, [
          h('span', { text: u.role }),
          u.is_active ? null : h('div.cell-meta', { text: 'Deactivated' }),
        ]),
        h('td.cell-mono.hide-sm', { text: String(u.open_leads) }),
        h('td.cell-mono.hide-sm', { text: u.last_login_at ? relativeTime(u.last_login_at) : 'never' }),
        h('td', {}, isAdmin ? h('button.btn.btn-sm', {
          type: 'button', text: 'Edit', onclick: () => userForm(ctx, u),
        }) : null),
      ]))),
    ]));
  }

  function userForm(ctx, user) {
    const isNew = !user;
    const name = h('input', { type: 'text', value: user?.display_name || '' });
    const emailInput = h('input', { type: 'email', value: user?.email || '', disabled: !isNew });
    const role = select([['viewer', 'Viewer — read only'], ['agent', 'Agent — work leads'],
      ['admin', 'Admin — manage settings'], ['owner', 'Owner — full control']],
    user?.role || 'agent');
    const password = h('input', { type: 'password', autocomplete: 'new-password' });
    const active = h('input', { type: 'checkbox', checked: user ? user.is_active : true });

    const submit = h('button.btn.btn-primary', {
      type: 'button',
      text: isNew ? 'Create user' : 'Save changes',
      onclick: async () => {
        submit.disabled = true;
        const payload = {
          display_name: name.value.trim(),
          role: role.value,
          is_active: active.checked,
        };
        if (isNew) { payload.email = emailInput.value.trim(); payload.password = password.value; }
        else if (password.value) payload.password = password.value;

        try {
          if (isNew) await api.post('/api/users', payload);
          else await api.patch(`/api/users/${user.id}`, payload);
          toast(isNew ? 'User created' : 'User updated');
          closeDrawer();
          ctx.reload();
        } catch (err) { toast(err.message, 'error'); submit.disabled = false; }
      },
    });

    openDrawer({
      title: isNew ? 'Add user' : user.display_name,
      subtitle: isNew ? 'They will sign in with this email on your workspace.' : user.email,
      body: [
        field('Name', name),
        isNew ? field('Email', emailInput) : null,
        field('Role', role),
        field(isNew ? 'Password (12+ characters)' : 'New password (leave blank to keep)', password),
        h('label.field', {}, [h('span', { text: 'Access' }),
          h('div', {}, [active, ' ', 'Can sign in'])]),
        submit,
      ].filter(Boolean),
    });
  }

  // =====================================================================
  // Webhooks
  // =====================================================================
  async function webhooks(ctx) {
    const addBtn = h('button.btn.btn-primary', {
      type: 'button', text: 'Add endpoint', onclick: () => webhookForm(ctx),
    });
    ctx.setHead('Webhooks', 'Push new leads to Zoho, HubSpot, Zapier or n8n as they arrive.', addBtn);
    mount(ctx.el, spinner());

    const { endpoints, deliveries } = await api.get('/api/webhooks');

    const endpointTable = endpoints.length
      ? h('table.list', {}, [
        h('thead', {}, h('tr', {}, [
          h('th', { text: 'Endpoint' }), h('th', { text: 'Events' }),
          h('th', { text: 'Queue' }), h('th', { text: '' }),
        ])),
        h('tbody', {}, endpoints.map((e) => h('tr', {}, [
          h('td', {}, [
            h('div.cell-name', { text: e.name }),
            h('div.cell-meta', { text: e.url }),
          ]),
          h('td.cell-mono', { text: e.events.join(', ') }),
          h('td.cell-mono', { text: `${e.pending} pending · ${e.failed} failed` }),
          h('td', {}, h('button.btn.btn-sm.btn-danger', {
            type: 'button', text: 'Remove',
            onclick: async () => {
              // eslint-disable-next-line no-alert
              if (!window.confirm(`Remove "${e.name}"? Queued deliveries are dropped.`)) return;
              try { await api.del(`/api/webhooks/${e.id}`); toast('Endpoint removed'); ctx.reload(); }
              catch (err) { toast(err.message, 'error'); }
            },
          })),
        ]))),
      ])
      : emptyState('No endpoints yet',
        'Add one to forward every new lead to your CRM within seconds.');

    const deliveryTable = h('section.panel', {}, [
      h('div.panel-head', {}, [h('h2', { text: 'Recent deliveries' })]),
      deliveries.length
        ? h('table.list', {}, [
          h('thead', {}, h('tr', {}, [
            h('th', { text: 'Event' }), h('th', { text: 'Endpoint' }),
            h('th', { text: 'Status' }), h('th', { text: 'When' }),
          ])),
          h('tbody', {}, deliveries.map((d) => h('tr', {}, [
            h('td.cell-mono', { text: d.event }),
            h('td', { text: d.endpoint }),
            h('td', {}, [
              h('span', { text: d.status }),
              d.last_error ? h('div.cell-meta', { text: `${d.attempts} tries · ${d.last_error}` }) : null,
            ]),
            h('td.cell-mono', { text: relativeTime(d.created_at) }),
          ]))),
        ])
        : h('div.panel-body', {}, h('p.muted', { text: 'Nothing sent yet.' })),
    ]);

    mount(ctx.el, [endpointTable, h('div', { style: 'height:18px' }), deliveryTable]);
  }

  function webhookForm(ctx) {
    const name = h('input', { type: 'text', placeholder: 'Zoho CRM — leads' });
    const url = h('input', { type: 'url', placeholder: 'https://hooks.example.com/leads' });
    const events = ['lead.created', 'lead.status_changed'].map((ev) => ({
      ev, box: h('input', { type: 'checkbox', checked: ev === 'lead.created' }),
    }));

    const submit = h('button.btn.btn-primary', {
      type: 'button', text: 'Create endpoint',
      onclick: async () => {
        submit.disabled = true;
        try {
          const res = await api.post('/api/webhooks', {
            name: name.value.trim(),
            url: url.value.trim(),
            events: events.filter((e) => e.box.checked).map((e) => e.ev),
          });
          closeDrawer();
          ctx.reload();
          // Shown once. It is never returned by the list endpoint again.
          setTimeout(() => {
            const host = document.getElementById('view');
            host.prepend(h('div.notice', {}, [
              h('strong', { text: 'Copy your signing secret now — it is not shown again. ' }),
              h('code', { text: res.secret }),
            ]));
          }, 60);
        } catch (err) { toast(err.message, 'error'); submit.disabled = false; }
      },
    });

    openDrawer({
      title: 'Add webhook endpoint',
      subtitle: 'Requests are signed with HMAC-SHA256 and retried with backoff.',
      body: [
        field('Name', name),
        field('URL (https only)', url),
        h('label.field', {}, [h('span', { text: 'Send these events' }),
          h('div', {}, events.map((e) => h('div', {}, [e.box, ' ', e.ev])))]),
        submit,
      ],
    });
  }

  // =====================================================================
  // Activity
  // =====================================================================
  async function activity(ctx) {
    ctx.setHead('Activity', 'Audit trail for everything that changed.');
    mount(ctx.el, spinner());

    const { activity: rows } = await api.get('/api/activity?limit=100');
    if (!rows.length) return mount(ctx.el, emptyState('Nothing logged yet', 'Actions appear here as your team works.'));

    mount(ctx.el, h('table.list', {}, [
      h('thead', {}, h('tr', {}, [
        h('th', { text: 'Action' }), h('th', { text: 'By' }),
        h('th', { class: 'hide-sm', text: 'Detail' }), h('th', { text: 'When' }),
      ])),
      h('tbody', {}, rows.map((a) => h('tr', {}, [
        h('td.cell-mono', { text: a.action }),
        h('td', { text: a.actor || 'system' }),
        h('td.cell-meta.hide-sm', {
          text: Object.entries(a.meta || {}).map(([k, v]) => `${k}: ${v}`).join(' · ') || '—',
        }),
        h('td.cell-mono', { title: formatDate(a.created_at, true), text: relativeTime(a.created_at) }),
      ]))),
    ]));
  }

  // =====================================================================
  // Settings
  // =====================================================================
  async function settings(ctx) {
    ctx.setHead('Settings', 'Workspace, forms and notifications.');
    mount(ctx.el, spinner());

    const data = await api.get('/api/settings');
    const isAdmin = ['owner', 'admin'].includes(ctx.session.user.role);
    const origin = window.location.origin;

    const notifyInput = h('input', {
      type: 'text',
      value: (data.settings.notify_emails || []).join(', '),
      placeholder: 'leads@arcitech.ai, sales@arcitech.ai',
      disabled: !isAdmin,
    });

    const formRows = data.forms.map((f) => h('tr', {}, [
      h('td', {}, [
        h('div.cell-name', { text: f.name }),
        h('div.cell-meta', { text: f.is_active ? 'Accepting submissions' : 'Paused' }),
      ]),
      h('td.cell-mono', {
        text: `POST ${origin}/api/public/${data.tenant.slug}/forms/${f.slug}`,
      }),
    ]));

    mount(ctx.el, [
      h('section.panel', {}, [
        h('div.panel-head', {}, [h('h2', { text: 'Workspace' })]),
        h('div.panel-body', {}, h('dl.meta', {}, [
          h('dt', { text: 'Name' }), h('dd', { text: data.tenant.name }),
          h('dt', { text: 'Slug' }), h('dd', { class: 'mono', text: data.tenant.slug }),
          h('dt', { text: 'Your role' }), h('dd', { text: ctx.session.user.role }),
        ])),
      ]),

      h('div', { style: 'height:18px' }),

      h('section.panel', {}, [
        h('div.panel-head', {}, [h('h2', { text: 'Lead notifications' })]),
        h('div.panel-body', {}, [
          field('Email these addresses when a lead arrives', notifyInput),
          isAdmin ? h('button.btn.btn-primary.btn-sm', {
            type: 'button', text: 'Save changes',
            onclick: async () => {
              const value = notifyInput.value.split(',').map((s) => s.trim()).filter(Boolean);
              try {
                await api.put('/api/settings/notify_emails', { value });
                toast('Notification list saved');
              } catch (err) { toast(err.message, 'error'); }
            },
          }) : h('p.muted', { text: 'Admins can change this.' }),
        ]),
      ]),

      h('div', { style: 'height:18px' }),

      h('section.panel', {}, [
        h('div.panel-head', {}, [h('h2', { text: 'Form endpoints' })]),
        formRows.length
          ? h('table.list', {}, [
            h('thead', {}, h('tr', {}, [h('th', { text: 'Form' }), h('th', { text: 'Post to' })])),
            h('tbody', {}, formRows),
          ])
          : h('div.panel-body', {}, h('p.muted', { text: 'No forms defined yet.' })),
      ]),
    ]);
  }

  window.views = { dashboard, leads, users, webhooks, activity, settings };
}());
