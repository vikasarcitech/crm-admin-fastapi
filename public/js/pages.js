/* global window, document, api, kit, ui */
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
  // The same character counter the content editor puts under these
  // fields, so a page's SEO title reads against the same targets.
  const { counted } = kit;

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

  // ---------------------------------------------------------- HTML editor
  /**
   * The HTML block's editor.
   *
   * Three parts, and the third is the point: a code pane with line
   * numbers and indent-aware keys, a bar of snippets for the markup this
   * platform actually allows, and a live report from the server's own
   * sanitizer (POST /api/pages/html/check). The report runs the same
   * cleaner the save runs, so an author learns that their <script> or
   * style="" is going away while they can still do something about it,
   * instead of finding it gone afterwards.
   */
  const INDENT = '  ';

  // No closing tag, so they never open an indent level.
  const VOID_TAGS = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img',
    'input', 'link', 'meta', 'source', 'track', 'wbr']);
  // Elements that live inside a line of prose. Re-indenting these onto
  // their own line would add whitespace the browser then renders.
  const INLINE_TAGS = new Set(['a', 'abbr', 'b', 'br', 'cite', 'code', 'del', 'em',
    'i', 'img', 'ins', 'kbd', 'mark', 'q', 's', 'small', 'span', 'strong', 'sub',
    'sup', 'time', 'u', 'wbr',
    // Controls and the legacy inline tags: all of these live in the
    // middle of a line, and <select>/<fieldset> deliberately do not —
    // their children belong one per line.
    'label', 'input', 'button', 'output', 'data', 'samp', 'var', 'bdi', 'bdo',
    'font', 'big', 'tt', 'nobr', 'strike', 'acronym', 'blink']);

  // Grouped, because the menu that inserts them is grouped: a dozen
  // buttons in a row is a list you have to read, three short groups is
  // one you can scan. Named for what they put on the page, not for the
  // tag they are spelled with — "Dropdown", not "<select>".
  const HTML_SNIPPETS = [
    ['Text', [
      ['Heading', '<h2>Section heading</h2>\n'],
      ['Paragraph', '<p>Paragraph text.</p>\n'],
      ['List', '<ul>\n  <li>First item</li>\n  <li>Second item</li>\n</ul>\n'],
      ['Quote', '<blockquote>\n  <p>Quoted text.</p>\n</blockquote>\n'],
      ['Link', '<a href="https://example.com">link text</a>'],
    ]],
    ['Media', [
      ['Image', '<figure>\n  <img src="/media/example.jpg" alt="">\n  <figcaption>Caption</figcaption>\n</figure>\n'],
      ['Video embed', '<iframe src="https://www.youtube.com/embed/VIDEO_ID" allowfullscreen></iframe>\n'],
      ['Table', '<table>\n  <thead>\n    <tr><th>Plan</th><th>Price</th></tr>\n  </thead>\n  <tbody>\n    <tr><td>Starter</td><td>Free</td></tr>\n  </tbody>\n</table>\n'],
      ['Scrolling text', '<marquee behavior="scroll" direction="left" scrollamount="5">Launch week — 20% off</marquee>\n'],
    ]],
    ['Interactive', [
      ['Form', '<form action="https://example.com/subscribe" method="post">\n'
        + '  <label for="email">Email</label>\n'
        + '  <input type="email" id="email" name="email" placeholder="you@company.com" required>\n'
        + '  <button type="submit">Subscribe</button>\n</form>\n'],
      ['Dropdown', '<label for="plan">Plan</label>\n<select id="plan" name="plan">\n'
        + '  <option value="starter">Starter</option>\n'
        + '  <option value="pro" selected>Pro</option>\n</select>\n'],
      ['Accordion', '<details>\n  <summary>Question</summary>\n  <p>Answer.</p>\n</details>\n'],
    ]],
  ];

  /** Insert markup at the cursor, lined up with the current indentation. */
  function insertSnippet(ta, snippet) {
    const { value, selectionStart: s, selectionEnd: e } = ta;
    const lineStart = value.lastIndexOf('\n', s - 1) + 1;
    const indent = (value.slice(lineStart, s).match(/^[ \t]*/) || [''])[0];
    const text = indent ? snippet.replace(/\n(?=[^\n])/g, `\n${indent}`) : snippet;
    ta.setRangeText(text, s, e, 'end');
    ta.focus();
  }

  /** Tab / Shift+Tab across every line the selection touches. */
  function indentLines(ta, outdent) {
    const { value, selectionStart: s, selectionEnd: e } = ta;
    const from = value.lastIndexOf('\n', s - 1) + 1;
    const lineEnd = value.indexOf('\n', e);
    const to = lineEnd === -1 ? value.length : lineEnd;
    const lines = value.slice(from, to).split('\n')
      .map((line) => (outdent ? line.replace(/^( {1,2}|\t)/, '') : INDENT + line));
    ta.setRangeText(lines.join('\n'), from, to, 'select');
  }

  /** Enter keeps the current indent, and steps in after an opening tag. */
  function autoIndent(ta) {
    const { value, selectionStart: s } = ta;
    const lineStart = value.lastIndexOf('\n', s - 1) + 1;
    const before = value.slice(lineStart, s);
    const indent = (before.match(/^[ \t]*/) || [''])[0];
    const open = before.trimEnd().match(/<([a-zA-Z][\w-]*)(?:\s[^<>]*)?>$/);
    const opens = Boolean(open)
      && !VOID_TAGS.has(open[1].toLowerCase())
      && !before.trimEnd().endsWith('/>');
    const inner = `\n${indent}${opens ? INDENT : ''}`;

    // Enter between an opening tag and its close: leave the caret on a
    // blank indented line and push the close tag down, as an editor does.
    if (opens && /^[ \t]*<\//.test(value.slice(s))) {
      ta.setRangeText(`${inner}\n${indent}`, s, s, 'end');
      ta.selectionStart = s + inner.length;
      ta.selectionEnd = ta.selectionStart;
    } else {
      ta.setRangeText(inner, s, s, 'end');
    }
  }

  /**
   * Re-indent markup: one block element per line, inline elements and
   * text left attached to their line. Returns null when the markup
   * cannot be touched safely.
   */
  function tidyHtml(src) {
    // <pre> and <textarea> render their whitespace literally, so
    // re-indenting them would change what a visitor sees.
    if (/<(pre|textarea)\b/i.test(src)) return null;

    const tokens = src.match(/<[^>]*>|[^<]+/g) || [];
    const out = [];
    let depth = 0;
    let line = '';
    const pad = () => INDENT.repeat(Math.max(depth, 0));
    const flush = () => { if (line.trim()) out.push(line); line = ''; };

    tokens.forEach((token) => {
      if (token[0] !== '<') {
        const text = token.replace(/\s+/g, ' ');
        if (!text.trim()) return;                 // whitespace between tags
        line = line ? line + text : pad() + text.replace(/^ /, '');
        return;
      }
      // Comments and doctypes: kept on their own line (the sanitizer
      // drops comments, but the editor should not rearrange them).
      if (token.startsWith('<!')) { flush(); out.push(pad() + token); return; }

      const name = ((token.match(/^<\/?\s*([a-zA-Z][\w-]*)/) || [])[1] || '').toLowerCase();
      if (INLINE_TAGS.has(name)) { line = (line || pad()) + token; return; }

      if (token.startsWith('</')) {
        depth = Math.max(depth - 1, 0);
        // A line already holding its opening tag stays a single line —
        // <li>text</li> should not become three lines.
        out.push(line.trim() ? line + token : pad() + token);
        line = '';
        return;
      }

      flush();
      if (VOID_TAGS.has(name) || /\/>$/.test(token)) { out.push(pad() + token); return; }
      line = pad() + token;
      depth += 1;
    });

    flush();
    return out.join('\n');
  }

  /**
   * Build the editor. Returns the node to drop into the drawer, the
   * textarea whose value is read on save, and start(), to be called once
   * the node is in the document.
   */
  function htmlEditor(initial, {
    compact = false, rows = 0, onClean = null, onInput = null,
    // The whole page, rather than one block of it: the only place
    // markup is allowed to be a whole <html> document.
    wholePage = false, onDocument = null,
  } = {}) {
    const ta = h('textarea.pb-code', {
      rows: rows || (compact ? 12 : 24),
      spellcheck: 'false',
      autocapitalize: 'off',
      autocomplete: 'off',
      wrap: 'off',
      'aria-label': 'HTML source',
      value: initial || '',
    });
    const gutter = h('div.pb-gutter', { 'aria-hidden': 'true' });
    const report = h('div.pb-check', { dataset: { state: 'idle' } });

    function syncGutter() {
      const lines = ta.value.split('\n').length;
      if (gutter.childElementCount !== lines) {
        mount(gutter, Array.from({ length: lines },
          (_, i) => h('span', { text: String(i + 1) })));
      }
      gutter.scrollTop = ta.scrollTop;
    }

    // ---------------------------------------------------- sanitizer report
    let timer = null;
    let latest = 0;

    const show = (state, children) => {
      report.dataset.state = state;
      mount(report, children);
    };

    function cleanedView(cleaned) {
      const view = h('textarea.pb-code', {
        rows: 10, readonly: 'readonly', wrap: 'off', value: cleaned,
        'aria-label': 'Markup that will be saved',
      });
      return [
        // The fix comes first and reads as an offer, because taking it
        // is what almost everyone wants: the alternative is reading a
        // diff of their own paste.
        h('button.btn.btn-sm', {
          type: 'button', text: 'Fix it for me',
          onclick: () => {
            ta.value = cleaned;
            syncGutter();
            check();
          },
        }),
        h('details.pb-check-detail', {}, [
          h('summary', { text: 'See what saving keeps' }),
          view,
        ]),
      ];
    }

    async function check() {
      const source = ta.value;
      if (!source.trim()) {
        onClean?.('');
        return show('idle', h('p', { text: 'The block is empty — nothing to check yet.' }));
      }
      const ticket = latest + 1;
      latest = ticket;

      let res;
      try {
        res = await api.post('/api/pages/html/check', { html: source, whole_page: wholePage });
      } catch (err) {
        if (ticket === latest) show('error', h('p', { text: err.message }));
        return undefined;
      }
      // A later keystroke already has its own request out; that answer wins.
      if (ticket !== latest) return undefined;

      // The preview follows the *cleaned* markup, never the raw
      // textarea: it is the string the save would store, and it has
      // been through the allow-list, so nothing unvetted is ever put
      // into a same-origin document. A whole document is the exception
      // and says so — it is never injected, only reloaded.
      onDocument?.(Boolean(res.document));
      if (!res.document) onClean?.(res.html);

      if (res.document) {
        return show('ok', [
          h('p', {}, [
            h('strong', { text: 'Full HTML document. ' }),
            'Saved and served exactly as written — <html> to </html>, your '
            + '<head>, your scripts. Nothing is removed.',
          ]),
          h('p.md-hint', { text: 'The preview reloads on save; it does not follow your typing, because the page runs its own scripts.' }),
        ]);
      }
      if (res.document_blocked) {
        return show('warn', [
          h('p', {}, [
            h('strong', { text: 'This is a whole HTML document. ' }),
            'Saving whole documents needs the "pages.raw_html" permission, '
            + 'which your role does not have — so this would be cleaned into '
            + 'a fragment instead: <head>, <script> and the rest dropped.',
          ]),
          h('p.md-hint', { text: 'An owner can grant it in Settings → Roles → Site.' }),
          cleanedView(res.html),
        ]);
      }

      if (!res.html.trim()) {
        return show('error', h('p', {}, [
          h('strong', { text: 'None of this can be saved. ' }),
          'It is all script or tags this platform does not allow — the block '
          + 'would come out empty.',
        ]));
      }
      if (!res.changed) {
        return show('ok', h('p', { text: 'All good — this saves exactly as you wrote it.' }));
      }

      // Named, not counted: "onclick" tells someone what to go look for,
      // "3 issues" sends them hunting.
      const removed = [
        ...(res.removed_tags || []).map((tag) => `<${tag}>`),
        ...(res.removed_attributes || []).map((attr) => attr),
      ];
      return show('warn', [
        h('p', {}, [
          removed.length
            ? `Dropped when you save: ${removed.join(', ')}. The rest is kept.`
            : 'Your tags will be tidied up (closed and re-quoted) on save. '
              + 'Nothing is lost.',
        ]),
        cleanedView(res.html),
      ]);
    }

    const schedule = () => {
      clearTimeout(timer);
      timer = setTimeout(check, 350);
    };

    // ------------------------------------------------------------ toolbar
    const tool = (label, title, fn) => h('button.md-btn', {
      type: 'button', title, text: label,
      onmousedown: (e) => e.preventDefault(),   // keep the caret where it is
      onclick: () => { fn(); syncGutter(); schedule(); },
    });

    // One menu instead of a row of buttons. It returns to "Insert…"
    // after each pick, so it reads as an action and never as a setting
    // that is now stuck on "Table".
    const inserter = h('select.md-btn.pb-insert', {
      'aria-label': 'Insert markup',
      onchange: (event) => {
        const [group, item] = event.target.value.split(':');
        const snippet = HTML_SNIPPETS[group]?.[1][item]?.[1];
        event.target.value = '';
        if (!snippet) return;
        insertSnippet(ta, snippet);
        syncGutter();
        schedule();
      },
    }, [
      h('option', { value: '', text: 'Insert…' }),
      ...HTML_SNIPPETS.map(([group, items], gi) => h('optgroup', { label: group },
        items.map(([label], ii) => h('option', { value: `${gi}:${ii}`, text: label })))),
    ]);

    const toolbar = h('div.md-toolbar', {}, [
      inserter,
      tool('Tidy', 'Re-indent the markup', () => {
        const tidied = tidyHtml(ta.value);
        if (tidied === null) {
          toast('Tidy leaves <pre> and <textarea> alone — their spacing is part of the content.');
          return;
        }
        ta.value = tidied;
      }),
    ]);

    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') {
        e.preventDefault();
        if (!e.shiftKey && ta.selectionStart === ta.selectionEnd) {
          ta.setRangeText(INDENT, ta.selectionStart, ta.selectionEnd, 'end');
        } else {
          indentLines(ta, e.shiftKey);
        }
        syncGutter();
        schedule();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        autoIndent(ta);
        syncGutter();
        schedule();
      }
    });
    ta.addEventListener('input', () => { syncGutter(); schedule(); onInput?.(); });
    ta.addEventListener('scroll', () => { gutter.scrollTop = ta.scrollTop; });

    // One sentence of standing rules, and the exceptions folded away
    // under it. The live report above already answers the only question
    // that matters most of the time — what happens to *this* markup —
    // so the general case does not get to cost the same room.
    const EXCEPTIONS = [
      ['Scripts', 'anything that runs: <script>, onclick and the other on… '
        + 'attributes, javascript: links, <object> and <embed>.'],
      ['Embeds', 'an <iframe> works only for YouTube, Vimeo, Spotify, '
        + 'SoundCloud, Google Maps and Calendly. Others are dropped.'],
      ['Forms', 'kept and shown, and they post wherever their action says '
        + '(https only). To collect leads in this CRM, use a Lead form block '
        + 'instead.'],
      ['CSS', 'kept — style attributes and <style> blocks both. Only @import '
        + 'and a url() pointing somewhere other than https or your own media '
        + 'are taken out.'],
    ];

    // The one way past all four: hand the page a whole document.
    const documentRule = h('p.pb-rule-doc', {}, [
      h('strong', { text: 'Or paste a whole document. ' }),
      'If the markup starts with <!doctype html> or <html>, the page *is* '
      + 'that file — head, scripts, CDN links and all, served byte-for-byte '
      + 'with nothing removed. It needs the "pages.raw_html" permission '
      + '(owners have it) and an HTML page to live on.',
    ]);

    const rules = [
      h('p.pb-rule-lede', {}, [
        h('strong', { text: 'Write any HTML you like. ' }),
        'Tables, forms, dropdowns, marquee, SVG, styles — it all works. '
        + 'The four exceptions:',
      ]),
      h('ul.pb-rule-list', {}, EXCEPTIONS.map(([term, detail]) => h('li', {}, [
        h('strong', { text: `${term}: ` }), detail,
      ]))),
      wholePage ? documentRule : null,
    ];

    const node = h('div.pb-html-editor', {}, [
      h('div.pb-code-pane', {}, [toolbar, h('div.pb-code-wrap', {}, [gutter, ta])]),
      h('div.pb-code-side', {}, [
        report,
        h('details.pb-rules', { open: compact ? null : 'open' }, [
          h('summary', { text: 'What HTML is allowed' }),
          h('div.notice', { dataset: { kind: 'info' } }, rules),
        ]),
      ]),
    ]);

    return {
      node,
      textarea: ta,
      start() { syncGutter(); check(); },
      stop() { clearTimeout(timer); },
    };
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
      // No drawer fields: an HTML block is edited in the code section
      // under Blocks, not in a popup — markup needs the preview next to
      // it. An empty list is what keeps the drawer from opening for it.
      fields: [],
      styledOptions: [[true, 'Use the page’s fonts and spacing'],
        [false, 'Leave the markup unstyled']],
      widthOptions: [['normal', 'Content width'], ['wide', 'Wide'], ['full', 'Full bleed']],
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
          // Worth saying in the list: the two kinds of page open onto
          // completely different editors.
          p.mode === 'html' ? h('span.pill', { text: 'HTML' }) : null,
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
    const metaTitle = h('input', { type: 'text', maxlength: 80, placeholder: 'defaults to the page title' });
    const description = h('textarea', { rows: 3, maxlength: 300 });
    const starter = select([
      ['blocks', 'Blocks — a hero and a paragraph to edit'],
      ['html', 'HTML — write the whole page as markup'],
    ], 'blocks');
    let slugTouched = false;
    slug.addEventListener('input', () => { slugTouched = true; });
    title.addEventListener('input', () => { if (!slugTouched) slug.value = slugify(title.value); });

    const submit = h('button.btn.btn-primary', {
      type: 'button', text: 'Create page',
      onclick: async () => {
        submit.disabled = true;
        try {
          const res = await api.post('/api/pages', {
            title: title.value.trim(),
            slug: slug.value.trim(),
            description: description.value.trim(),
            seo: { meta_title: metaTitle.value.trim() },
            starter: starter.value,
          });
          closeDrawer();
          toast('Page created');
          // An HTML page comes back in HTML mode, so its editor is the
          // screen it lands on — nothing more to point at.
          ctx.navigate(`#/pages?edit=${res.page.id}`);
        } catch (err) { toast(err.message, 'error'); submit.disabled = false; }
      },
    });

    openDrawer({
      title: 'New page',
      subtitle: 'It starts as a draft — publish it when it is ready.',
      body: [
        field('Title', title),
        field('URL slug', slug),
        field('Start with', starter),
        // Asked for here rather than only in page settings: these two
        // tags are what a search result and a shared link show, and a
        // page published without them shows whatever Google picks.
        field('SEO title (optional)', counted(metaTitle, 30, 60, '')),
        field('Meta description', counted(description, 70, 160, '')),
        h('p.md-hint', { text: 'Both can be changed any time in Page settings, along with the social and canonical tags.' }),
        submit,
      ],
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
    // A reload replaces the document the live edit was written into, so
    // put it back — but only while there is an unsaved edit to put back,
    // or a restored revision would be overwritten by stale markup.
    iframe.addEventListener('load', () => {
      if (codeEditor && codeLastClean !== null && codeEditor.textarea.value !== codeSaved) {
        livePreview(codeLastClean);
      }
    });

    function setHead() {
      const liveUrl = `/p/${tenant}/${page.slug}`;
      const actions = h('div.row-actions', {}, [
        h('a.btn.btn-sm', { href: '#/pages', text: '← All pages' }),
        admin ? h('button.btn.btn-sm', { type: 'button', text: 'Page settings', onclick: settingsDrawer }) : null,
        admin ? h('button.btn.btn-sm', {
          type: 'button',
          text: page.mode === 'html' ? 'Edit as blocks' : 'Edit as HTML',
          onclick: () => switchMode(page.mode === 'html' ? 'blocks' : 'html'),
        }) : null,
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
        // Only when the HTML blocks themselves changed: rebuilding the
        // code section on every save would throw away the caret and the
        // scroll position of whoever is typing in it. The whole-page
        // view is different — every block edit changes what it shows —
        // so it re-reads, unless there is unsaved typing in it to lose.
        if (codeIndex === WHOLE_PAGE) {
          if (!codeEditor || codeEditor.textarea.value === codeSaved) renderCodeSection();
        } else if (htmlSignature() !== codeSignature) renderCodeSection();
        refreshPreview();
      } catch (err) { toast(err.message, 'error'); }
    }

    /**
     * Turn the whole page into hand-written HTML, or turn it back.
     *
     * Converting renders the existing blocks into the starting markup
     * server-side, so nobody starts from a blank page; the reload is
     * because the two modes are different screens, not different states
     * of one.
     */
    async function switchMode(mode, html) {
      // `html` comes from the whole-page source view, which has already
      // asked and is saving the author's own edit rather than letting
      // the server re-render the blocks.
      if (mode === 'html' && html === undefined
        // eslint-disable-next-line no-alert
        && !window.confirm('Write this page as HTML?\n\nWhat is on it now becomes '
          + 'your starting markup, and the block list goes away. A lead form block '
          + 'cannot be converted and would be dropped. You can switch back at any time.')) {
        return;
      }
      try {
        const res = await api.post(`/api/pages/${id}/mode`,
          html === undefined ? { mode } : { mode, html });
        if (res.skipped?.length) {
          toast(`Converted. Dropped: ${res.skipped.join(', ')} — re-add it as a block.`, 'error');
        } else {
          toast(mode === 'html' ? 'This page is now written in HTML' : 'Back to blocks');
        }
        ctx.reload();
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
            // An HTML block has no drawer — its row jumps to the code
            // section below instead.
            onclick: admin && (block.type === 'html'
              || (BLOCKS[block.type]?.fields || []).length)
              ? () => (block.type === 'html'
                ? selectCodeBlock(index)
                : blockDrawer(block, index)) : null,
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
        if (kind === 'toggle') {
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
        controls.push({ key, label, kind, coerce, control });
      });

      /** Read every control back into the block object. */
      function collect() {
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
      }

      const submit = h('button.btn.btn-primary', {
        type: 'button', text: 'Apply',
        onclick: async () => {
          collect();
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
          ...controls.map(({ kind, label, control }) => field(
            label,
            (kind === 'textarea' || kind === 'rich')
              ? mdToolbar(control, kind === 'rich')
              : control,
          )),
          submit,
        ],
      });
    }

    // ------------------------------------------------------ page settings
    function settingsDrawer() {
      const theme = page.theme || {};
      const seo = page.seo || {};
      const title = h('input', { type: 'text', value: page.title });
      const slug = h('input', { type: 'text', value: page.slug });
      const description = h('textarea', { rows: 3, maxlength: 300, value: page.description || '' });
      const metaTitle = h('input', {
        type: 'text', maxlength: 80, value: seo.meta_title || '',
        placeholder: 'defaults to the page title',
      });
      const canonical = h('input', {
        type: 'text', maxlength: 500, value: seo.canonical || '',
        placeholder: 'https://example.com/the-real-page',
      });
      const ogTitle = h('input', {
        type: 'text', maxlength: 120, value: seo.og_title || '',
        placeholder: 'defaults to the SEO title',
      });
      const ogDescription = h('textarea', {
        rows: 2, maxlength: 320, value: seo.og_description || '',
        placeholder: 'defaults to the meta description',
      });
      const ogType = select([['', 'Website (default)'], ['article', 'Article'],
        ['product', 'Product'], ['profile', 'Profile'], ['video.other', 'Video']],
      seo.og_type || '');
      const twitterCard = select([['', 'Match the share image'], ['summary', 'Small card'],
        ['summary_large_image', 'Large image card'], ['player', 'Player']],
      seo.twitter_card || '');
      // Populated from the media library once it answers; until then the
      // current value is the only option, so opening and saving the
      // drawer quickly cannot blank an image that is already set.
      const ogImage = select([['', 'None']], '');
      const noindex = h('input', { type: 'checkbox', checked: Boolean(seo.noindex) });
      const nofollow = h('input', { type: 'checkbox', checked: Boolean(seo.nofollow) });
      const primary = h('input', { type: 'color', value: theme.primary || '#0b6e5a' });
      const background = h('input', { type: 'color', value: theme.background || '#ffffff' });
      const text = h('input', { type: 'color', value: theme.text || '#1c2422' });
      const font = select([['system', 'Modern (sans-serif)'], ['serif', 'Classic (serif)'], ['mono', 'Technical (mono)']], theme.font || 'system');
      const width = select([['narrow', 'Narrow'], ['normal', 'Normal'], ['wide', 'Wide']], theme.max_width || 'normal');

      // Images only, newest first. Lazy: a page whose meta nobody edits
      // never pays for this request.
      (async () => {
        try {
          const { media } = await api.get('/api/media?mime=image&per_page=200');
          mount(ogImage, [
            h('option', { value: '', text: 'None' }),
            ...media.map((m) => h('option', {
              value: m.id,
              selected: String(m.id) === String(seo.og_image_id || ''),
              text: m.original_filename + (m.width ? ` (${m.width}×${m.height})` : ''),
            })),
          ]);
        } catch (err) {
          mount(ogImage, h('option', { value: '', text: `Media unavailable — ${err.message}` }));
        }
      })();

      const submit = h('button.btn.btn-primary', {
        type: 'button', text: 'Save settings',
        onclick: async () => {
          submit.disabled = true;
          try {
            ({ page } = await api.patch(`/api/pages/${id}`, {
              title: title.value.trim(),
              slug: slug.value.trim(),
              description: description.value.trim(),
              // Spread first: focus_keyword, schema_org and the image alt
              // are stored in the same block and no field here owns them.
              seo: {
                ...seo,
                meta_title: metaTitle.value.trim(),
                canonical: canonical.value.trim(),
                og_title: ogTitle.value.trim(),
                og_description: ogDescription.value.trim(),
                og_type: ogType.value,
                twitter_card: twitterCard.value,
                og_image_id: ogImage.value ? Number(ogImage.value) : null,
                noindex: noindex.checked,
                nofollow: nofollow.checked,
              },
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
        subtitle: 'URL, the tags search engines and social apps read, and the theme.',
        body: [
          h('div.drawer-section', { text: 'Page' }),
          field('Title', title),
          field('URL slug', slug),

          h('div.drawer-section', { text: 'Search' }),
          field('SEO title', counted(metaTitle, 30, 60, seo.meta_title)),
          field('Meta description', counted(description, 70, 160, page.description)),
          field('Canonical URL (optional)', canonical),
          h('label.check', {}, [noindex, h('span', { text: 'Ask search engines not to index this page' })]),
          h('label.check', {}, [nofollow, h('span', { text: 'Ask them not to follow its links' })]),

          h('div.drawer-section', { text: 'Social share' }),
          field('Share image', ogImage),
          field('Share title', ogTitle),
          field('Share description', ogDescription),
          field('Page type', ogType),
          field('Twitter card', twitterCard),
          h('p.md-hint', {
            text: 'Social tags fall back to the search ones, so filling these in is '
              + 'only worth it when a shared link should read differently. og:url and '
              + 'og:image need APP_BASE_URL set, since a crawler cannot follow a path.',
          }),

          h('div.drawer-section', { text: 'Theme' }),
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
              // The restore replaced the markup too, so the code section
              // cannot keep showing what was in it a moment ago.
              renderCodeSection();
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
          if (type === 'html') { codeIndex = blocks.length - 1; saveBlocks(); }
          else if (def.fields.length) blockDrawer(block, blocks.length - 1);
          else saveBlocks();
        },
      }))) : h('p.muted', { text: 'Admins can edit this page.' });

    // --------------------------------------------------- code section
    /**
     * The HTML editor, in the page under Blocks rather than in a drawer.
     * Markup is written by looking at what it renders, so it belongs
     * beside the preview and stays open while blocks are added, moved
     * or deleted around it.
     */
    const codeBody = h('div.panel-body');
    const codeHead = h('div.panel-head', {}, [h('h2', { text: 'HTML' })]);
    const codeSection = h('section.panel.pb-code-panel', {}, [codeHead, codeBody]);

    // The section under Blocks shows one of two things: the whole
    // page's HTML, or one HTML block's. WHOLE_PAGE is the first, and it
    // is where the section starts — "what is the code of this page" is
    // the question people open it with.
    const WHOLE_PAGE = -1;

    let codeEditor = null;      // the live htmlEditor, or null
    let codeIndex = WHOLE_PAGE; // WHOLE_PAGE, or the block it is editing
    let codeSignature = '';     // which blocks are HTML, to spot real changes
    let codeSaved = '';         // markup as the draft has it, to spot unsaved work
    let codeDirty = null;       // paints the unsaved marker
    let codeLastClean = null;   // last markup pushed into the preview
    let codeDirtyText = {};     // what that marker says, per view

    const classesFor = (styled, width) => ['pb-html',
      styled ? 'pb-html-styled' : null,
      width !== 'normal' ? `pb-html-${width}` : null].filter(Boolean).join(' ');

    /**
     * Put markup into the preview without saving.
     *
     * The preview is this app's own page on this origin, so its document
     * is reachable and the matching block can simply be replaced —
     * rendering it again on the server would cost a round trip to show
     * something the client already has. Only ever the sanitizer's
     * output goes in (see htmlEditor's onClean).
     */
    function livePreview(markup) {
      const doc = iframe.contentDocument;
      if (!doc || !codeEditor) return;
      // The whole-page view replaces the page's content column, which
      // is what the page would be if this markup were saved.
      if (codeIndex === WHOLE_PAGE) {
        // A whole document has a <head> and a <body> of its own; the
        // only honest preview of it is the page itself, after a save.
        // Only a fragment (someone who deleted the wrapper) is injected.
        const main = doc.querySelector('main.page');
        if (!main || markup === undefined || /^\s*(?:<!doctype|<html)/i.test(markup)) return;
        codeLastClean = markup;
        main.innerHTML = markup;
        paintDirty();
        return;
      }
      // The nth .pb-html in the document is the nth HTML block on the
      // page; a block added but not yet saved has none, and waits for
      // the save that renders it.
      const nth = htmlIndexes().indexOf(codeIndex);
      const target = doc.querySelectorAll('.pb-html')[nth];
      if (!target) return;
      target.className = classesFor(codeEditor.styled.value === 'true', codeEditor.width.value);
      // undefined means "only the wrapper changed" — typography and
      // width repaint without re-injecting the markup.
      if (markup !== undefined) {
        codeLastClean = markup;
        target.innerHTML = markup;
      }
      paintDirty();
    }

    function paintDirty() {
      if (!codeDirty || !codeEditor) return;
      const unsaved = codeEditor.textarea.value !== codeSaved;
      codeDirty.textContent = unsaved
        ? (codeDirtyText.unsaved || 'Unsaved — the preview is showing your edit')
        : (codeDirtyText.saved || 'Saved to the draft');
      codeDirty.dataset.state = unsaved ? 'unsaved' : 'saved';
    }

    const htmlIndexes = () => blocks
      .map((block, index) => (block.type === 'html' ? index : -1))
      .filter((index) => index >= 0);
    const htmlSignature = () => htmlIndexes().join(',');

    /** Current editor state back into its block, without saving. */
    function stashCode() {
      if (!codeEditor || !blocks[codeIndex]) return;
      blocks[codeIndex] = {
        type: 'html',
        html: codeEditor.textarea.value,
        styled: codeEditor.styled.value === 'true',
        width: codeEditor.width.value,
      };
    }

    function selectCodeBlock(index) {
      if (index === codeIndex && codeEditor) {
        return codeEditor.textarea.focus();
      }
      stashCode();                       // switching must not lose typing
      const leavingWholePage = codeIndex === WHOLE_PAGE && codeLastClean !== null;
      codeIndex = index;
      codeLastClean = null;
      // The whole-page view writes over the preview's content column,
      // so the draft has to be re-rendered before another view trusts
      // what is in that document.
      if (leavingWholePage) refreshPreview();
      renderCodeSection();
      return codeEditor?.textarea.focus();
    }

    async function addHtmlBlock() {
      stashCode();
      blocks.push(BLOCKS.html.make());
      codeIndex = blocks.length - 1;
      codeLastClean = null;
      // saveBlocks re-renders the section: the HTML block list changed,
      // and codeIndex now points at the new one.
      await saveBlocks();
    }

    /**
     * The section under Blocks: the page's HTML.
     *
     * It opens on the whole page — every block rendered, the real
     * source — because that is what someone means by "the HTML of this
     * page". The picker beside the heading narrows it to a single HTML
     * block when there is one to edit in place.
     */
    async function renderCodeSection() {
      const pageIsHtml = page.mode === 'html';
      const indexes = htmlIndexes();
      codeSignature = indexes.join(',');
      codeEditor?.stop();
      codeEditor = null;

      if (!admin) {
        return mount(codeBody, h('p.muted', { text: 'Admins can edit this page.' }));
      }

      // In HTML mode the single block IS the page, so there is nothing
      // to pick between and no whole-page view to switch to.
      if (pageIsHtml) {
        codeIndex = indexes[0] ?? WHOLE_PAGE;
        if (codeIndex === WHOLE_PAGE) {     // an HTML page with no markup left
          mount(codeHead, [h('h2', { text: 'Page HTML' })]);
          return mount(codeBody, [
            h('p.muted', { text: 'This page has no markup yet.' }),
            h('button.btn.btn-sm', { type: 'button', text: '+ Add markup', onclick: addHtmlBlock }),
          ]);
        }
      } else if (codeIndex !== WHOLE_PAGE && !indexes.includes(codeIndex)) {
        codeIndex = WHOLE_PAGE;
      }

      mount(codeHead, [
        h('h2', { text: pageIsHtml ? 'Page HTML' : 'HTML' }),
        h('div.spacer'),
        pageIsHtml ? null : select(
          [[WHOLE_PAGE, 'Whole page'], ...indexes.map((i) => [i, `HTML block ${i + 1}`])],
          codeIndex, (e) => selectCodeBlock(Number(e.target.value))),
        pageIsHtml ? null : h('button.btn.btn-sm', {
          type: 'button', text: '+ Add block', onclick: addHtmlBlock,
        }),
      ]);

      return codeIndex === WHOLE_PAGE ? renderPageSource() : renderBlockSource();
    }

    /**
     * The whole page as markup: read it, copy it, or edit and save it.
     *
     * Saving is the same conversion "Edit as HTML" performs, with one
     * difference that matters: it keeps the text on screen instead of
     * re-rendering the blocks, so an edit made here is not thrown away
     * by the very action that saves it.
     */
    async function renderPageSource() {
      mount(codeBody, spinner());
      let source = '';
      try {
        ({ html: source } = await api.get(`/api/pages/${id}/html`));
      } catch (err) {
        return mount(codeBody, h('p.muted', { text: err.message }));
      }

      codeSaved = source;
      codeDirtyText = {
        saved: 'The page as a browser receives it',
        unsaved: 'Edited — save to make this the page',
      };
      const editor = htmlEditor(source, {
        compact: true,
        rows: 18,
        wholePage: true,
        onClean: livePreview,
        onInput: () => paintDirty(),
      });
      codeEditor = editor;
      codeDirty = h('span.pb-dirty', { dataset: { state: 'saved' }, text: codeDirtyText.saved });

      const copy = h('button.btn.btn-sm', {
        type: 'button', text: 'Copy HTML',
        onclick: async () => {
          try {
            await navigator.clipboard.writeText(editor.textarea.value);
            toast('Page HTML copied');
          } catch {
            // Clipboard access needs https or a permission; the code is
            // right there either way.
            editor.textarea.select();
            toast('Press ⌘C / Ctrl+C to copy the selected code');
          }
        },
      });
      const save = h('button.btn.btn-primary.btn-sm', {
        type: 'button', text: 'Save as page HTML',
        onclick: async () => {
          // eslint-disable-next-line no-alert
          if (!window.confirm('Save this markup as the page?\n\nThe page becomes an '
            + 'HTML page: this code is what it is, and the block list goes away. '
            + 'You can switch back to blocks at any time.')) return;
          await switchMode('html', editor.textarea.value);
        },
      });

      mount(codeBody, [
        editor.node,
        h('div.row-actions', {}, [copy, save, codeDirty]),
        h('p.md-hint', {
          text: 'The full document — <!doctype html> to </html>: the head with '
            + 'its title, meta and theme CSS, then every block as it renders. '
            + 'Saving stores exactly this and serves it byte-for-byte; the '
            + 'blocks list goes away, and the page is this file from then on.',
        }),
      ]);
      return editor.start();
    }

    /** One HTML block, edited in place — the page stays a block page. */
    function renderBlockSource() {
      const pageIsHtml = page.mode === 'html';
      if (codeIndex === WHOLE_PAGE) return undefined;
      const block = blocks[codeIndex];
      if (!block) return undefined;

      codeSaved = block.html || '';
      codeDirtyText = {};
      // In HTML mode the block is the page, so it may be a whole
      // document; a block on a block page is always a fragment.
      const opts = h('div.pb-code-opts');
      const editor = htmlEditor(codeSaved, {
        compact: true,
        rows: pageIsHtml ? 22 : 12,
        wholePage: pageIsHtml,
        onClean: livePreview,
        onInput: () => paintDirty(),
        // A document brings its own <head> and CSS, so the page's
        // typography and content width have nothing to apply to.
        onDocument: (isDoc) => { opts.hidden = isDoc; },
      });
      // Typography and width are classes on the same wrapper, so they
      // can go straight into the preview too.
      const repaint = () => livePreview(undefined);
      editor.styled = select(BLOCKS.html.styledOptions, block.styled !== false, repaint);
      editor.width = select(BLOCKS.html.widthOptions, block.width || 'normal', repaint);
      codeEditor = editor;

      const save = h('button.btn.btn-primary.btn-sm', {
        type: 'button', text: 'Save to draft',
        onclick: async () => {
          const written = editor.textarea.value;
          stashCode();
          await saveBlocks();
          codeSaved = written;
          paintDirty();
          // A document page runs in a sandbox, so nothing was injected
          // into the preview while typing — the save is what shows it.
          if (pageIsHtml) refreshPreview();
          toast('Saved to the draft');
        },
      });

      // An HTML page that is still a fragment can become the whole
      // document in one step: what the browser receives, loaded into
      // the editor to save as-is.
      const expand = pageIsHtml && !block.doc ? h('button.btn.btn-sm', {
        type: 'button', text: 'Show full document',
        title: 'Load the whole page — doctype, head, theme CSS — into the editor',
        onclick: async () => {
          try {
            const { html } = await api.get(`/api/pages/${id}/html`);
            editor.textarea.value = html;
            // The editor's own input handling: gutter, check, dirty marker.
            editor.textarea.dispatchEvent(new Event('input'));
            editor.textarea.focus();
          } catch (err) { toast(err.message, 'error'); }
        },
      }) : null;

      codeDirty = h('span.pb-dirty', { dataset: { state: 'saved' }, text: 'Saved to the draft' });
      mount(opts, [field('Typography', editor.styled), field('Width', editor.width)]);
      mount(codeBody, [
        editor.node,
        opts,
        h('div.row-actions', {}, [save, expand, codeDirty,
          h('span.md-hint', {
            text: pageIsHtml ? 'This markup is the whole page' : `Block ${codeIndex + 1} of the page`,
          })]),
      ]);
      return editor.start();
    }

    setHead();
    renderBlockList();
    // An HTML page has no block list to show — the markup is the page —
    // so the editor takes that half of the screen instead.
    const htmlPage = page.mode === 'html';
    mount(ctx.el, h(`div.pb-editor${htmlPage ? '.is-html' : ''}`, {}, [
      h('div.pb-side', {}, htmlPage ? [codeSection] : [
        h('section.panel', {}, [
          h('div.panel-head', {}, [h('h2', { text: 'Blocks' })]),
          h('div.panel-body', {}, [blockList, h('div', { style: 'height:12px' }), palette]),
        ]),
        // Under Blocks, in the page: the code section.
        codeSection,
      ]),
      h('div.pb-preview', {}, iframe),
    ]));

    // #/pages?edit=<id>&block=<index> goes straight to that block on
    // arrival — how "create an HTML page" lands somewhere useful.
    const openIndex = /^\d+$/.test(ctx.params.block || '') ? Number(ctx.params.block) : -1;
    if (admin && !htmlPage && blocks[openIndex]) {
      // Drop the param once used, so a refresh does not reopen what the
      // author closed. replaceState leaves the route alone: changing a
      // hash this way fires no hashchange.
      window.history.replaceState(null, '', `#/pages?edit=${id}`);
      if (blocks[openIndex].type === 'html') codeIndex = openIndex;
      else if ((BLOCKS[blocks[openIndex].type]?.fields || []).length) {
        blockDrawer(blocks[openIndex], openIndex);
      }
    }
    renderCodeSection();
  }

  window.views.pages = pages;
}());
