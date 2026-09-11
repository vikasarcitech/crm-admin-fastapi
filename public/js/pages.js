/* global window, document, api, ui */
/**
 * Pages — WordPress-style builder view.
 *
 * #/pages          list of pages
 * #/pages?edit=ID  block editor: palette + block list on the left, a live
 *                  draft preview (served by /api/pages/{id}/preview) on
 *                  the right. Every edit auto-saves the draft; the live
 *                  page only changes on Publish.
 */
(function () {
  'use strict';

  const { h, mount, toast, openDrawer, closeDrawer, spinner, emptyState,
    field, select, formatDate, relativeTime } = ui;

  const isAdmin = (user) => ['owner', 'admin'].includes(user.role);

  // -------------------------------------------------- formatting toolbar
  // Text is stored as a small markdown subset; the server renders it with
  // escape-first substitutions, so the toolbar just inserts the syntax.
  function wrapSelection(ta, before, after) {
    const s = ta.selectionStart; const e = ta.selectionEnd;
    const sel = ta.value.slice(s, e) || 'text';
    ta.setRangeText(before + sel + after, s, e, 'end');
    ta.focus();
  }

  function linePrefix(ta, prefix) {
    const lineStart = ta.value.lastIndexOf('\n', ta.selectionStart - 1) + 1;
    ta.setRangeText(prefix, lineStart, lineStart, 'end');
    ta.focus();
  }

  function mdToolbar(ta, rich) {
    const btn = (label, title, fn, cls = '') => h(`button.md-btn${cls}`, {
      type: 'button', title, text: label,
      onmousedown: (e) => e.preventDefault(),   // keep the textarea selection
      onclick: fn,
    });

    const buttons = [
      btn('B', 'Bold (⌘B) — **text**', () => wrapSelection(ta, '**', '**'), '.md-b'),
      btn('I', 'Italic (⌘I) — *text*', () => wrapSelection(ta, '*', '*'), '.md-i'),
      btn('S', 'Strikethrough — ~~text~~', () => wrapSelection(ta, '~~', '~~'), '.md-s'),
      btn('</>', 'Code — `text`', () => wrapSelection(ta, '`', '`')),
      btn('Link', 'Link — [text](url)', () => {
        // eslint-disable-next-line no-alert
        const url = window.prompt('Link URL (https://…, mailto:, tel: or /path)');
        if (url) wrapSelection(ta, '[', `](${url.trim()})`);
      }),
    ];
    if (rich) {
      buttons.push(
        btn('H2', 'Heading — ## text', () => linePrefix(ta, '## ')),
        btn('H3', 'Subheading — ### text', () => linePrefix(ta, '### ')),
        btn('• List', 'Bulleted list — - item', () => linePrefix(ta, '- ')),
        btn('1. List', 'Numbered list — 1. item', () => linePrefix(ta, '1. ')),
        btn('❝', 'Quote — > text', () => linePrefix(ta, '> ')),
        btn('—', 'Divider — ---', () => linePrefix(ta, '---\n')),
      );
    }

    ta.addEventListener('keydown', (e) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      if (e.key === 'b') { e.preventDefault(); wrapSelection(ta, '**', '**'); }
      if (e.key === 'i') { e.preventDefault(); wrapSelection(ta, '*', '*'); }
    });

    return h('div', {}, [
      h('div.md-toolbar', {}, buttons),
      ta,
      h('p.md-hint', {
        text: rich
          ? '**bold**, *italic*, ~~strike~~, `code`, [link](url), ## heading, - list, > quote, --- divider. Blank line = new paragraph.'
          : '**bold**, *italic*, ~~strike~~, `code`, [link](url)',
      }),
    ]);
  }

  // ------------------------------------------------------- block library
  const BLOCKS = {
    hero: {
      label: 'Hero',
      make: () => ({ type: 'hero', heading: 'Your headline', sub: '', button_label: '', button_href: '', align: 'center' }),
      summary: (b) => b.heading,
      fields: [
        ['heading', 'Heading', 'text'],
        ['sub', 'Subheading', 'textarea'],
        ['button_label', 'Button label (optional)', 'text'],
        ['button_href', 'Button link', 'text'],
        ['align', 'Alignment', 'select', [['center', 'Centred'], ['left', 'Left']]],
      ],
    },
    heading: {
      label: 'Heading',
      make: () => ({ type: 'heading', text: 'Section heading', level: 2 }),
      summary: (b) => b.text,
      fields: [
        ['text', 'Text', 'text'],
        ['level', 'Size', 'select', [[2, 'Large'], [3, 'Medium'], [4, 'Small']], Number],
      ],
    },
    text: {
      label: 'Rich text',
      make: () => ({ type: 'text', body: 'Write something…' }),
      summary: (b) => (b.body || '').slice(0, 60),
      fields: [['body', 'Content', 'rich']],
    },
    image: {
      label: 'Image',
      make: () => ({ type: 'image', src: '', alt: '', caption: '' }),
      summary: (b) => b.src || 'no image yet',
      fields: [
        ['src', 'Image URL (https)', 'text'],
        ['alt', 'Alt text', 'text'],
        ['caption', 'Caption (optional)', 'text'],
      ],
    },
    button: {
      label: 'Button',
      make: () => ({ type: 'button', label: 'Learn more', href: '#', align: 'center', variant: 'solid' }),
      summary: (b) => b.label,
      fields: [
        ['label', 'Label', 'text'],
        ['href', 'Link', 'text'],
        ['align', 'Alignment', 'select', [['center', 'Centred'], ['left', 'Left']]],
        ['variant', 'Style', 'select', [['solid', 'Solid'], ['outline', 'Outline']]],
      ],
    },
    features: {
      label: 'Feature grid',
      make: () => ({
        type: 'features',
        heading: 'Why choose us',
        items: [{ title: 'Fast', body: 'Describe a benefit.' }, { title: 'Reliable', body: 'Describe another.' }],
      }),
      summary: (b) => `${(b.items || []).length} item(s)`,
      fields: [
        ['heading', 'Heading (optional)', 'text'],
        ['items', 'Items — one per line, as: Title | Description', 'items'],
      ],
    },
    quote: {
      label: 'Quote',
      make: () => ({ type: 'quote', body: 'A kind word from a customer.', attribution: '' }),
      summary: (b) => (b.body || '').slice(0, 60),
      fields: [['body', 'Quote', 'textarea'], ['attribution', 'Attribution (optional)', 'text']],
    },
    form: {
      label: 'Lead form',
      make: () => ({ type: 'form', form_slug: '', heading: 'Get in touch', button_label: 'Send' }),
      summary: (b) => b.form_slug ? `form: ${b.form_slug}` : 'pick a form',
      fields: [
        ['form_slug', 'Form', 'formselect'],
        ['heading', 'Heading (optional)', 'text'],
        ['button_label', 'Button label', 'text'],
      ],
    },
    html: {
      label: 'HTML',
      make: () => ({
        type: 'html',
        html: '<h2>Heading</h2>\n<p>Paste or write HTML here.</p>',
        styled: true,
        width: 'normal',
      }),
      summary: (b) => {
        // A tag soup preview is useless in the list; show the text.
        const text = (b.html || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
        return text ? text.slice(0, 60) : 'empty HTML block';
      },
      fields: [
        ['html', 'HTML', 'code'],
        ['styled', 'Typography', 'toggle',
          [[true, 'Use the page’s fonts and spacing'],
            [false, 'Leave the markup unstyled']]],
        ['width', 'Width', 'select',
          [['normal', 'Content width'], ['wide', 'Wide'], ['full', 'Full bleed']]],
      ],
    },
    spacer: {
      label: 'Spacer',
      make: () => ({ type: 'spacer', size: 'medium' }),
      summary: (b) => b.size,
      fields: [['size', 'Size', 'select', [['small', 'Small'], ['medium', 'Medium'], ['large', 'Large']]]],
    },
    divider: {
      label: 'Divider',
      make: () => ({ type: 'divider' }),
      summary: () => 'horizontal rule',
      fields: [],
    },
  };

  const slugify = (text) => text.toLowerCase().trim()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80);

  // ================================================================ list
  async function pages(ctx) {
    if (ctx.params.edit) return editor(ctx, ctx.params.edit);

    const admin = isAdmin(ctx.session.user);
    const newBtn = h('button.btn.btn-primary', {
      type: 'button', text: 'New page', onclick: () => newPageForm(ctx),
    });
    ctx.setHead('Pages', 'Build and publish web pages without touching code.', admin ? newBtn : null);
    mount(ctx.el, spinner());

    const { pages: list } = await api.get('/api/pages');
    const tenant = ctx.session.user.tenantSlug;

    if (!list.length) {
      return mount(ctx.el, emptyState('No pages yet',
        'Create a page, arrange its blocks, then publish it at its own URL.',
        admin ? h('button.btn', { type: 'button', text: 'Create your first page', onclick: () => newPageForm(ctx) }) : null));
    }

    mount(ctx.el, h('table.list', {}, [
      h('thead', {}, h('tr', {}, [
        h('th', { text: 'Page' }), h('th', { text: 'Status' }),
        h('th', { class: 'hide-sm', text: 'Last edited' }), h('th', { text: '' }),
      ])),
      h('tbody', {}, list.map((p) => h('tr', {}, [
        h('td', {}, [
          h('a.cell-name', { href: `#/pages?edit=${p.id}`, text: p.title }),
          h('div.cell-meta.mono', { text: `/p/${tenant}/${p.slug}` }),
        ]),
        h('td', {}, [
          h(`span.pill.s-${p.status}`, { text: p.status === 'published' ? 'Published' : 'Draft' }),
          p.published_at ? h('div.cell-meta', { text: `live since ${formatDate(p.published_at)}` }) : null,
        ]),
        h('td.hide-sm', {}, [
          h('div.cell-mono', { text: relativeTime(p.updated_at) }),
          p.updated_by_name ? h('div.cell-meta', { text: `by ${p.updated_by_name}` }) : null,
        ]),
        h('td', {}, h('div.row-actions', {}, [
          p.status === 'published'
            ? h('a.btn.btn-sm', { href: `/p/${tenant}/${p.slug}`, target: '_blank', rel: 'noopener', text: 'View' })
            : null,
          h('button.btn.btn-sm', { type: 'button', text: 'Edit', onclick: () => ctx.navigate(`#/pages?edit=${p.id}`) }),
          admin ? h('button.btn.btn-sm.btn-danger', {
            type: 'button', text: 'Delete',
            onclick: async () => {
              // eslint-disable-next-line no-alert
              if (!window.confirm(`Delete "${p.title}"? The live page goes down immediately.`)) return;
              try { await api.del(`/api/pages/${p.id}`); toast('Page deleted'); ctx.reload(); }
              catch (err) { toast(err.message, 'error'); }
            },
          }) : null,
        ])),
      ]))),
    ]));
  }

  function newPageForm(ctx) {
    const title = h('input', { type: 'text', placeholder: 'Spring campaign' });
    const slug = h('input', { type: 'text', placeholder: 'spring-campaign' });
    let slugTouched = false;
    slug.addEventListener('input', () => { slugTouched = true; });
    title.addEventListener('input', () => { if (!slugTouched) slug.value = slugify(title.value); });

    const submit = h('button.btn.btn-primary', {
      type: 'button', text: 'Create page',
      onclick: async () => {
        submit.disabled = true;
        try {
          const res = await api.post('/api/pages', { title: title.value.trim(), slug: slug.value.trim() });
          closeDrawer();
          toast('Page created');
          ctx.navigate(`#/pages?edit=${res.page.id}`);
        } catch (err) { toast(err.message, 'error'); submit.disabled = false; }
      },
    });

    openDrawer({
      title: 'New page',
      subtitle: 'It starts as a draft — publish it when it is ready.',
      body: [field('Title', title), field('URL slug', slug), submit],
    });
  }

  // ============================================================== editor
  async function editor(ctx, id) {
    mount(ctx.el, spinner());

    let page, settings;
    try {
      [{ page }, settings] = await Promise.all([
        api.get(`/api/pages/${id}`),
        api.get('/api/settings'),
      ]);
    } catch (err) {
      toast(err.message, 'error');
      return ctx.navigate('#/pages');
    }

    const admin = isAdmin(ctx.session.user);
    const tenant = ctx.session.user.tenantSlug;
    const formOptions = (settings.forms || []).filter((f) => f.is_active)
      .map((f) => [f.slug, f.name]);
    let blocks = Array.isArray(page.blocks) ? page.blocks : [];

    const iframe = h('iframe', {
      src: `/api/pages/${id}/preview`, title: 'Page preview',
    });
    const refreshPreview = () => { iframe.src = `/api/pages/${id}/preview?ts=${Date.now()}`; };

    function setHead() {
      const liveUrl = `/p/${tenant}/${page.slug}`;
      const actions = h('div.row-actions', {}, [
        h('a.btn.btn-sm', { href: '#/pages', text: '← All pages' }),
        admin ? h('button.btn.btn-sm', { type: 'button', text: 'Page settings', onclick: settingsDrawer }) : null,
        admin ? h('button.btn.btn-sm', { type: 'button', text: 'History', onclick: historyDrawer }) : null,
        page.status === 'published'
          ? h('a.btn.btn-sm', { href: liveUrl, target: '_blank', rel: 'noopener', text: 'View live' })
          : null,
        admin ? (page.status === 'published'
          ? h('button.btn.btn-sm', {
            type: 'button', text: 'Unpublish',
            onclick: async () => {
              try { ({ page } = await api.post(`/api/pages/${id}/unpublish`)); toast('Page taken offline'); setHead(); }
              catch (err) { toast(err.message, 'error'); }
            },
          })
          : null) : null,
        admin ? h('button.btn.btn-primary.btn-sm', {
          type: 'button', text: page.status === 'published' ? 'Publish changes' : 'Publish',
          onclick: async () => {
            try {
              ({ page } = await api.post(`/api/pages/${id}/publish`));
              toast(`Live at ${liveUrl}`);
              setHead();
            } catch (err) { toast(err.message, 'error'); }
          },
        }) : null,
      ]);
      ctx.setHead(page.title,
        `${liveUrl} · ${page.status === 'published' ? 'published' : 'draft'}`, actions);
    }

    async function saveBlocks() {
      try {
        ({ page } = await api.patch(`/api/pages/${id}`, { blocks }));
        blocks = page.blocks;
        renderBlockList();
        refreshPreview();
      } catch (err) { toast(err.message, 'error'); }
    }

    // ------------------------------------------------------- block list
    const blockList = h('div.pb-list');

    function renderBlockList() {
      if (!blocks.length) {
        return mount(blockList, h('p.muted', { text: 'No blocks yet — add one below.' }));
      }
      mount(blockList, blocks.map((block, index) => {
        const def = BLOCKS[block.type] || { label: block.type, summary: () => '' };
        return h('div.pb-row', {}, [
          h('div.pb-row-main', {
            onclick: admin && (BLOCKS[block.type]?.fields || []).length
              ? () => blockDrawer(block, index) : null,
            style: admin ? 'cursor:pointer' : '',
          }, [
            h('div.cell-name', { text: def.label }),
            h('div.cell-meta', { text: def.summary(block) || '—' }),
          ]),
          admin ? h('div.pb-row-actions', {}, [
            h('button.icon-btn', {
              type: 'button', text: '↑', 'aria-label': 'Move up', disabled: index === 0,
              onclick: () => { [blocks[index - 1], blocks[index]] = [blocks[index], blocks[index - 1]]; saveBlocks(); },
            }),
            h('button.icon-btn', {
              type: 'button', text: '↓', 'aria-label': 'Move down', disabled: index === blocks.length - 1,
              onclick: () => { [blocks[index + 1], blocks[index]] = [blocks[index], blocks[index + 1]]; saveBlocks(); },
            }),
            h('button.icon-btn', {
              type: 'button', text: '×', 'aria-label': 'Remove block',
              onclick: () => { blocks.splice(index, 1); saveBlocks(); },
            }),
          ]) : null,
        ]);
      }));
    }

    // ------------------------------------------------------ block editor
    function blockDrawer(block, index) {
      const def = BLOCKS[block.type];
      const controls = [];

      def.fields.forEach(([key, label, kind, options, coerce]) => {
        let control;
        if (kind === 'code') {
          control = h('textarea', {
            rows: 18,
            class: 'pb-code',
            spellcheck: 'false',
            value: block[key] || '',
          });
        } else if (kind === 'toggle') {
          // A select rather than a checkbox: the two options each need a
          // sentence to be understandable, which a checkbox label cannot
          // carry well.
          control = select(options, block[key] !== false, null);
        } else if (kind === 'textarea' || kind === 'rich') {
          control = h('textarea', { rows: kind === 'rich' ? 14 : 6, value: block[key] || '' });
        } else if (kind === 'select') {
          control = select(options, block[key], null);
        } else if (kind === 'formselect') {
          control = select([['', 'Choose a form…'], ...formOptions], block[key], null);
        } else if (kind === 'items') {
          control = h('textarea', {
            rows: 6,
            value: (block[key] || []).map((i) => [i.title, i.body].filter(Boolean).join(' | ')).join('\n'),
          });
        } else {
          control = h('input', { type: 'text', value: block[key] ?? '' });
        }
        controls.push({ key, kind, coerce, control });
      });

      const submit = h('button.btn.btn-primary', {
        type: 'button', text: 'Apply',
        onclick: async () => {
          controls.forEach(({ key, kind, coerce, control }) => {
            if (kind === 'items') {
              block[key] = control.value.split('\n').map((line) => {
                const [title, ...rest] = line.split('|');
                return { title: (title || '').trim(), body: rest.join('|').trim() };
              }).filter((i) => i.title);
            } else if (kind === 'toggle') {
              // select values are strings; the block field is a boolean.
              block[key] = control.value === 'true';
            } else {
              block[key] = coerce ? coerce(control.value) : control.value;
            }
          });
          blocks[index] = block;
          closeDrawer();
          await saveBlocks();
        },
      });

      openDrawer({
        // Lowercased so it reads as a sentence ("Edit hero") — but an
        // all-caps label is an acronym and must survive as one.
        title: `Edit ${def.label === def.label.toUpperCase() ? def.label : def.label.toLowerCase()}`,
        subtitle: 'Changes save to the draft — the live page updates on publish.',
        body: [
          ...controls.map(({ kind, control }, i) => field(
            def.fields[i][1],
            (kind === 'textarea' || kind === 'rich')
              ? mdToolbar(control, kind === 'rich')
              : control,
          )),
          // Said once, in the place where someone is about to paste
          // something: what will survive the save, and what will not.
          block.type === 'html'
            ? h('div.notice', { dataset: { kind: 'info' } }, [
              h('strong', { text: 'HTML is sanitized when you save. ' }),
              'Structure, links, lists, tables, images and figures are kept. '
              + 'Scripts, event handlers, inline styles and javascript: links are '
              + 'removed. Embeds work only from the allow-listed hosts (YouTube, '
              + 'Vimeo, Spotify, SoundCloud, Google Maps, Calendly) — an iframe '
              + 'pointing anywhere else is dropped, not left broken.',
            ])
            : null,
          submit,
        ],
      });
    }

    // ------------------------------------------------------ page settings
    function settingsDrawer() {
      const theme = page.theme || {};
      const title = h('input', { type: 'text', value: page.title });
      const slug = h('input', { type: 'text', value: page.slug });
      const description = h('textarea', { rows: 3, value: page.description || '' });
      const primary = h('input', { type: 'color', value: theme.primary || '#0b6e5a' });
      const background = h('input', { type: 'color', value: theme.background || '#ffffff' });
      const text = h('input', { type: 'color', value: theme.text || '#1c2422' });
      const font = select([['system', 'Modern (sans-serif)'], ['serif', 'Classic (serif)'], ['mono', 'Technical (mono)']], theme.font || 'system');
      const width = select([['narrow', 'Narrow'], ['normal', 'Normal'], ['wide', 'Wide']], theme.max_width || 'normal');

      const submit = h('button.btn.btn-primary', {
        type: 'button', text: 'Save settings',
        onclick: async () => {
          submit.disabled = true;
          try {
            ({ page } = await api.patch(`/api/pages/${id}`, {
              title: title.value.trim(),
              slug: slug.value.trim(),
              description: description.value.trim(),
              theme: {
                primary: primary.value, background: background.value, text: text.value,
                font: font.value, max_width: width.value,
              },
            }));
            closeDrawer();
            setHead();
            refreshPreview();
            toast('Settings saved');
          } catch (err) { toast(err.message, 'error'); submit.disabled = false; }
        },
      });

      openDrawer({
        title: 'Page settings',
        subtitle: 'Title, URL, search snippet and theme.',
        body: [
          field('Title', title),
          field('URL slug', slug),
          field('Meta description (search results)', description),
          field('Accent colour', primary),
          field('Background colour', background),
          field('Text colour', text),
          field('Typeface', font),
          field('Content width', width),
          submit,
        ],
      });
    }

    // --------------------------------------------------------- revisions
    async function historyDrawer() {
      const drawer = openDrawer({ title: 'Publish history', subtitle: 'Restore any version into the draft.', body: spinner() });
      let revisions;
      try {
        ({ revisions } = await api.get(`/api/pages/${id}/revisions`));
      } catch (err) { toast(err.message, 'error'); return closeDrawer(); }

      mount(drawer.body, revisions.length ? revisions.map((r) => h('div.pb-row', {}, [
        h('div.pb-row-main', {}, [
          h('div.cell-name', { text: r.title }),
          h('div.cell-meta', { text: `${r.author || 'system'} · ${formatDate(r.created_at, true)}` }),
        ]),
        h('button.btn.btn-sm', {
          type: 'button', text: 'Restore',
          onclick: async () => {
            try {
              ({ page } = await api.post(`/api/pages/${id}/revisions/${r.id}/restore`));
              blocks = page.blocks;
              closeDrawer();
              setHead();
              renderBlockList();
              refreshPreview();
              toast('Revision restored into the draft');
            } catch (err) { toast(err.message, 'error'); }
          },
        }),
      ])) : [h('p.muted', { text: 'Nothing published yet — each publish is recorded here.' })]);
    }

    // ------------------------------------------------------------ layout
    const palette = admin ? h('div.pb-palette', {}, Object.entries(BLOCKS).map(([type, def]) =>
      h('button.btn.btn-sm', {
        type: 'button', text: `+ ${def.label}`,
        onclick: () => {
          const block = def.make();
          blocks.push(block);
          if (def.fields.length) blockDrawer(block, blocks.length - 1);
          else saveBlocks();
        },
      }))) : h('p.muted', { text: 'Admins can edit this page.' });

    setHead();
    renderBlockList();
    mount(ctx.el, h('div.pb-editor', {}, [
      h('div.pb-side', {}, [
        h('section.panel', {}, [
          h('div.panel-head', {}, [h('h2', { text: 'Blocks' })]),
          h('div.panel-body', {}, [blockList, h('div', { style: 'height:12px' }), palette]),
        ]),
      ]),
      h('div.pb-preview', {}, iframe),
    ]));
  }

  window.views.pages = pages;
}());
