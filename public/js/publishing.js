/* global window, api, kit, ui, navigator */
/** Deployment & publishing (2.10) — build hooks, CDN, API keys, status. */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool, number,
    textInput, checkbox, formatDate, relativeTime, openDrawer } = kit;

  const JOB_KINDS = { complete: 'ok', pending: 'warn', running: 'warn', failed: 'off' };

  async function publishing(ctx) {
    const tab = ctx.params.tab || 'status';
    mount(ctx.el, ui.spinner());

    const [status, hooks, keys] = await Promise.all([
      api.get('/api/publishing/status'),
      api.get('/api/publishing/hooks'),
      api.get('/api/publishing/api-keys'),
    ]);

    ctx.setHead('Publishing', status.siteUrl || 'No site URL configured yet',
      h('div.row-actions', {}, [
        actionButton('Rebuild site', async () => {
          const result = await api.post('/api/publishing/trigger', { reason: 'manual' });
          toast(`${result.queued.length} build hook(s) fired.`);
          ctx.reload();
        }, { primary: true }),
      ]));

    const strip = tabs(ctx, [
      ['status', 'Status', status.staleContent.length || null],
      ['hooks', 'Build hooks', hooks.hooks.length],
      ['keys', 'API keys', keys.keys.filter((k) => k.is_valid).length],
    ], tab);

    const panes = {
      status: () => statusPane(ctx, status, hooks),
      hooks: () => hooksPane(ctx, hooks),
      keys: () => keysPane(ctx, keys),
    };
    mount(ctx.el, [strip, (panes[tab] || panes.status)()]);
  }

  function statusPane(ctx, status, hooks) {
    const c = status.content;
    return h('div.stack', {}, [
      status.siteUrl
        ? null
        : notice('No site URL. Set it in Site → Identity so sitemaps and canonical '
          + 'URLs can be absolute.', 'warn'),
      hooks.hooks.length
        ? null
        : notice('No build hook configured. Publishing changes content in the CMS but '
          + 'will not rebuild your static site.', 'warn'),
      h('div.kpis', {}, [
        kit.kpi('Published', number(c.published)),
        kit.kpi('Drafts', number(c.drafts)),
        kit.kpi('Scheduled', number(c.scheduled)),
        kit.kpi('In trash', number(c.trashed)),
        kit.kpi('Last publish', c.last_published_at ? relativeTime(c.last_published_at) : '—'),
      ]),
      status.staleContent.length
        ? panel('Live pages with unpublished edits', [
          panelBody(h('p.muted', {
            text: 'These are live, but the version on the site is older than the draft. '
              + 'Publish each one to push the changes.',
          })),
          table([
            { label: 'Title', cell: (i) => i.title },
            { label: 'Path', cell: (i) => h('code', { text: i.path || i.slug }) },
            { label: 'Edited', class: 'cell-mono', cell: (i) => relativeTime(i.updated_at) },
            { label: 'Published', class: 'cell-mono', cell: (i) => relativeTime(i.published_at) },
            {
              label: '',
              cell: (i) => actionButton('Publish edits', async () => {
                await api.post(`/api/content/items/${i.id}/publish`, {});
                toast('Live version updated.');
                ctx.reload();
              }, { small: true }),
            },
          ], status.staleContent),
        ])
        : null,
      status.scheduled.length
        ? panel('Waiting to go live', table([
          { label: 'Title', cell: (i) => i.title },
          { label: 'Type', cell: (i) => badge(i.type_slug, 'neutral') },
          { label: 'Goes live', class: 'cell-mono',
            cell: (i) => formatDate(i.scheduled_for, true) },
        ], status.scheduled))
        : null,
      h('div.split', {}, [
        panel('Recent builds', table([
          { label: 'Hook', cell: (r) => r.hook_name },
          { label: 'Status', cell: (r) => badge(r.status, JOB_KINDS[r.status] || 'neutral') },
          { label: 'Reason', cell: (r) => r.reason },
          { label: 'When', class: 'cell-mono', cell: (r) => relativeTime(r.created_at) },
          { label: 'Error', cell: (r) => r.error || '—' },
        ], hooks.runs, { empty: 'No builds triggered yet.' })),
        h('div.stack', {}, [
          panel('Sitemap', panelBody(
            status.sitemap
              ? [
                h('p', { text: `${number(status.sitemap.url_count)} sitemap file(s), `
                  + `generated ${relativeTime(status.sitemap.generated_at)}.` }),
                actionButton('Regenerate', async () => {
                  const result = await api.post('/api/publishing/sitemap', {});
                  toast(result.ok ? `Regenerated ${result.urls} URL(s).`
                    : 'Set the site URL first.');
                  ctx.reload();
                }, { small: true }),
              ]
              : h('p.muted', { text: 'Not generated yet.' }),
          )),
          panel('CDN invalidation', panelBody([
            hooks.cdn.configured
              ? h('p.muted', { text: `Provider: ${hooks.cdn.provider}.` })
              : h('p.muted', {
                text: 'No CloudFront distribution configured. Invalidation requests are '
                  + 'recorded and skipped, which is correct for a CDN that honours '
                  + 'cache-control.',
              }),
            actionButton('Purge everything', async () => {
              await api.post('/api/publishing/invalidate', { paths: ['/*'] });
              toast('Invalidation queued.');
              ctx.reload();
            }, { small: true }),
          ])),
          panel('Recent invalidations', table([
            { label: 'Paths', cell: (i) => (i.paths || []).slice(0, 3).join(', ') },
            { label: 'Status', cell: (i) => badge(i.status, JOB_KINDS[i.status] || 'neutral') },
            { label: 'When', class: 'cell-mono', cell: (i) => relativeTime(i.created_at) },
          ], hooks.invalidations, { empty: 'None yet.' })),
        ]),
      ]),
    ]);
  }

  function hooksPane(ctx, data) {
    return h('div.stack', {}, [
      panel(null, [
        toolbar([
          h('div.spacer'),
          h('button.btn.btn-primary', { type: 'button', text: 'New build hook',
            onclick: () => editHook(ctx, data, null) }),
        ]),
        table([
          { label: 'Hook', cell: (hk) => [h('div.cell-name', { text: hk.name }),
            h('div.cell-meta', { text: hk.url })] },
          { label: 'Provider', cell: (hk) => badge(hk.provider, 'neutral') },
          { label: 'Fires on', cell: (hk) => (hk.trigger_events || []).join(', ') },
          { label: 'Debounce', class: 'cell-mono', cell: (hk) => `${hk.debounce_seconds}s` },
          { label: 'Token', cell: (hk) => bool(hk.has_token, 'Set', 'None') },
          { label: 'Active', cell: (hk) => bool(hk.is_active, 'On', 'Off') },
          { label: 'Last fired', class: 'cell-mono',
            cell: (hk) => relativeTime(hk.last_triggered_at) },
          {
            label: '',
            cell: (hk) => h('div.row-actions', {}, [
              actionButton('Edit', () => editHook(ctx, data, hk), { small: true }),
              actionButton('Fire now', async () => {
                await api.post('/api/publishing/trigger',
                  { hook_id: hk.id, reason: 'manual' });
                toast('Queued.');
                ctx.reload();
              }, { small: true }),
              confirmButton('Delete', async () => {
                await api.del(`/api/publishing/hooks/${hk.id}`);
                toast('Hook deleted.');
                ctx.reload();
              }, { small: true }),
            ]),
          },
        ], data.hooks, {
          empty: 'No build hooks. Add your Vercel/Netlify deploy hook so publishing '
            + 'rebuilds the site.',
        }),
      ]),
      panelBody(h('p.muted', {
        text: 'Debounce coalesces a burst of publishes into one rebuild — most providers '
          + 'bill per build minute.',
      })),
    ]);
  }

  function editHook(ctx, data, hook) {
    const eventBoxes = h('div.chips', {}, data.events.map((event) =>
      h('label.chip', {}, [
        h('input', {
          type: 'checkbox', value: event,
          checked: (hook?.trigger_events || ['content.published']).includes(event),
        }),
        h('span', { text: event }),
      ])));

    formDrawer({
      title: hook ? `Edit “${hook.name}”` : 'New build hook',
      fields: [
        { name: 'name', label: 'Name', control: textInput('name', hook?.name) },
        { name: 'provider', label: 'Provider',
          control: select([['vercel', 'Vercel'], ['netlify', 'Netlify'],
            ['github', 'GitHub Actions'], ['cloudflare', 'Cloudflare Pages'],
            ['generic', 'Generic webhook']], hook?.provider || 'generic', null) },
        { name: 'url', label: 'Deploy hook URL', control: textInput('url', hook?.url),
          help: 'https:// only. Internal and link-local addresses are rejected.' },
        { name: 'auth_token', label: 'Bearer token', control: textInput('auth_token', ''),
          help: hook?.has_token
            ? 'A token is stored. Leave blank to keep it, or type a new one to replace it.'
            : 'Only needed for GitHub Actions and similar. Never read back.' },
        { label: 'Fires on', control: eventBoxes },
        { name: 'debounce_seconds', label: 'Debounce (seconds)',
          control: h('input', { name: 'debounce_seconds', type: 'number',
            value: hook?.debounce_seconds ?? 60 }) },
        hook
          ? { name: 'is_active', label: 'Active',
            control: checkbox('is_active', hook.is_active, 'Enabled') }
          : null,
      ],
      onSave: async (values) => {
        const chosen = [...eventBoxes.querySelectorAll('input:checked')].map((i) => i.value);
        const body = {
          name: values.name,
          url: values.url,
          trigger_events: chosen.length ? chosen : ['content.published'],
          debounce_seconds: Number(values.debounce_seconds) || 0,
        };
        if (hook) {
          const patch = { ...body, is_active: Boolean(values.is_active) };
          // Only send the token when the field was actually filled in.
          if (values.auth_token) patch.auth_token = values.auth_token;
          await api.patch(`/api/publishing/hooks/${hook.id}`, patch);
        } else {
          await api.post('/api/publishing/hooks', {
            ...body, provider: values.provider,
            auth_token: values.auth_token || null,
          });
        }
        toast('Saved.');
        ctx.reload();
      },
    });
  }

  function keysPane(ctx, data) {
    return h('div.stack', {}, [
      panel(null, [
        toolbar([
          h('div.spacer'),
          h('button.btn.btn-primary', { type: 'button', text: 'New API key',
            onclick: () => createKey(ctx, data) }),
        ]),
        table([
          { label: 'Key', cell: (k) => [h('div.cell-name', { text: k.name }),
            h('div.cell-meta', { text: `${k.key_prefix}…` })] },
          { label: 'Scopes', cell: (k) => (k.scopes || []).join(', ') },
          { label: 'Status',
            cell: (k) => (k.revoked_at ? badge('revoked', 'off')
              : k.is_valid ? badge('valid', 'ok') : badge('expired', 'off')) },
          { label: 'Last used', class: 'cell-mono', cell: (k) => relativeTime(k.last_used_at) },
          { label: 'Expires', class: 'cell-mono',
            cell: (k) => (k.expires_at ? formatDate(k.expires_at) : 'never') },
          {
            label: '',
            cell: (k) => (k.revoked_at ? null : confirmButton('Revoke', async () => {
              await api.del(`/api/publishing/api-keys/${k.id}`);
              toast('Key revoked.');
              ctx.reload();
            }, { small: true })),
          },
        ], data.keys, { empty: 'No API keys. Published content is readable without one '
          + 'unless you turn that off in Site → Configuration.' }),
      ]),
      panelBody(h('p.muted', {
        text: 'Revoked keys are kept as an audit trail of what was used and when.',
      })),
    ]);
  }

  function createKey(ctx, data) {
    const scopeBoxes = h('div.chips', {}, data.scopes.map((scope) =>
      h('label.chip', {}, [
        h('input', { type: 'checkbox', value: scope,
          checked: ['content:read', 'config:read'].includes(scope) }),
        h('span', { text: scope }),
      ])));

    formDrawer({
      title: 'New API key',
      fields: [
        { name: 'name', label: 'Name', control: textInput('name', ''),
          help: 'What will use it — “Vercel build”, “Mobile app”.' },
        { label: 'Scopes', control: scopeBoxes },
        { name: 'expires_in_days', label: 'Expires in (days)',
          control: h('input', { name: 'expires_in_days', type: 'number', value: 365 }),
          help: 'Blank never expires. A build key with an expiry is one you remember to rotate.' },
        { name: 'note', label: 'Note', control: textInput('note', '') },
      ],
      onSave: async (values) => {
        const scopes = [...scopeBoxes.querySelectorAll('input:checked')].map((i) => i.value);
        if (!scopes.length) throw new Error('Choose at least one scope.');
        const result = await api.post('/api/publishing/api-keys', {
          name: values.name,
          scopes,
          expires_in_days: Number(values.expires_in_days) || null,
          note: values.note || null,
        });
        // Shown once. There is no second chance to read it.
        openDrawer({
          title: 'Copy this key now',
          subtitle: 'It is stored only as a hash and cannot be shown again.',
          body: [
            h('pre.code-block', { text: result.secret }),
            actionButton('Copy to clipboard', async () => {
              await navigator.clipboard?.writeText(result.secret).catch(() => {});
              toast('Copied.');
            }, { primary: true }),
            h('p.muted', {
              text: 'Send it as: Authorization: Bearer <key>',
            }),
          ],
        });
        ctx.reload({ keepDrawer: true });
      },
      saveLabel: 'Create key',
    });
  }

  window.publishingViews = { publishing };
}());
