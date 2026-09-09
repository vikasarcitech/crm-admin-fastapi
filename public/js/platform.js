/* global window, api, kit, ui, navigator */
/**
 * The multi-site control plane.
 *
 * Only reachable with `sites.manage` (Super Admin by default), which is
 * why it is a separate screen rather than a tab inside Site settings:
 * everything here is cross-tenant, and a site's own Owner should not
 * see the portfolio at all.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, search, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool, number, bytes, kpi,
    textInput, textarea, checkbox, formatDate, relativeTime, openDrawer } = kit;

  const STATUS_KINDS = { active: 'ok', suspended: 'warn', archived: 'off' };

  async function platform(ctx) {
    const tab = ctx.params.tab || 'sites';
    mount(ctx.el, ui.spinner());

    let data;
    try {
      data = await api.get(`/api/platform/sites${api.qs({
        q: ctx.params.q, include_archived: ctx.params.archived,
      })}`);
    } catch (err) {
      if (err.status === 403) {
        mount(ctx.el, ui.emptyState('Portfolio administration',
          'This screen needs the “sites.manage” permission, which only a Super Admin '
          + 'holds by default.'));
        ctx.setHead('Platform', 'Cross-site administration');
        return;
      }
      throw err;
    }

    ctx.setHead('Platform',
      `${data.totals.active} active · ${data.totals.suspended} suspended · `
      + `${number(data.totals.leads30d)} leads/30d · ${bytes(data.totals.mediaBytes)} media`,
      h('button.btn.btn-primary', {
        type: 'button', text: 'New site', onclick: () => createSite(ctx, data),
      }));

    const strip = tabs(ctx, [
      ['sites', 'Sites', data.totals.sites],
      ['people', 'People'],
      ['audit', 'Site audit'],
      ['isolation', 'Isolation'],
    ], tab);

    const panes = {
      sites: () => sitesPane(ctx, data),
      people: () => peoplePane(ctx),
      audit: () => auditPane(ctx),
      isolation: () => isolationPane(ctx),
    };
    mount(ctx.el, [strip, await (panes[tab] || panes.sites)()]);
  }

  // =============================================================== sites
  function sitesPane(ctx, data) {
    const warn = data.isolation && !data.isolation.effective ? data.isolation.warning : null;

    return h('div.stack', {}, [
      warn ? notice(`Tenant isolation: ${warn}`, 'warn') : null,
      h('div.kpis', {}, [
        kpi('Sites', data.totals.sites, `${data.totals.active} serving`),
        kpi('Suspended', data.totals.suspended),
        kpi('Leads, 30d', number(data.totals.leads30d)),
        kpi('Page views, 30d', number(data.totals.pageViews30d)),
        kpi('Media stored', bytes(data.totals.mediaBytes)),
      ]),
      panel(null, [
        toolbar([
          search(ctx.params.q, (q) =>
            ctx.navigate(`#/platform${api.qs({ ...ctx.params, q })}`),
          'Search name, slug or domain…'),
          h('div.spacer'),
          h('button.btn.btn-sm', {
            type: 'button',
            text: ctx.params.archived ? 'Hiding nothing' : 'Show archived',
            onclick: () => ctx.navigate(`#/platform${api.qs({
              ...ctx.params, archived: ctx.params.archived ? '' : '1',
            })}`),
          }),
        ]),
        table([
          {
            label: 'Site',
            cell: (s) => [
              h('div.cell-name', { text: s.name }),
              h('div.cell-meta', {
                text: [s.slug, s.primary_domain || 'no domain',
                  `${s.domain_count} domain(s)`].join(' · '),
              }),
            ],
          },
          {
            label: 'Status',
            cell: (s) => h('span', {}, [
              badge(s.status, STATUS_KINDS[s.status] || 'neutral'),
              s.overLimit?.length
                ? badge(`${s.overLimit.length} over limit`, 'warn')
                : null,
            ]),
          },
          { label: 'Plan', cell: (s) => s.plan },
          { label: 'Users', class: 'cell-mono', cell: (s) => number(s.users) },
          { label: 'Content', class: 'cell-mono', cell: (s) => number(s.content_items) },
          { label: 'Leads 30d', class: 'cell-mono', cell: (s) => number(s.leads_30d) },
          { label: 'Media', class: 'cell-mono', cell: (s) => bytes(s.media_bytes || 0) },
          {
            label: 'Usage as of',
            class: 'cell-mono',
            cell: (s) => (s.computed_at ? relativeTime(s.computed_at) : 'never'),
          },
        ], data.sites, {
          empty: 'No sites match.',
          onRow: (s) => openSite(ctx, s.slug, data),
        }),
      ]),
      panelBody(h('p.muted', {
        text: 'Usage is a rollup refreshed hourly, so a number here can lag. Quota '
          + 'gates count live, so a stale rollup never lets a site past its ceiling.',
      })),
    ]);
  }

  function createSite(ctx, data) {
    const limitInputs = {};
    const limitFields = data.limitKeys.map((key) => {
      const input = h('input', {
        type: 'number', min: 0,
        placeholder: String(data.defaultLimits[key]),
      });
      limitInputs[key] = input;
      return h('label.field', {}, [
        h('span', { text: key.replace(/_/g, ' ') }),
        input,
        h('small.muted', { text: `platform default ${number(data.defaultLimits[key])}` }),
      ]);
    });

    formDrawer({
      title: 'New site',
      subtitle: 'Creates the tenant, its owner, a starter form and every default.',
      fields: [
        { name: 'name', label: 'Site name', control: textInput('name', '') },
        { name: 'slug', label: 'Slug', control: textInput('slug', ''),
          help: 'Used in API paths. Derived from the name when blank; a clash gets '
            + 'a numbered suffix.' },
        { name: 'domain', label: 'Primary domain', control: textInput('domain', '',
          { placeholder: 'example.com' }),
        help: 'Optional, and trusted immediately — you are the platform '
            + 'administrator. Added domains need verifying.' },
        { name: 'owner_email', label: 'Owner email', control: textInput('owner_email', '') },
        { name: 'owner_name', label: 'Owner name', control: textInput('owner_name', '') },
        { name: 'owner_password', label: 'Owner password',
          control: h('input', { name: 'owner_password', type: 'password' }),
          help: 'At least 12 characters. There is no invite email yet, so you hand '
            + 'this over — the owner should change it on first sign-in.' },
        { name: 'plan', label: 'Plan', control: textInput('plan', 'standard') },
        { name: 'notes', label: 'Notes', control: textarea('notes', '', { rows: 2 }) },
        { label: 'Limits', control: h('div.stack', {}, [
          h('p.muted', { text: 'Blank uses the platform default. Raising one site '
            + 'later is a single change to that site.' }),
          ...limitFields,
        ]) },
      ],
      onSave: async (values) => {
        const limits = {};
        Object.entries(limitInputs).forEach(([key, input]) => {
          if (input.value !== '') limits[key] = Number(input.value);
        });
        const result = await api.post('/api/platform/sites', {
          name: values.name,
          slug: values.slug || null,
          domain: values.domain || null,
          owner_email: values.owner_email,
          owner_name: values.owner_name || null,
          owner_password: values.owner_password,
          plan: values.plan || 'standard',
          notes: values.notes || null,
          limits,
        });
        toast(`“${result.tenant.name}” created at /${result.tenant.slug}.`);
        ctx.reload();
      },
      saveLabel: 'Create and provision',
    });
  }

  // ========================================================= site detail
  async function openSite(ctx, slug, portfolio) {
    const d = await api.get(`/api/platform/sites/${slug}`);
    const site = d.site;

    const limitRow = (line) => h('div.check-row', {}, [
      h('span.check-dot', { dataset: { state: line.overLimit ? 'off' : 'ok' } }),
      h('span', { text: line.key.replace(/_/g, ' ') }),
      h('span.cell-mono', {
        text: line.key === 'media_bytes'
          ? `${bytes(line.used)} / ${bytes(line.limit)}`
          : `${number(line.used)} / ${number(line.limit)}`,
      }),
      line.percent !== null ? h('span.muted', { text: `${line.percent}%` }) : null,
    ]);

    openDrawer({
      title: site.name,
      subtitle: `${site.slug} · ${site.status} · plan ${site.plan}`,
      actions: h('div.row-actions', {}, [
        actionButton('Switch into', async () => {
          await api.post(`/api/platform/switch?tenant_slug=${site.slug}`, {});
          toast(`Switched to ${site.name}. This is logged in its audit trail.`);
          window.location.hash = '#/dashboard';
          window.location.reload();
        }, { small: true }),
      ]),
      body: [
        site.status === 'suspended'
          ? notice(`Suspended${site.suspended_reason ? `: ${site.suspended_reason}` : ''}. `
            + 'Its public API answers 503 and nobody can sign in.', 'warn')
          : null,
        site.status === 'archived'
          ? notice('Archived. Invisible publicly and ready to delete permanently.', 'warn')
          : null,
        d.anyOverLimit ? notice('This site is at or over one of its limits.', 'warn') : null,

        panel('Usage against limits', panelBody(d.limits.map(limitRow))),

        panel('Frontend connection', panelBody([
          h('p.muted', { text: 'What this site’s static frontend calls. Each also works '
            + 'without the slug when requested on a verified domain.' }),
          ...Object.entries(d.frontend)
            .filter(([, v]) => typeof v === 'string')
            .map(([key, value]) => h('div.field-inline', {}, [
              h('span.muted', { text: key }),
              h('input', { type: 'text', value, readonly: 'readonly' }),
              h('button.btn.btn-sm', {
                type: 'button', text: 'Copy',
                onclick: () => {
                  navigator.clipboard?.writeText(window.location.origin + value)
                    .catch(() => {});
                  toast('Copied.');
                },
              }),
            ])),
        ])),

        panel('Domains', [
          table([
            { label: 'Domain', cell: (x) => h('code', { text: x.domain }) },
            { label: 'Primary', cell: (x) => bool(x.is_primary, 'Primary', '—') },
            { label: 'Verified', cell: (x) => bool(x.is_verified, 'Verified', 'Pending') },
            {
              label: '',
              cell: (x) => h('div.row-actions', {}, [
                x.is_verified ? null : actionButton('Mark verified', async () => {
                  await api.post(
                    `/api/platform/sites/${site.slug}/domains/${x.id}/verify`, {});
                  toast('Verified. It now resolves and is trusted for CORS.');
                  ctx.reload();
                }, { small: true }),
                x.is_verified && !x.is_primary
                  ? actionButton('Make primary', async () => {
                    await api.post(
                      `/api/platform/sites/${site.slug}/domains/${x.id}/primary`, {});
                    toast('Primary domain set.');
                    ctx.reload();
                  }, { small: true })
                  : null,
                confirmButton('Remove', async () => {
                  await api.del(`/api/platform/sites/${site.slug}/domains/${x.id}`);
                  toast('Domain removed.');
                  ctx.reload();
                }, { small: true }),
              ]),
            },
          ], d.domains, { empty: 'No domains. The site is reachable by slug only.' }),
          panelBody([
            h('p.muted', { text: 'An unverified domain is recorded but inert — it will '
              + 'not resolve and is not trusted for CORS, so one client cannot point a '
              + 'hostname at another client’s content.' }),
            actionButton('Add domain', () => addDomain(ctx, site.slug), { small: true }),
          ]),
        ]),

        panel('People', table([
          { label: 'User', cell: (u) => [h('div.cell-name', { text: u.display_name }),
            h('div.cell-meta', { text: u.email })] },
          { label: 'Role', cell: (u) => badge(u.role, 'neutral') },
          { label: 'Active', cell: (u) => bool(u.is_active, 'Yes', 'No') },
          { label: 'Last signed in', class: 'cell-mono',
            cell: (u) => relativeTime(u.last_login_at) },
        ], d.users, { empty: 'No users.' })),

        d.members.length
          ? panel('Granted extra access', table([
            { label: 'User', cell: (m) => m.display_name },
            { label: 'Role', cell: (m) => badge(m.role, 'neutral') },
            {
              label: '',
              cell: (m) => confirmButton('Revoke', async () => {
                const r = await api.del(`/api/platform/members/${m.id}`);
                toast(`Revoked. ${r.sessionsEnded} session(s) on this site ended.`);
                ctx.reload();
              }, { small: true }),
            },
          ], d.members))
          : null,

        panel('Lifecycle', panelBody([
          h('div.row-actions', {}, [
            site.status === 'active'
              ? actionButton('Suspend…', () => suspendSite(ctx, site), {})
              : actionButton('Resume', async () => {
                await api.put(`/api/platform/sites/${site.slug}/status`,
                  { status: 'active' });
                toast('Resumed.');
                ctx.reload();
              }, { primary: true }),
            site.status !== 'archived'
              ? confirmButton('Archive', async () => {
                await api.put(`/api/platform/sites/${site.slug}/status`,
                  { status: 'archived' });
                toast('Archived. It is now invisible publicly.');
                ctx.reload();
              }, { danger: false })
              : null,
            actionButton('Edit limits', () => editLimits(ctx, site, portfolio), {}),
            actionButton('Edit details', () => editSite(ctx, site), {}),
            actionButton('Refresh usage', async () => {
              await api.post(`/api/platform/sites/${site.slug}/refresh-usage`, {});
              toast('Usage recomputed.');
              ctx.reload();
            }, {}),
          ]),
          site.status === 'archived'
            ? h('div.stack', {}, [
              h('hr'),
              notice('Deleting removes every user, lead, page and stored file for this '
                + 'site. There is no undo.', 'error'),
              actionButton('Delete permanently…', () => deleteSite(ctx, site), {}),
            ])
            : h('p.muted', { text: 'A site must be archived before it can be deleted.' }),
        ])),

        panel('Site audit', table([
          { label: 'Action', cell: (e) => badge(e.action, 'neutral') },
          { label: 'By', cell: (e) => e.actor_email || 'system' },
          { label: 'Detail', cell: (e) => JSON.stringify(e.detail).slice(0, 70) },
          { label: 'When', class: 'cell-mono', cell: (e) => relativeTime(e.created_at) },
        ], d.events, { empty: 'No lifecycle events.' })),
      ],
    });
  }

  function addDomain(ctx, slug) {
    formDrawer({
      title: 'Add a domain',
      fields: [
        { name: 'domain', label: 'Hostname', control: textInput('domain', '',
          { placeholder: 'www.example.com' }),
        help: 'Bare hostname — no scheme, port or path.' },
        { name: 'make_primary', label: 'Primary',
          control: checkbox('make_primary', false, 'Make this the primary domain'),
          help: 'Only possible once it is verified.' },
      ],
      onSave: async (values) => {
        const r = await api.post(`/api/platform/sites/${slug}/domains`, {
          domain: values.domain, make_primary: Boolean(values.make_primary),
        });
        toast(`Added. Verify it with token ${r.domain.verify_token.slice(0, 8)}… `
          + 'before it resolves.');
        ctx.reload();
      },
    });
  }

  function suspendSite(ctx, site) {
    formDrawer({
      title: `Suspend ${site.name}`,
      subtitle: 'Ends every live session and answers 503 on the public API.',
      fields: [
        { name: 'reason', label: 'Reason', control: textInput('reason', ''),
          help: 'Shown in the audit trail and appended to the public 503 message.' },
      ],
      onSave: async (values) => {
        const r = await api.put(`/api/platform/sites/${site.slug}/status`, {
          status: 'suspended', reason: values.reason || null,
        });
        toast(`Suspended. ${r.sessionsEnded} session(s) ended.`);
        ctx.reload();
      },
      saveLabel: 'Suspend',
    });
  }

  function editLimits(ctx, site, portfolio) {
    const keys = portfolio?.limitKeys || Object.keys(site.limits || {});
    const defaults = portfolio?.defaultLimits || {};
    const inputs = {};
    const fields = keys.map((key) => {
      const input = h('input', {
        type: 'number', min: 0,
        value: (site.limits || {})[key] ?? '',
        placeholder: String(defaults[key] ?? ''),
      });
      inputs[key] = input;
      return { label: key.replace(/_/g, ' '), control: input,
        help: `platform default ${number(defaults[key])}` };
    });

    formDrawer({
      title: `Limits for ${site.name}`,
      subtitle: 'Blank falls back to the platform default. This is how one site is '
        + 'scaled without touching the others.',
      fields,
      onSave: async () => {
        const limits = {};
        Object.entries(inputs).forEach(([key, input]) => {
          if (input.value !== '') limits[key] = Number(input.value);
        });
        await api.put(`/api/platform/sites/${site.slug}/limits`, { limits });
        toast('Limits saved.');
        ctx.reload();
      },
    });
  }

  function editSite(ctx, site) {
    const infra = site.infra || {};
    const infraKeys = ['media_s3_bucket', 'media_s3_prefix', 'media_public_base_url',
      'cloudfront_distribution_id', 'build_target', 'region'];
    const infraInputs = {};
    const infraFields = infraKeys.map((key) => {
      const input = textInput(key, infra[key]);
      infraInputs[key] = input;
      return h('label.field', {}, [h('span', { text: key.replace(/_/g, ' ') }), input]);
    });

    formDrawer({
      title: `Edit ${site.name}`,
      fields: [
        { name: 'name', label: 'Name', control: textInput('name', site.name) },
        { name: 'plan', label: 'Plan', control: textInput('plan', site.plan) },
        { name: 'notes', label: 'Notes', control: textarea('notes', site.notes, { rows: 3 }) },
        { label: 'Infrastructure overrides', control: h('div.stack', {}, [
          h('p.muted', { text: 'Per-site bucket, CDN and build target. How a busy site '
            + 'gets its own storage without a platform change. Blank uses the '
            + 'platform-wide environment setting.' }),
          ...infraFields,
        ]) },
      ],
      onSave: async (values) => {
        const infraOut = {};
        Object.entries(infraInputs).forEach(([key, input]) => {
          if (input.value.trim()) infraOut[key] = input.value.trim();
        });
        await api.patch(`/api/platform/sites/${site.slug}`, {
          name: values.name, plan: values.plan,
          notes: values.notes || null, infra: infraOut,
        });
        toast('Saved.');
        ctx.reload();
      },
    });
  }

  function deleteSite(ctx, site) {
    formDrawer({
      title: `Delete ${site.name}`,
      subtitle: 'Every user, lead, page and stored file. No undo.',
      fields: [
        { name: 'confirm', label: `Type “${site.slug}” to confirm`,
          control: textInput('confirm', '') },
      ],
      onSave: async (values) => {
        if (values.confirm.trim() !== site.slug) {
          throw new Error(`Type ${site.slug} exactly to confirm.`);
        }
        const r = await api.del(
          `/api/platform/sites/${site.slug}?confirm_slug=${encodeURIComponent(site.slug)}`);
        toast(`Deleted. ${r.objectsDeleted} stored file(s) removed.`);
        ctx.reload();
      },
      saveLabel: 'Delete permanently',
    });
  }

  // ============================================================== people
  async function peoplePane(ctx) {
    const { users } = await api.get(`/api/platform/users${api.qs({ q: ctx.params.pq })}`);
    return h('div.stack', {}, [
      panel(null, [
        toolbar([
          search(ctx.params.pq, (pq) =>
            ctx.navigate(`#/platform${api.qs({ ...ctx.params, pq })}`),
          'Search name or email…'),
          h('div.spacer'),
          actionButton('Grant site access', () => grantAccess(ctx),
            { small: true, primary: true }),
        ]),
        table([
          { label: 'User', cell: (u) => [h('div.cell-name', { text: u.display_name }),
            h('div.cell-meta', { text: u.email })] },
          { label: 'Home site', cell: (u) => [h('div', { text: u.home_site_name }),
            h('div.cell-meta', { text: u.home_site })] },
          { label: 'Role', cell: (u) => badge(u.home_role, 'neutral') },
          { label: 'Extra sites', class: 'cell-mono', cell: (u) => number(u.extra_sites) },
          { label: '2FA', cell: (u) => bool(u.has_2fa, 'On', 'Off') },
          { label: 'Sessions', class: 'cell-mono', cell: (u) => number(u.live_sessions) },
          { label: 'Active', cell: (u) => bool(u.is_active, 'Yes', 'No') },
          { label: 'Last seen', class: 'cell-mono', cell: (u) => relativeTime(u.last_login_at) },
        ], users, { empty: 'No accounts match.' }),
      ]),
      panelBody(h('p.muted', {
        text: 'One account belongs to one home site. Extra sites are memberships — '
          + 'revoking one ends that account’s sessions on that site immediately.',
      })),
    ]);
  }

  function grantAccess(ctx) {
    formDrawer({
      title: 'Grant site access',
      subtitle: 'Gives an existing account a role on another site.',
      fields: [
        { name: 'user_email', label: 'Account email', control: textInput('user_email', '') },
        { name: 'tenant_slug', label: 'Site slug', control: textInput('tenant_slug', '') },
        { name: 'role', label: 'Role on that site',
          control: select([['admin', 'Admin'], ['editor', 'Editor'], ['author', 'Author'],
            ['contributor', 'Contributor'], ['agent', 'Agent (CRM)'],
            ['viewer', 'Viewer']], 'editor', null) },
      ],
      onSave: async (values) => {
        await api.post('/api/platform/members', {
          user_email: values.user_email,
          tenant_slug: values.tenant_slug,
          role: values.role,
        });
        toast('Access granted.');
        ctx.reload();
      },
      saveLabel: 'Grant',
    });
  }

  // =============================================================== audit
  async function auditPane(ctx) {
    const { events } = await api.get('/api/platform/events?limit=200');
    return h('div.stack', {}, [
      panel(null, table([
        { label: 'Action', cell: (e) => badge(e.action, 'neutral') },
        {
          label: 'Site',
          cell: (e) => h('span', {}, [
            h('span', { text: e.tenant_slug }),
            e.site_gone ? badge('deleted', 'off') : null,
          ]),
        },
        { label: 'By', cell: (e) => e.actor_email || 'system' },
        { label: 'Detail', cell: (e) => JSON.stringify(e.detail || {}).slice(0, 80) },
        { label: 'IP', class: 'cell-mono', cell: (e) => e.ip || '—' },
        { label: 'When', class: 'cell-mono', cell: (e) => formatDate(e.created_at, true) },
      ], events, { empty: 'No site lifecycle events yet.' })),
      panelBody(h('p.muted', {
        text: 'Separate from each site’s own activity log, because a hard delete '
          + 'cascades that away — the record of a deletion has to outlive the site.',
      })),
    ]);
  }

  // =========================================================== isolation
  async function isolationPane(ctx) {
    const r = await api.get('/api/platform/isolation');

    const layer = (title, ok, rows, note) => panel(title, panelBody([
      h('div.check-row', {}, [
        h('span.check-dot', { dataset: { state: ok ? 'ok' : 'off' } }),
        h('strong', { text: ok ? 'Enforced' : 'Not enforced' }),
      ]),
      ...rows.map(([k, v]) => h('div.field-inline', {}, [
        h('span.muted', { text: k }),
        h('span.cell-mono', { text: String(v) }),
      ])),
      note ? h('p.muted', { text: note }) : null,
    ]));

    return h('div.stack', {}, [
      r.data.effective
        ? notice('All four layers are enforced: authentication, authorization, API '
          + 'and data access.', 'info')
        : notice(`Data-access isolation is not enforced. ${r.data.warning}`, 'warn'),
      h('div.split', {}, [
        h('div.stack', {}, [
          layer('Authentication', r.authentication.sessionCarriesTenant, [
            ['session carries tenant', r.authentication.sessionCarriesTenant],
            ['suspension ends sessions', r.authentication.suspendedSiteEndsSessions],
          ], r.authentication.note),
          layer('Authorization', true, [
            ['your permissions', r.authorization.permissions],
            ['portfolio gated on', r.authorization.portfolioGatedOn],
            ['roles with portfolio access',
              r.authorization.rolesWithPortfolioAccess.join(', ')],
          ]),
        ]),
        h('div.stack', {}, [
          layer('API', r.api.corsPerSite, [
            ['public routes resolve a tenant', r.api.publicRoutesResolveTenant],
            ['CORS is per-site', r.api.corsPerSite],
          ], r.api.note),
          layer('Data access', r.data.effective, [
            ['database role', r.data.role],
            ['superuser (bypasses policies)', r.data.isSuperuser],
            ['BYPASSRLS', r.data.bypassRls],
            ['policies installed', r.data.policies],
            ['tables with RLS forced', r.data.tablesForced],
            ['tables without tenant_id', r.data.tablesWithoutTenantId],
          ], r.data.note),
        ]),
      ]),
      r.data.effective ? null : panel('How to enforce it', panelBody([
        h('p', { text: 'Row-level security is installed but inert while the app '
          + 'connects as a superuser — PostgreSQL exempts superusers from every '
          + 'policy, and FORCE ROW LEVEL SECURITY only reaches the table owner.' }),
        h('pre.code-block', {
          text: '-- as the database owner\n'
            + "ALTER ROLE crm_app PASSWORD '<from your secret store>';\n\n"
            + '# then point the app at it\n'
            + 'DATABASE_URL=postgres://crm_app:<pw>@host:5432/crm',
        }),
        h('p.muted', { text: 'Application-layer scoping (TenantDB) is unaffected and '
          + 'stays in force either way — this is the layer underneath it.' }),
      ])),
    ]);
  }

  window.platformViews = { platform };
}());
