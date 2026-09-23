/* global window, api, kit, ui */
/**
 * SEO & site discovery (2.2) — every page's meta tags, redirects, the
 * 404 log, sitemap and robots.txt.
 *
 * The Pages tab is the one place that answers "what does this site tell
 * a search engine about each of its pages"; the same fields are also on
 * each page's own settings drawer, because that is where someone is
 * when they finish writing one.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, search, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool, counted,
    textInput, textarea, formatDate, relativeTime, number } = kit;

  async function seo(ctx) {
    const tab = ctx.params.tab || 'pages';
    ctx.setHead('SEO', 'Meta tags, redirects, missing pages, sitemap and crawler rules.');
    mount(ctx.el, ui.spinner());

    const [pages, redirects, notFound, sitemaps, robots] = await Promise.all([
      api.get('/api/pages').catch(() => ({ pages: [] })),
      api.get(`/api/seo/redirects${api.qs({ q: ctx.params.q })}`),
      api.get('/api/seo/not-found'),
      api.get('/api/seo/sitemaps'),
      api.get('/api/seo/robots'),
    ]);

    const strip = tabs(ctx, [
      ['pages', 'Pages', pages.pages.length],
      ['redirects', 'Redirects', redirects.redirects.length],
      ['not-found', '404s', notFound.notFound.filter((n) => !n.resolved_redirect_id).length],
      ['sitemap', 'Sitemap', sitemaps.sitemaps.length],
      ['robots', 'robots.txt'],
    ], tab);

    const panes = {
      pages: () => pagesPane(ctx, pages.pages),
      redirects: () => redirectsPane(ctx, redirects),
      'not-found': () => notFoundPane(ctx, notFound),
      sitemap: () => sitemapPane(ctx, sitemaps),
      robots: () => robotsPane(ctx, robots),
    };

    ctx.setHead('SEO', 'Meta tags, redirects, missing pages, sitemap and crawler rules.',
      tab === 'redirects'
        ? h('button.btn.btn-primary', {
          type: 'button', text: 'New redirect', onclick: () => editRedirect(ctx, null),
        })
        : null);

    mount(ctx.el, [strip, (panes[tab] || panes.pages)()]);
  }

  // ================================================== per-page meta tags
  // Google truncates a title around 60 characters and a description
  // around 160; below 30 / 70 there is usually room to say more. The
  // counters colour on those bounds rather than enforcing them.
  const TITLE_RANGE = [30, 60];
  const DESC_RANGE = [70, 160];

  function pagesPane(ctx, pages) {
    const tenant = ctx.session.user.tenantSlug;
    const lengthNote = (value, [min, max]) => {
      const n = (value || '').length;
      if (!n) return 'missing';
      if (n < min) return `${n} — short`;
      if (n > max) return `${n} — will be cut off`;
      return `${n} characters`;
    };

    return h('div.stack', {}, [
      panelBody(h('p.muted', {
        text: 'The title and description a search result shows for each page. '
          + 'Left empty, the title falls back to the page’s own title and the '
          + 'description to nothing — so a page with no description is one '
          + 'Google writes the snippet for.',
      })),
      panel(null, table([
        {
          label: 'Page',
          cell: (p) => [
            h('div.cell-name', { text: p.title }),
            h('div.cell-meta.mono', { text: `/p/${tenant}/${p.slug}` }),
          ],
        },
        {
          label: 'Meta title',
          cell: (p) => [
            h('div', { text: (p.seo || {}).meta_title || p.title }),
            h('div.cell-meta', {
              text: (p.seo || {}).meta_title
                ? lengthNote((p.seo || {}).meta_title, TITLE_RANGE)
                : 'using the page title',
            }),
          ],
        },
        {
          label: 'Meta description',
          cell: (p) => [
            h('div', { text: p.description || '—' }),
            h('div.cell-meta', { text: lengthNote(p.description, DESC_RANGE) }),
          ],
        },
        {
          label: 'Indexing',
          cell: (p) => {
            const seo = p.seo || {};
            if (p.status !== 'published') return badge('draft — not live', 'off');
            if (seo.noindex) return badge('noindex', 'off');
            return badge(seo.nofollow ? 'nofollow' : 'indexed', 'ok');
          },
        },
        {
          label: '',
          cell: (p) => h('div.row-actions', {}, [
            actionButton('Edit meta', () => editPageMeta(ctx, p), { small: true }),
            h('a.btn.btn-sm', { href: `#/pages?edit=${p.id}`, text: 'Open page' }),
          ]),
        },
      ], pages, { empty: 'No pages yet. Create one under Page builder.' })),
    ]);
  }

  /**
   * One page's meta tags.
   *
   * Saves through the page API (PATCH /api/pages/{id}), the same call
   * the page's own settings drawer makes, so there is one definition of
   * what a page's meta is and no second path that could disagree. The
   * SEO block is spread first: focus keyword, Schema.org and the social
   * image are stored in it too and no field here owns them.
   */
  function editPageMeta(ctx, page) {
    const seo = page.seo || {};
    const metaTitle = textInput('meta_title', seo.meta_title, { maxlength: 80,
      placeholder: page.title });
    const description = textarea('description', page.description, { rows: 3, maxlength: 300 });
    const canonical = textInput('canonical', seo.canonical, { maxlength: 500,
      placeholder: 'https://example.com/the-real-page' });
    const ogTitle = textInput('og_title', seo.og_title, { maxlength: 120,
      placeholder: 'defaults to the meta title' });
    const ogDescription = textarea('og_description', seo.og_description, { rows: 2, maxlength: 320 });
    const noindex = h('input', { type: 'checkbox', checked: Boolean(seo.noindex) });
    const nofollow = h('input', { type: 'checkbox', checked: Boolean(seo.nofollow) });

    formDrawer({
      title: `Meta tags — ${page.title}`,
      subtitle: `/p/${ctx.session.user.tenantSlug}/${page.slug}`,
      fields: [
        { name: 'meta_title', label: 'Meta title', control: counted(metaTitle, ...TITLE_RANGE, seo.meta_title || ''),
          help: '<title> and the blue line in a search result. Empty uses the page title.' },
        { name: 'description', label: 'Meta description', control: counted(description, ...DESC_RANGE, page.description || ''),
          help: '<meta name="description"> — the grey snippet under the link.' },
        { name: 'canonical', label: 'Canonical URL', control: canonical,
          help: 'Only when this page duplicates another. Empty means the page’s own URL.' },
        { name: 'og_title', label: 'Share title (Open Graph)', control: ogTitle },
        { name: 'og_description', label: 'Share description', control: ogDescription,
          help: 'Used by Facebook, LinkedIn, WhatsApp and X. Empty falls back to the meta description.' },
        { label: 'Crawlers', control: h('div.stack', {}, [
          h('label.pb-check', {}, [noindex, 'noindex — keep this page out of search results']),
          h('label.pb-check', {}, [nofollow, 'nofollow — do not follow links on this page']),
        ]) },
      ],
      onSave: async (values) => {
        await api.patch(`/api/pages/${page.id}`, {
          description: values.description || null,
          seo: {
            ...seo,
            meta_title: values.meta_title || '',
            canonical: values.canonical || '',
            og_title: values.og_title || '',
            og_description: values.og_description || '',
            noindex: noindex.checked,
            nofollow: nofollow.checked,
          },
        });
        toast('Meta tags saved. Publish the page to put them live.');
        ctx.reload();
      },
    });
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
