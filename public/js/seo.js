/* global window, api, kit, ui */
/**
 * SEO & site discovery (2.2) — redirects, the 404 log, sitemap and
 * robots.txt. Per-page meta lives in the content editor, next to the
 * content it describes.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, search, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool,
    textInput, textarea, formatDate, relativeTime, number } = kit;

  async function seo(ctx) {
    const tab = ctx.params.tab || 'redirects';
    ctx.setHead('SEO', 'Redirects, missing pages, sitemap and crawler rules.');
    mount(ctx.el, ui.spinner());

    const [redirects, notFound, sitemaps, robots] = await Promise.all([
      api.get(`/api/seo/redirects${api.qs({ q: ctx.params.q })}`),
      api.get('/api/seo/not-found'),
      api.get('/api/seo/sitemaps'),
      api.get('/api/seo/robots'),
    ]);

    const strip = tabs(ctx, [
      ['redirects', 'Redirects', redirects.redirects.length],
      ['not-found', '404s', notFound.notFound.filter((n) => !n.resolved_redirect_id).length],
      ['sitemap', 'Sitemap', sitemaps.sitemaps.length],
      ['robots', 'robots.txt'],
    ], tab);

    const panes = {
      redirects: () => redirectsPane(ctx, redirects),
      'not-found': () => notFoundPane(ctx, notFound),
      sitemap: () => sitemapPane(ctx, sitemaps),
      robots: () => robotsPane(ctx, robots),
    };

    ctx.setHead('SEO', 'Redirects, missing pages, sitemap and crawler rules.',
      tab === 'redirects'
        ? h('button.btn.btn-primary', {
          type: 'button', text: 'New redirect', onclick: () => editRedirect(ctx, null),
        })
        : null);

    mount(ctx.el, [strip, (panes[tab] || panes.redirects)()]);
  }

  function redirectsPane(ctx, data) {
    return panel(null, [
      toolbar([
        search(ctx.params.q, (q) => ctx.navigate(`#/seo${api.qs({ ...ctx.params, q })}`),
          'Search from/to path…'),
        h('div.spacer'),
        h('span.muted', {
          text: `${data.redirects.filter((r) => r.is_automatic).length} created automatically `
            + 'by slug changes',
        }),
      ]),
      table([
        { label: 'From', cell: (r) => h('code', { text: r.from_path }) },
        { label: 'To', cell: (r) => h('code', { text: r.to_path }) },
        { label: 'Code', class: 'cell-mono', cell: (r) => r.status_code },
        { label: 'Source', cell: (r) => badge(r.is_automatic ? 'auto' : 'manual',
          r.is_automatic ? 'neutral' : 'ok') },
        { label: 'Hits', class: 'cell-mono', cell: (r) => number(r.hits) },
        { label: 'Active', cell: (r) => bool(r.is_active, 'On', 'Off') },
        {
          label: '',
          cell: (r) => h('div.row-actions', {}, [
            actionButton('Edit', () => editRedirect(ctx, r), { small: true }),
            confirmButton('Delete', async () => {
              await api.del(`/api/seo/redirects/${r.id}`);
              toast('Redirect deleted.');
              ctx.reload();
            }, { small: true }),
          ]),
        },
      ], data.redirects, {
        empty: 'No redirects. One is created automatically whenever a published slug changes.',
      }),
      panelBody(h('p.muted', {
        text: 'A static frontend resolves these by calling '
          + '/api/v1/{site}/redirect?path=… on a 404, or at the edge.',
      })),
    ]);
  }

  function editRedirect(ctx, redirect) {
    formDrawer({
      title: redirect ? 'Edit redirect' : 'New redirect',
      fields: [
        redirect
          ? null
          : { name: 'from_path', label: 'From path',
            control: textInput('from_path', '', { placeholder: '/old-page' }),
            help: 'Query strings and trailing slashes are normalised away.' },
        { name: 'to_path', label: 'To path or URL',
          control: textInput('to_path', redirect?.to_path, { placeholder: '/new-page' }) },
        { name: 'status_code', label: 'Status code',
          control: select([[301, '301 — moved permanently'], [302, '302 — found (temporary)'],
            [307, '307 — temporary, keep method'], [308, '308 — permanent, keep method']],
          redirect?.status_code || 301, null) },
        { name: 'note', label: 'Note', control: textInput('note', redirect?.note) },
        redirect
          ? { name: 'is_active', label: 'Active',
            control: kit.checkbox('is_active', redirect.is_active, 'Redirect is live') }
          : null,
      ],
      onSave: async (values) => {
        const body = {
          to_path: values.to_path,
          status_code: Number(values.status_code),
          note: values.note || null,
        };
        if (redirect) {
          await api.patch(`/api/seo/redirects/${redirect.id}`,
            { ...body, is_active: Boolean(values.is_active) });
        } else {
          await api.post('/api/seo/redirects', { from_path: values.from_path, ...body });
        }
        toast('Saved.');
        ctx.reload();
      },
    });
  }

  function notFoundPane(ctx, data) {
    return panel(null, [
      toolbar([
        h('span.muted', { text: 'Paths visitors asked for that do not exist.' }),
        h('div.spacer'),
        confirmButton('Clear resolved & ignored', async () => {
          const result = await api.del('/api/seo/not-found?resolved_only=true');
          toast(`${result.deleted} entry(ies) cleared.`);
          ctx.reload();
        }, { small: true, danger: false }),
      ]),
      table([
        { label: 'Path', cell: (n) => h('code', { text: n.path }) },
        { label: 'Hits', class: 'cell-mono', cell: (n) => number(n.hits) },
        { label: 'Last seen', class: 'cell-mono', cell: (n) => relativeTime(n.last_seen_at) },
        {
          label: 'Suggestion',
          cell: (n) => (n.suggestion
            ? h('span', {}, [h('code', { text: n.suggestion.toPath }),
              h('span.muted', { text: ` — ${n.suggestion.title}` })])
            : h('span.muted', { text: 'no close match' })),
        },
        {
          label: 'Status',
          cell: (n) => (n.resolved_redirect_id ? badge('redirected', 'ok')
            : n.is_ignored ? badge('ignored', 'off') : badge('open', 'warn')),
        },
        {
          label: '',
          cell: (n) => (n.resolved_redirect_id || n.is_ignored ? null : h('div.row-actions', {}, [
            n.suggestion
              ? actionButton('Redirect to suggestion', async () => {
                await api.post(`/api/seo/not-found/${n.id}/resolve`,
                  { to_path: n.suggestion.toPath, status_code: 301 });
                toast('Redirect created.');
                ctx.reload();
              }, { small: true })
              : null,
            actionButton('Redirect to…', () => resolveNotFound(ctx, n), { small: true }),
            actionButton('Ignore', async () => {
              await api.post(`/api/seo/not-found/${n.id}/ignore`, {});
              ctx.reload();
            }, { small: true }),
          ])),
        },
      ], data.notFound, {
        empty: 'No 404s logged. Point your frontend’s 404 page at '
          + 'POST /api/v1/{site}/not-found to start collecting them.',
      }),
    ]);
  }

  function resolveNotFound(ctx, entry) {
    formDrawer({
      title: `Redirect ${entry.path}`,
      fields: [
        { name: 'to_path', label: 'Send visitors to',
          control: textInput('to_path', entry.suggestion?.toPath || '') },
        { name: 'status_code', label: 'Status code',
          control: select([[301, '301 — permanent'], [302, '302 — temporary']], 301, null) },
      ],
      onSave: async (values) => {
        await api.post(`/api/seo/not-found/${entry.id}/resolve`, {
          to_path: values.to_path, status_code: Number(values.status_code),
        });
        toast('Redirect created.');
        ctx.reload();
      },
      saveLabel: 'Create redirect',
    });
  }

  function sitemapPane(ctx, data) {
    return panel(null, [
      data.ready
        ? notice(`Sitemap index: ${data.indexUrl}`, 'info')
        : notice('Set the site URL in Site settings → Identity. A sitemap needs '
          + 'absolute URLs, so nothing can be generated until then.', 'warn'),
      toolbar([
        h('div.spacer'),
        actionButton('Regenerate now', async () => {
          const result = await api.post('/api/seo/sitemaps/regenerate', {});
          toast(`Regenerated ${result.sitemaps.length} sitemap(s), ${result.urls} URL(s).`);
          ctx.reload();
        }, { primary: true }),
      ]),
      table([
        { label: 'Sitemap', cell: (s) => h('code', { text: `/sitemap-${s.name}.xml` }) },
        { label: 'URLs', class: 'cell-mono', cell: (s) => number(s.url_count) },
        { label: 'Size', class: 'cell-mono', cell: (s) => kit.bytes(s.bytes) },
        { label: 'Generated', class: 'cell-mono', cell: (s) => relativeTime(s.generated_at) },
      ], data.sitemaps, { empty: 'No sitemap generated yet. Publish something, or regenerate.' }),
      panelBody(h('p.muted', {
        text: 'Regenerated automatically on every publish. Noindexed pages are left out.',
      })),
    ]);
  }

  function robotsPane(ctx, data) {
    const body = textarea('body', data.body, { rows: 14, class: 'code' });
    return panel(null, panelBody([
      h('label.field', {}, [
        h('span', { text: 'robots.txt' }),
        body,
        h('small.muted', {
          text: data.isCustom
            ? 'Custom rules. Clear the box and save to go back to the generated default.'
            : 'Currently generated automatically. Editing takes over.',
        }),
      ]),
      h('div.row-actions', {}, [
        actionButton('Save robots.txt', async () => {
          const result = await api.put('/api/seo/robots', { body: body.value });
          (result.warnings || []).forEach((w) => toast(w, 'error'));
          toast('Saved.');
          ctx.reload();
        }, { primary: true }),
      ]),
    ]));
  }

  window.seoViews = { seo };
}());
