/* global window, api, kit, ui */
/**
 * Content management (2.1) — schema-driven items, plus categories and
 * tags.
 *
 * The editor renders its field inputs from the content type's own
 * `field_schema`, so adding a field in Settings changes this screen
 * without a frontend change.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, search, badge, select,
    formDrawer, confirmButton, actionButton, pager, notice, tabs, counted,
    textInput, textarea, checkbox, formatDate, relativeTime, openDrawer } = kit;

  const STATUS_KINDS = { published: 'ok', draft: 'neutral', scheduled: 'warn', trashed: 'off' };

  const statusBadge = (status) => badge(status, STATUS_KINDS[status] || 'neutral');

  // =====================================================================
  // List
  // =====================================================================
  async function content(ctx) {
    const typeSlug = ctx.params.type || '';
    const [{ types }, counts] = await Promise.all([
      api.get('/api/content/types'),
      api.get('/api/content/items/counts'),
    ]);
    const active = types.filter((t) => t.is_active);
    if (!active.length) {
      mount(ctx.el, ui.emptyState('No content types yet',
        'Create one in Settings → Content types to start publishing.'));
      return;
    }

    const current = active.find((t) => t.slug === typeSlug) || active[0];
    const status = ctx.params.status || '';
    const page = Number(ctx.params.page) || 1;

    ctx.setHead(current.plural_name, `${current.item_count} item(s) · ${current.route_prefix
      ? `served at ${current.route_prefix}` : 'not addressable on its own'}`,
    h('button.btn.btn-primary', {
      type: 'button', text: `New ${current.name.toLowerCase()}`,
      onclick: () => openEditor(ctx, current, null),
    }));

    mount(ctx.el, ui.spinner());
    const data = await api.get(`/api/content/items${api.qs({
      type: current.slug, status, q: ctx.params.q, term_id: ctx.params.term_id,
      mine: ctx.params.mine, sort: ctx.params.sort, page,
    })}`);

    const typeCounts = counts.byType[current.slug] || {};
    const statusTabs = [
      ['', 'All', data.total],
      ['published', 'Published', typeCounts.published],
      ['draft', 'Drafts', typeCounts.draft],
      ['scheduled', 'Scheduled', typeCounts.scheduled],
      ['trashed', 'Trash', typeCounts.trashed],
    ];

    const selected = new Set();
    const bulkBar = h('div.bulkbar.hidden');

    const paintBulk = () => {
      bulkBar.classList.toggle('hidden', selected.size === 0);
      mount(bulkBar, [
        h('span', { text: `${selected.size} selected` }),
        h('div.spacer'),
        actionButton('Publish', () => runBulk(ctx, 'publish', selected), { small: true }),
        actionButton('Unpublish', () => runBulk(ctx, 'unpublish', selected), { small: true }),
        status === 'trashed'
          ? actionButton('Restore', () => runBulk(ctx, 'restore', selected), { small: true })
          : actionButton('Trash', () => runBulk(ctx, 'trash', selected), { small: true }),
        status === 'trashed'
          ? confirmButton('Delete permanently',
            () => runBulk(ctx, 'delete', selected), { small: true })
          : null,
      ]);
    };

    const columns = [
      {
        label: '', width: '32px', cell: (row) => h('input', {
          type: 'checkbox',
          onclick: (e) => {
            e.stopPropagation();
            if (e.target.checked) selected.add(row.id); else selected.delete(row.id);
            paintBulk();
          },
        }),
      },
      {
        label: 'Title',
        cell: (row) => [
          h('div.cell-name', { text: row.title }),
          h('div.cell-meta', {
            text: [row.path || row.slug, row.noindex ? 'noindex' : null]
              .filter(Boolean).join(' · '),
          }),
        ],
      },
      { label: 'Status', cell: (row) => statusBadge(row.status) },
      { label: 'Author', cell: (row) => row.author_name },
      {
        label: 'Updated',
        class: 'cell-mono',
        cell: (row) => (row.status === 'scheduled' && row.scheduled_for
          ? `goes live ${formatDate(row.scheduled_for, true)}`
          : relativeTime(row.updated_at)),
      },
    ];

    mount(ctx.el, [
      tabs(ctx, statusTabs, status, 'status'),
      panel(null, [
        toolbar([
          select(active.map((t) => [t.slug, t.plural_name]), current.slug,
            (e) => ctx.navigate(`#/content?type=${e.target.value}`)),
          search(ctx.params.q, (q) =>
            ctx.navigate(`#/content${api.qs({ ...ctx.params, q, page: 1 })}`)),
          select([['updated', 'Recently updated'], ['created', 'Newest'],
            ['published', 'Recently published'], ['title', 'Title A–Z'],
            ['order', 'Manual order']], ctx.params.sort || 'updated',
          (e) => ctx.navigate(`#/content${api.qs({ ...ctx.params, sort: e.target.value })}`)),
          checkbox('mine', ctx.params.mine === '1', 'Only mine'),
          h('div.spacer'),
          h('a.btn.btn-sm', { href: '#/taxonomy', text: 'Categories & tags' }),
        ]),
        bulkBar,
        table(columns, data.items, {
          empty: `No ${current.plural_name.toLowerCase()} here yet.`,
          onRow: (row) => openEditor(ctx, current, row.id),
        }),
        pager(ctx, data.page, data.pages),
      ]),
    ]);

    // Deep link from a notification or an email.
    if (ctx.params.open) openEditor(ctx, current, Number(ctx.params.open));

    ctx.el.querySelector('input[name=mine]')?.addEventListener('change', (e) => {
      ctx.navigate(`#/content${api.qs({ ...ctx.params, mine: e.target.checked ? '1' : '', page: 1 })}`);
    });
  }

  async function runBulk(ctx, action, ids) {
    const result = await api.post('/api/content/items/bulk',
      { ids: [...ids], action });
    toast(`${result.affected} item(s) ${action === 'delete' ? 'deleted' : `set to ${action}`}.`);
    ctx.reload();
  }

  // =====================================================================
  // Editor
  // =====================================================================
  async function openEditor(ctx, type, itemId) {
    const isNew = !itemId;
    const [detail, taxonomies, authors] = await Promise.all([
      isNew ? Promise.resolve(null) : api.get(`/api/content/items/${itemId}`),
      api.get('/api/content/taxonomies'),
      api.get('/api/content/authors'),
    ]);
    const item = detail ? detail.item : {};
    const schema = (detail ? item.field_schema : type.field_schema) || [];
    const supports = (detail ? item.supports : type.supports) || {};
    const applicable = taxonomies.taxonomies.filter((t) =>
      (t.type_slugs || []).includes(type.slug));

    const terms = applicable.length
      ? (await api.get('/api/content/terms')).terms
      : [];
    const chosen = new Set((item.terms || []).map((t) => t.id));

    const titleInput = textInput('title', item.title, { required: true, maxlength: 200 });
    const slugInput = textInput('slug', item.slug, {
      maxlength: 80, placeholder: isNew ? 'derived from the title' : '',
    });
    const excerptInput = textarea('excerpt', item.excerpt, { rows: 2, maxlength: 600 });
    const bodyInput = textarea('body', item.body, { rows: 14, class: 'code' });

    const seo = item.seo || {};
    const metaTitle = textInput('meta_title', seo.meta_title, { maxlength: 80 });
    const metaDesc = textarea('meta_description', seo.meta_description, { rows: 3, maxlength: 320 });

    const fieldControls = schema.map((f) => ({
      name: `f_${f.name}`,
      label: f.label + (f.required ? ' *' : ''),
      help: f.help,
      control: fieldControl(f, (item.fields || {})[f.name]),
    }));

    const termPicker = applicable.length
      ? h('div.chips', {}, applicable.flatMap((tax) => [
        h('div.chip-group-label', { text: tax.plural_name }),
        ...terms.filter((t) => t.taxonomy === tax.slug).map((t) =>
          h('label.chip', {}, [
            h('input', {
              type: 'checkbox', value: t.id, checked: chosen.has(t.id),
              onchange: (e) => (e.target.checked ? chosen.add(t.id) : chosen.delete(t.id)),
            }),
            h('span', { text: t.name }),
          ])),
        terms.filter((t) => t.taxonomy === tax.slug).length
          ? null
          : h('span.muted', { text: `No ${tax.plural_name.toLowerCase()} defined yet.` }),
      ]))
      : null;

    const fields = [
      { name: 'title', label: 'Title', control: titleInput },
      { name: 'slug', label: 'Slug', control: slugInput,
        help: type.route_prefix
          ? `Published at ${type.route_prefix === '/' ? '' : type.route_prefix}/<slug>. `
            + 'Changing it on a live page leaves a 301 behind.'
          : 'Not addressable on its own.' },
      supports.excerpt !== false
        ? { name: 'excerpt', label: 'Excerpt', control: excerptInput,
          help: 'Used in listings and as the fallback meta description.' }
        : null,
      supports.body !== false
        ? { name: 'body', label: 'Body (HTML)', control: bodyInput,
          help: 'Sanitized on save. Embeds are allowed from the configured hosts only.' }
        : null,
      ...fieldControls,
      applicable.length ? { label: 'Categories & tags', control: termPicker } : null,
      { name: 'author_id', label: 'Author',
        control: select(authors.authors.map((a) => [a.id, a.display_name]),
          item.author_id || ctx.session.user.id, null) },
      supports.seo !== false
        ? { name: 'meta_title', label: 'SEO title',
          control: counted(metaTitle, 30, 60, seo.meta_title),
          help: 'Shown in search results. 30–60 characters reads best.' }
        : null,
      supports.seo !== false
        ? { name: 'meta_description', label: 'Meta description',
          control: counted(metaDesc, 70, 160, seo.meta_description) }
        : null,
    ];

    const save = async (values) => {
      const body = {
        title: values.title,
        excerpt: values.excerpt ?? undefined,
        body: values.body ?? undefined,
        author_id: Number(values.author_id) || undefined,
        fields: collectFields(schema, values),
        seo: { ...seo, meta_title: values.meta_title, meta_description: values.meta_description },
        term_ids: [...chosen],
      };
      if (values.slug) body.slug = values.slug;

      if (isNew) {
        const created = await api.post('/api/content/items', { type: type.slug, ...body });
        toast('Draft created.');
        // Changing the hash fires the router, which re-renders and
        // reopens the editor on the new item. Calling reload() as well
        // would render twice.
        ctx.navigate(`#/content?type=${type.slug}&open=${created.item.id}`);
        return;
      }
      const saved = await api.patch(`/api/content/items/${itemId}`, body);
      toast(saved.redirectCreated
        ? `Saved. A 301 now points ${saved.redirectCreated} at the new URL.`
        : 'Saved.');
      ctx.reload();
    };

    const drawer = formDrawer({
      title: isNew ? `New ${type.name.toLowerCase()}` : item.title,
      subtitle: isNew ? null
        : `${item.status} · ${item.revisionCount} revision(s) · updated ${relativeTime(item.updated_at)}`,
      fields,
      onSave: save,
      saveLabel: isNew ? 'Create draft' : 'Save changes',
      extra: isNew ? null : lifecyclePanel(ctx, item),
    });
    return drawer;
  }

  function fieldControl(f, value) {
    if (f.type === 'boolean') return checkbox(`f_${f.name}`, value, 'Enabled');
    if (f.type === 'textarea') return textarea(`f_${f.name}`, value, { rows: 3, maxlength: f.max });
    if (f.type === 'richtext') return textarea(`f_${f.name}`, value, { rows: 8, class: 'code' });
    if (f.type === 'select') return select((f.options || []).map((o) => [o, o]), value, null);
    if (f.type === 'multiselect') {
      return textInput(`f_${f.name}`, Array.isArray(value) ? value.join(', ') : value,
        { placeholder: 'Comma separated' });
    }
    if (f.type === 'number') return h('input', { name: `f_${f.name}`, type: 'number', value: value ?? '' });
    if (f.type === 'date') return h('input', { name: `f_${f.name}`, type: 'date', value: value ?? '' });
    if (f.type === 'datetime') {
      return h('input', {
        name: `f_${f.name}`, type: 'datetime-local',
        value: value ? String(value).slice(0, 16) : '',
      });
    }
    if (f.type === 'media') {
      return h('input', { name: `f_${f.name}`, type: 'number', value: value ?? '',
        placeholder: 'Media id — copy it from the Media library' });
    }
    return textInput(`f_${f.name}`, value, { maxlength: f.max || 200 });
  }

  function collectFields(schema, values) {
    const out = {};
    schema.forEach((f) => {
      const raw = values[`f_${f.name}`];
      if (raw === undefined) return;
      if (f.type === 'multiselect') {
        out[f.name] = String(raw).split(',').map((s) => s.trim()).filter(Boolean);
      } else if (f.type === 'boolean') {
        out[f.name] = Boolean(raw);
      } else {
        out[f.name] = raw === '' ? null : raw;
      }
    });
    return out;
  }

  /** Publish / schedule / trash / revisions / preview, under the form. */
  function lifecyclePanel(ctx, item) {
    const scheduleInput = h('input', { type: 'datetime-local' });

    const row = (children) => h('div.row-actions', {}, children);
    const can = item.can || {};

    return h('div.stack', {}, [
      h('hr'),
      h('h3', { text: 'Publishing' }),
      item.path
        ? h('p.muted', { text: `Public path: ${item.path}` })
        : h('p.muted', { text: 'This type has no public URL of its own.' }),
      row([
        can.publish && item.status !== 'published'
          ? actionButton('Publish now', async () => {
            await api.post(`/api/content/items/${item.id}/publish`, {});
            toast('Published.');
            ctx.reload();
          }, { primary: true })
          : null,
        can.publish && item.status === 'published'
          ? actionButton('Re-publish edits', async () => {
            await api.post(`/api/content/items/${item.id}/publish`, {});
            toast('Live version updated.');
            ctx.reload();
          }, { primary: true })
          : null,
        can.publish && item.status === 'published'
          ? actionButton('Unpublish', async () => {
            await api.post(`/api/content/items/${item.id}/unpublish`, {});
            toast('Taken off the site.');
            ctx.reload();
          })
          : null,
        actionButton('Preview link', async () => {
          const preview = await api.post(`/api/content/items/${item.id}/preview`, {});
          await navigator.clipboard?.writeText(
            preview.siteUrl || window.location.origin + preview.apiUrl,
          ).catch(() => {});
          toast('Preview link copied. It expires in 48 hours.');
        }),
        actionButton('Revisions', () => openRevisions(ctx, item)),
      ]),
      can.publish && item.status !== 'published'
        ? h('div.field-inline', {}, [
          scheduleInput,
          actionButton('Schedule', async () => {
            if (!scheduleInput.value) throw new Error('Pick a date and time first.');
            await api.post(`/api/content/items/${item.id}/publish`,
              { scheduled_for: new Date(scheduleInput.value).toISOString() });
            toast('Scheduled.');
            ctx.reload();
          }),
        ])
        : null,
      h('hr'),
      row([
        item.status === 'trashed'
          ? actionButton('Restore from trash', async () => {
            await api.post(`/api/content/items/${item.id}/restore`, {});
            toast('Restored as a draft.');
            ctx.reload();
          })
          : (can.trash ? confirmButton('Move to trash', async () => {
            await api.post(`/api/content/items/${item.id}/trash`, {});
            toast('Moved to trash.');
            ctx.reload();
          }) : null),
        item.status === 'trashed' && can.purge
          ? confirmButton('Delete permanently', async () => {
            await api.del(`/api/content/items/${item.id}?confirm=true`);
            toast('Deleted.');
            ctx.reload();
          })
          : null,
      ]),
      seoReport(item.seoReport),
    ]);
  }

  function seoReport(report) {
    if (!report) return null;
    const line = (label, ok, note) =>
      h('div.check-row', {}, [
        h('span.check-dot', { dataset: { state: ok ? 'ok' : 'off' } }),
        h('span', { text: label }),
        note ? h('span.muted', { text: note }) : null,
      ]);
    return h('div.stack', {}, [
      h('hr'),
      h('h3', { text: 'SEO checks' }),
      ...report.checks.map((c) =>
        line(c.field.replace('_', ' '), c.state === 'ok',
          `${c.length} chars (target ${c.min}–${c.max})`)),
      line('Focus keyword set', report.hasFocusKeyword),
      report.hasFocusKeyword ? line('Keyword in title', report.keywordInTitle) : null,
      report.hasFocusKeyword ? line('Keyword in body', report.keywordInBody) : null,
      line('Open Graph fields', report.hasOpenGraph),
      line('Schema.org block', report.hasSchema),
      report.noindex ? notice('This page is set to noindex — search engines will skip it.', 'warn') : null,
      h('p.muted', { text: `${report.wordCount} words in the body.` }),
    ]);
  }

  async function openRevisions(ctx, item) {
    const { revisions, keep } = await api.get(`/api/content/items/${item.id}/revisions`);
    openDrawer({
      title: 'Revision history',
      subtitle: `Newest ${keep} kept for “${item.title}”`,
      body: table([
        { label: 'When', class: 'cell-mono', cell: (r) => formatDate(r.created_at, true) },
        { label: 'Reason', cell: (r) => r.reason || 'save' },
        { label: 'By', cell: (r) => r.author },
        {
          label: '',
          cell: (r) => actionButton('Restore', async () => {
            await api.post(`/api/content/items/${item.id}/revisions/${r.id}/restore`, {});
            toast('Restored into the draft. Publish to push it live.');
            ctx.reload();
          }, { small: true }),
        },
      ], revisions, { empty: 'No revisions yet.' }),
    });
  }

  // =====================================================================
  // Taxonomy
  // =====================================================================
  async function taxonomy(ctx) {
    ctx.setHead('Categories & tags', 'Taxonomies group content and create archive URLs.');
    mount(ctx.el, ui.spinner());

    const [{ taxonomies }, { terms }, { types }] = await Promise.all([
      api.get('/api/content/taxonomies'),
      api.get('/api/content/terms'),
      api.get('/api/content/types'),
    ]);

    ctx.setHead('Categories & tags', `${taxonomies.length} taxonomy(ies), ${terms.length} term(s)`,
      h('div.row-actions', {}, [
        h('button.btn', {
          type: 'button', text: 'New taxonomy',
          onclick: () => editTaxonomy(ctx, types, null),
        }),
        h('button.btn.btn-primary', {
          type: 'button', text: 'New term',
          onclick: () => editTerm(ctx, taxonomies, terms, null),
        }),
      ]));

    mount(ctx.el, taxonomies.map((tax) => panel(
      tax.plural_name,
      table([
        {
          label: 'Name',
          cell: (t) => [
            h('div.cell-name', { text: (t.parent_id ? '— ' : '') + t.name }),
            h('div.cell-meta', { text: `/${tax.slug}/${t.slug}` }),
          ],
        },
        { label: 'Items', class: 'cell-mono', cell: (t) => t.item_count },
        {
          label: '',
          cell: (t) => h('div.row-actions', {}, [
            actionButton('Edit', () => editTerm(ctx, taxonomies, terms, t), { small: true }),
            confirmButton('Delete', async () => {
              await api.del(`/api/content/terms/${t.id}`);
              toast('Term deleted.');
              ctx.reload();
            }, { small: true }),
          ]),
        },
      ], terms.filter((t) => t.taxonomy === tax.slug), {
        empty: `No ${tax.plural_name.toLowerCase()} yet.`,
      }),
      h('div.row-actions', {}, [
        h('span.muted', {
          text: `applies to: ${(tax.type_slugs || []).join(', ') || 'nothing yet'}`,
        }),
        actionButton('Edit', () => editTaxonomy(ctx, types, tax), { small: true }),
        tax.is_builtin ? null : confirmButton('Delete', async () => {
          await api.del(`/api/content/taxonomies/${tax.id}`);
          toast('Taxonomy deleted.');
          ctx.reload();
        }, { small: true }),
      ]),
    )));
  }

  function editTerm(ctx, taxonomies, terms, term) {
    const taxSelect = select(taxonomies.map((t) => [t.slug, t.plural_name]),
      term?.taxonomy || taxonomies[0]?.slug, null);
    const parentOptions = [['', 'No parent'],
      ...terms.filter((t) => !term || t.id !== term.id).map((t) => [t.id, t.name])];

    formDrawer({
      title: term ? `Edit “${term.name}”` : 'New term',
      fields: [
        term ? null : { name: 'taxonomy', label: 'Taxonomy', control: taxSelect },
        { name: 'name', label: 'Name', control: textInput('name', term?.name) },
        { name: 'slug', label: 'Slug', control: textInput('slug', term?.slug,
          { placeholder: 'derived from the name' }) },
        { name: 'description', label: 'Description',
          control: textarea('description', term?.description, { rows: 3 }) },
        { name: 'parent_id', label: 'Parent',
          control: select(parentOptions, term?.parent_id, null),
          help: 'Only hierarchical taxonomies (categories) can nest.' },
      ],
      onSave: async (values) => {
        const body = {
          name: values.name,
          slug: values.slug || undefined,
          description: values.description || null,
          parent_id: values.parent_id ? Number(values.parent_id) : null,
        };
        if (term) await api.patch(`/api/content/terms/${term.id}`, body);
        else await api.post('/api/content/terms', { taxonomy: values.taxonomy, ...body });
        toast('Saved.');
        ctx.reload();
      },
    });
  }

  function editTaxonomy(ctx, types, tax) {
    const typeBoxes = types.map((t) =>
      h('label.chip', {}, [
        h('input', {
          type: 'checkbox', value: t.slug,
          checked: (tax?.type_slugs || []).includes(t.slug),
        }),
        h('span', { text: t.plural_name }),
      ]));
    const wrapper = h('div.chips', {}, typeBoxes);

    formDrawer({
      title: tax ? `Edit “${tax.plural_name}”` : 'New taxonomy',
      fields: [
        { name: 'name', label: 'Name (singular)', control: textInput('name', tax?.name) },
        { name: 'plural_name', label: 'Name (plural)',
          control: textInput('plural_name', tax?.plural_name) },
        tax ? null : { name: 'slug', label: 'Slug', control: textInput('slug', '') },
        tax ? null 
          : { name: 'is_hierarchical', label: 'Nesting',
            control: checkbox('is_hierarchical', false, 'Terms can have parents') },
        { label: 'Applies to', control: wrapper },
      ],
      onSave: async (values) => {
        const typeSlugs = [...wrapper.querySelectorAll('input:checked')].map((i) => i.value);
        const body = {
          name: values.name,
          plural_name: values.plural_name || undefined,
          type_slugs: typeSlugs,
        };
        if (tax) await api.patch(`/api/content/taxonomies/${tax.id}`, body);
        else {
          await api.post('/api/content/taxonomies', {
            slug: values.slug || values.name.toLowerCase().replace(/[^a-z0-9]+/g, '-'),
            is_hierarchical: Boolean(values.is_hierarchical),
            ...body,
          });
        }
        toast('Saved.');
        ctx.reload();
      },
    });
  }

  window.contentViews = { content, taxonomy };
}());
