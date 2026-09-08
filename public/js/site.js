/* global window, api, kit, ui */
/**
 * Site structure & global settings (2.5) — the menu builder, reusable
 * blocks, and the configuration a static frontend reads from
 * /api/v1/{site}/config.
 *
 * The menu builder reorders in place with move buttons and indent /
 * outdent, then PUTs the whole tree. HTML5 drag-and-drop is the obvious
 * alternative, but it is unusable on touch and inaccessible from the
 * keyboard, so the buttons are the primary interaction.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool,
    textInput, textarea, checkbox, relativeTime } = kit;

  async function site(ctx) {
    const tab = ctx.params.tab || 'identity';
    ctx.setHead('Site', 'Identity, menus, reusable blocks and global configuration.');
    mount(ctx.el, ui.spinner());

    const [settings, menus, blocks] = await Promise.all([
      api.get('/api/site/settings'),
      api.get('/api/site/menus'),
      api.get('/api/site/blocks'),
    ]);

    const strip = tabs(ctx, [
      ['identity', 'Identity'],
      ['menus', 'Menus', menus.menus.length],
      ['blocks', 'Blocks', blocks.blocks.length],
      ['config', 'Configuration'],
    ], tab);

    const panes = {
      identity: () => identityPane(ctx, settings),
      menus: () => menusPane(ctx, menus),
      blocks: () => blocksPane(ctx, blocks),
      config: () => configPane(ctx, settings),
    };

    ctx.setHead('Site', `${settings.tenant.name} · /api/v1/${settings.tenant.slug}/config`,
      tab === 'menus'
        ? h('button.btn.btn-primary', { type: 'button', text: 'New menu',
          onclick: () => newMenu(ctx) })
        : tab === 'blocks'
          ? h('button.btn.btn-primary', { type: 'button', text: 'New block',
            onclick: () => editBlock(ctx, null) })
          : null);

    mount(ctx.el, [strip, (panes[tab] || panes.identity)()]);
  }

  // ============================================================ identity
  function identityPane(ctx, data) {
    const identity = data.settings.site_identity || {};
    const locale = data.settings.locale || {};
    const social = identity.social || {};

    const inputs = {
      site_name: textInput('site_name', identity.site_name),
      tagline: textInput('tagline', identity.tagline),
      site_url: textInput('site_url', identity.site_url, { placeholder: 'https://example.com' }),
      contact_email: textInput('contact_email', identity.contact_email),
      contact_phone: textInput('contact_phone', identity.contact_phone),
      whatsapp: textInput('whatsapp', identity.whatsapp),
      address: textarea('address', identity.address, { rows: 2 }),
      logo_media_id: textInput('logo_media_id', identity.logo_media_id),
      favicon_media_id: textInput('favicon_media_id', identity.favicon_media_id),
    };
    const socialInputs = ['website', 'linkedin', 'x', 'instagram', 'facebook', 'youtube']
      .reduce((acc, key) => ({ ...acc, [key]: textInput(key, social[key]) }), {});

    const tz = textInput('timezone', locale.timezone || 'UTC');
    const lang = textInput('language', locale.language || 'en');

    const field = (label, control, help) =>
      h('label.field', {}, [h('span', { text: label }), control,
        help ? h('small.muted', { text: help }) : null]);

    return h('div.split', {}, [
      panel('Identity', panelBody([
        field('Site name', inputs.site_name),
        field('Tagline', inputs.tagline),
        field('Site URL', inputs.site_url,
          'Where the published website lives. Sitemaps and canonical URLs need it.'),
        field('Logo (media id)', inputs.logo_media_id,
          identity.logo_url ? `Currently: ${identity.logo_url}` : 'Copy the id from Media.'),
        field('Favicon (media id)', inputs.favicon_media_id),
        h('div.row-actions', {}, [
          actionButton('Save identity', async () => {
            await api.put('/api/site/settings/site_identity', {
              value: {
                site_name: inputs.site_name.value || null,
                tagline: inputs.tagline.value || null,
                site_url: inputs.site_url.value || null,
                contact_email: inputs.contact_email.value || null,
                contact_phone: inputs.contact_phone.value || null,
                whatsapp: inputs.whatsapp.value || null,
                address: inputs.address.value || null,
                logo_media_id: Number(inputs.logo_media_id.value) || null,
                favicon_media_id: Number(inputs.favicon_media_id.value) || null,
                social: Object.fromEntries(
                  Object.entries(socialInputs)
                    .map(([k, v]) => [k, v.value.trim()])
                    .filter(([, v]) => v),
                ),
              },
            });
            toast('Identity saved.');
            ctx.reload();
          }, { primary: true }),
        ]),
      ])),
      h('div.stack', {}, [
        panel('Contact', panelBody([
          field('Contact email', inputs.contact_email),
          field('Contact phone', inputs.contact_phone),
          field('WhatsApp', inputs.whatsapp),
          field('Address', inputs.address),
        ])),
        panel('Social links', panelBody([
          ...Object.entries(socialInputs).map(([key, control]) =>
            field(key === 'x' ? 'X / Twitter' : key[0].toUpperCase() + key.slice(1), control)),
          h('p.muted', { text: 'https:// only — other schemes are dropped on save.' }),
        ])),
        panel('Locale', panelBody([
          field('Timezone', tz, 'IANA name, e.g. Asia/Dubai.'),
          field('Language', lang),
          actionButton('Save locale', async () => {
            await api.put('/api/site/settings/locale', {
              value: { ...locale, timezone: tz.value, language: lang.value },
            });
            toast('Locale saved.');
          }),
        ])),
      ]),
    ]);
  }

  // =============================================================== menus
  function menusPane(ctx, data) {
    return h('div.stack', {}, [
      panel(null, table([
        { label: 'Menu', cell: (m) => [h('div.cell-name', { text: m.name }),
          h('div.cell-meta', { text: m.slug })] },
        { label: 'Location', cell: (m) => badge(m.location || 'unassigned',
          m.location ? 'ok' : 'off') },
        { label: 'Items', class: 'cell-mono', cell: (m) => m.item_count },
        { label: 'Updated', class: 'cell-mono', cell: (m) => relativeTime(m.updated_at) },
        {
          label: '',
          cell: (m) => h('div.row-actions', {}, [
            actionButton('Edit items', () => openMenuBuilder(ctx, m.id), { small: true }),
            confirmButton('Delete', async () => {
              await api.del(`/api/site/menus/${m.id}`);
              toast('Menu deleted.');
              ctx.reload();
            }, { small: true }),
          ]),
        },
      ], data.menus, { empty: 'No menus yet.' })),
      panelBody(h('p.muted', {
        text: 'Menus reach the frontend through /api/v1/{site}/config. Items pointing at '
          + 'content store the id, so a slug change never breaks the link.',
      })),
    ]);
  }

  function newMenu(ctx) {
    formDrawer({
      title: 'New menu',
      fields: [
        { name: 'name', label: 'Name', control: textInput('name', '') },
        { name: 'location', label: 'Location',
          control: select([['', 'Unassigned'], ['header', 'Header'], ['footer', 'Footer'],
            ['mobile', 'Mobile'], ['sidebar', 'Sidebar'], ['legal', 'Legal'],
            ['social', 'Social']], '', null) },
      ],
      onSave: async (values) => {
        await api.post('/api/site/menus', {
          name: values.name, location: values.location || null,
        });
        toast('Menu created.');
        ctx.reload();
      },
    });
  }

  /**
   * Menu builder. Holds the tree in a flat array of
   * { label, link_type, url, object_id, target, depth } and re-nests it
   * on save — a flat list with a depth is far easier to move items
   * around in than a nested one.
   */
  async function openMenuBuilder(ctx, menuId) {
    const [{ menu }, targets] = await Promise.all([
      api.get(`/api/site/menus/${menuId}`),
      api.get('/api/site/menus/link-targets/list'),
    ]);

    const flat = [];
    const walk = (nodes, depth) => nodes.forEach((n) => {
      flat.push({
        label: n.label, link_type: n.link_type, url: n.url, object_id: n.object_id,
        target: n.target, is_active: n.is_active, depth,
        resolvedUrl: n.resolvedUrl, isBroken: n.isBroken,
        targetUnpublished: n.targetUnpublished,
      });
      walk(n.children || [], depth + 1);
    });
    walk(menu.items || [], 0);

    const host = h('div.menu-builder');

    const paint = () => {
      mount(host, flat.length
        ? flat.map((node, index) => row(node, index))
        : h('p.muted', { text: 'Empty menu. Add the first item below.' }));
    };

    const move = (index, delta) => {
      const target = index + delta;
      if (target < 0 || target >= flat.length) return;
      [flat[index], flat[target]] = [flat[target], flat[index]];
      // Moving above the first item cannot leave it indented.
      if (flat[0].depth !== 0) flat[0].depth = 0;
      paint();
    };

    const indent = (index, delta) => {
      if (index === 0) return;
      const next = Math.max(0, Math.min(2, flat[index].depth + delta));
      // An item can only be one level deeper than the one above it,
      // or the tree it implies has a hole in it.
      flat[index].depth = Math.min(next, flat[index - 1].depth + 1);
      paint();
    };

    const row = (node, index) => h('div.menu-row', {
      dataset: { depth: String(node.depth) },
      style: `margin-left:${node.depth * 24}px`,
    }, [
      h('div.menu-row-main', {}, [
        h('strong', { text: node.label }),
        h('span.cell-meta', {
          text: node.link_type === 'custom'
            ? node.url
            : `${node.link_type}: ${node.resolvedUrl || `#${node.object_id}`}`,
        }),
        node.isBroken ? badge('broken link', 'off') : null,
        node.targetUnpublished ? badge('target not published', 'warn') : null,
        node.is_active ? null : badge('hidden', 'off'),
      ]),
      h('div.row-actions', {}, [
        iconBtn('↑', 'Move up', () => move(index, -1)),
        iconBtn('↓', 'Move down', () => move(index, 1)),
        iconBtn('→', 'Indent', () => indent(index, 1)),
        iconBtn('←', 'Outdent', () => indent(index, -1)),
        iconBtn('✎', 'Edit', () => editItem(index)),
        iconBtn('×', 'Remove', () => { flat.splice(index, 1); paint(); }),
      ]),
    ]);

    const iconBtn = (glyph, label, onclick) =>
      h('button.icon-btn', { type: 'button', title: label, 'aria-label': label,
        text: glyph, onclick });

    const editItem = (index) => {
      const node = index === null ? { link_type: 'custom', is_active: true, depth: 0 } : flat[index];
      const linkTypeSelect = select([['custom', 'Custom URL'], ['content', 'Content item'],
        ['term', 'Category / tag'], ['page', 'Builder page']], node.link_type, null);
      const objectSelect = select([
        ['', '— choose —'],
        ...targets.content.map((c) => [`content:${c.id}`, `${c.type_name}: ${c.title}`]),
        ...targets.terms.map((t) => [`term:${t.id}`, `${t.taxonomy_name}: ${t.name}`]),
        ...targets.pages.map((p) => [`page:${p.id}`, `Page: ${p.title}`]),
      ], node.object_id ? `${node.link_type}:${node.object_id}` : '', null);

      formDrawer({
        title: index === null ? 'Add menu item' : `Edit “${node.label}”`,
        fields: [
          { name: 'label', label: 'Label', control: textInput('label', node.label) },
          { name: 'link_type', label: 'Links to', control: linkTypeSelect },
          { name: 'object', label: 'Target', control: objectSelect,
            help: 'Used when the link type is not a custom URL.' },
          { name: 'url', label: 'Custom URL', control: textInput('url', node.url,
            { placeholder: '/pricing or https://…' }) },
          { name: 'target', label: 'Open in',
            control: select([['_self', 'Same tab'], ['_blank', 'New tab']],
              node.target || '_self', null) },
          { name: 'is_active', label: 'Visible',
            control: checkbox('is_active', node.is_active !== false, 'Show in the menu') },
        ],
        onSave: async (values) => {
          const [kind, id] = (values.object || '').split(':');
          const next = {
            label: values.label,
            link_type: values.link_type,
            url: values.link_type === 'custom' ? values.url : null,
            object_id: values.link_type === 'custom' ? null : Number(id) || null,
            target: values.target === '_blank' ? '_blank' : null,
            is_active: Boolean(values.is_active),
            depth: node.depth || 0,
          };
          if (!next.label) throw new Error('Give the item a label.');
          if (next.link_type === 'custom' && !next.url) throw new Error('Give it a URL.');
          if (next.link_type !== 'custom' && !next.object_id) {
            throw new Error('Choose what it should point at.');
          }
          if (next.link_type !== 'custom' && kind && kind !== next.link_type) {
            throw new Error(`That target is a ${kind}, not a ${next.link_type}.`);
          }
          if (index === null) flat.push(next); else flat[index] = { ...node, ...next };
          paint();
        },
        saveLabel: index === null ? 'Add' : 'Update',
      });
    };

    paint();

    const drawer = kit.openDrawer({
      title: `Menu: ${menu.name}`,
      subtitle: 'Reorder with the arrows, then save. Nothing changes until you save.',
      body: [
        host,
        h('div.row-actions', {}, [
          h('button.btn', { type: 'button', text: 'Add item', onclick: () => editItem(null) }),
          h('div.spacer'),
          actionButton('Save menu', async () => {
            // Re-nest by depth: each item becomes a child of the nearest
            // preceding item one level shallower.
            const roots = [];
            const stack = [];
            flat.forEach((node) => {
              const entry = {
                label: node.label, link_type: node.link_type, url: node.url,
                object_id: node.object_id, target: node.target,
                is_active: node.is_active, children: [],
              };
              stack.length = node.depth;
              if (node.depth === 0 || !stack[node.depth - 1]) roots.push(entry);
              else stack[node.depth - 1].children.push(entry);
              stack[node.depth] = entry;
            });
            await api.put(`/api/site/menus/${menuId}`, { items: roots });
            toast('Menu saved.');
            ctx.reload();
          }, { primary: true }),
        ]),
      ],
    });
    return drawer;
  }

  // ============================================================== blocks
  function blocksPane(ctx, data) {
    return panel(null, table([
      { label: 'Block', cell: (b) => [h('div.cell-name', { text: b.name }),
        h('div.cell-meta', { text: b.slug })] },
      { label: 'Kind', cell: (b) => badge(b.kind, 'neutral') },
      { label: 'Active', cell: (b) => bool(b.is_active, 'Live', 'Off') },
      { label: 'Updated', class: 'cell-mono', cell: (b) => relativeTime(b.updated_at) },
      {
        label: '',
        cell: (b) => h('div.row-actions', {}, [
          actionButton('Edit', () => editBlock(ctx, b), { small: true }),
          confirmButton('Delete', async () => {
            await api.del(`/api/site/blocks/${b.id}`);
            toast('Block deleted.');
            ctx.reload();
          }, { small: true }),
        ]),
      },
    ], data.blocks, {
      empty: 'No reusable blocks. Use these for CTAs, banners and footer content '
        + 'the frontend pulls from /api/v1/{site}/config.',
    }));
  }

  function editBlock(ctx, block) {
    const content = block?.content || {};
    const htmlInput = textarea('html', content.html, { rows: 10, class: 'code' });
    const headingInput = textInput('heading', content.heading);
    const ctaLabel = textInput('cta_label', content.cta_label);
    const ctaHref = textInput('cta_href', content.cta_href);

    formDrawer({
      title: block ? `Edit “${block.name}”` : 'New block',
      fields: [
        { name: 'name', label: 'Name', control: textInput('name', block?.name) },
        { name: 'kind', label: 'Kind',
          control: select([['html', 'Raw HTML'], ['cta', 'Call to action'],
            ['banner', 'Banner'], ['footer', 'Footer'], ['promo', 'Promotion'],
            ['custom', 'Custom']], block?.kind || 'html', null) },
        { name: 'heading', label: 'Heading', control: headingInput },
        { name: 'html', label: 'HTML', control: htmlInput,
          help: 'Sanitized on save, like page content.' },
        { name: 'cta_label', label: 'CTA label', control: ctaLabel },
        { name: 'cta_href', label: 'CTA link', control: ctaHref },
        block
          ? { name: 'is_active', label: 'Active',
            control: checkbox('is_active', block.is_active, 'Include in the site config') }
          : null,
      ],
      onSave: async (values) => {
        const body = {
          name: values.name,
          kind: values.kind,
          content: {
            heading: values.heading || null,
            html: values.html || null,
            cta_label: values.cta_label || null,
            cta_href: values.cta_href || null,
          },
        };
        if (block) {
          await api.patch(`/api/site/blocks/${block.id}`,
            { ...body, is_active: Boolean(values.is_active) });
        } else {
          await api.post('/api/site/blocks', body);
        }
        toast('Saved.');
        ctx.reload();
      },
    });
  }

  // ============================================================== config
  function configPane(ctx, data) {
    const maintenance = data.settings.maintenance || {};
    const consent = data.settings.cookie_consent || {};
    const seoDefaults = data.settings.seo_defaults || {};
    const breadcrumbs = data.settings.breadcrumbs || {};
    const apiConfig = data.settings.api || {};
    const smtp = data.settings.smtp || {};

    const mEnabled = checkbox('enabled', maintenance.enabled, 'Maintenance mode on');
    const mMessage = textarea('message', maintenance.message, { rows: 2 });
    const mIps = textInput('allow_ips', (maintenance.allow_ips || []).join(', '));

    const cEnabled = checkbox('c_enabled', consent.enabled !== false, 'Show the cookie banner');
    const cVersion = textInput('policy_version', consent.policy_version || '1');
    const cText = textarea('banner_text', consent.banner_text, { rows: 2 });
    const cUrl = textInput('policy_url', consent.policy_url || '/privacy');

    const titleTemplate = textInput('title_template',
      seoDefaults.title_template || '{{title}} — {{site_name}}');
    const bcEnabled = checkbox('bc_enabled', breadcrumbs.enabled !== false, 'Emit breadcrumbs');
    const bcHome = textInput('home_label', breadcrumbs.home_label || 'Home');
    const requireKey = checkbox('require_key', apiConfig.require_key,
      'Require an API key to read published content');
    const smtpFrom = textInput('from_email', smtp.from_email);
    const smtpName = textInput('from_name', smtp.from_name);

    const field = (label, control, help) =>
      h('label.field', {}, [h('span', { text: label }), control,
        help ? h('small.muted', { text: help }) : null]);

    return h('div.split', {}, [
      h('div.stack', {}, [
        panel('Maintenance mode', panelBody([
          field('', mEnabled),
          field('Message', mMessage),
          field('Always allow these IPs', mIps,
            'Comma separated addresses or CIDR ranges. Never sent to the browser.'),
          actionButton('Save maintenance', async () => {
            await api.put('/api/site/settings/maintenance', {
              value: {
                enabled: mEnabled.querySelector('input').checked,
                message: mMessage.value || null,
                allow_ips: mIps.value.split(',').map((s) => s.trim()).filter(Boolean),
              },
            });
            toast('Saved.');
          }, { primary: true }),
        ])),
        panel('SEO defaults', panelBody([
          field('Title template', titleTemplate, 'Placeholders: {{title}}, {{site_name}}.'),
          field('', bcEnabled),
          field('Breadcrumb home label', bcHome),
          actionButton('Save SEO defaults', async () => {
            await api.put('/api/site/settings/seo_defaults', {
              value: { ...seoDefaults, title_template: titleTemplate.value },
            });
            await api.put('/api/site/settings/breadcrumbs', {
              value: {
                ...breadcrumbs,
                enabled: bcEnabled.querySelector('input').checked,
                home_label: bcHome.value,
              },
            });
            toast('Saved.');
          }, { primary: true }),
        ])),
      ]),
      h('div.stack', {}, [
        panel('Cookie consent', panelBody([
          field('', cEnabled),
          field('Policy version', cVersion,
            'Bump this when the policy changes — consent is logged against it.'),
          field('Banner text', cText),
          field('Policy URL', cUrl),
          actionButton('Save consent settings', async () => {
            await api.put('/api/site/settings/cookie_consent', {
              value: {
                ...consent,
                enabled: cEnabled.querySelector('input').checked,
                policy_version: cVersion.value,
                banner_text: cText.value || null,
                policy_url: cUrl.value || null,
              },
            });
            toast('Saved.');
          }, { primary: true }),
        ])),
        panel('Content API', panelBody([
          field('', requireKey,
            'Published content is public by default. Turning this on means every read '
            + 'needs a key from Publishing → API keys.'),
          actionButton('Save API settings', async () => {
            await api.put('/api/site/settings/api', {
              value: { require_key: requireKey.querySelector('input').checked },
            });
            toast('Saved.');
          }, { primary: true }),
        ])),
        panel('Email sender', panelBody([
          field('From name', smtpName),
          field('From address', smtpFrom),
          h('p.muted', {
            text: 'SMTP credentials come from the environment (EMAIL_PROVIDER, SMTP_*), '
              + 'never from the database.',
          }),
          actionButton('Save sender', async () => {
            await api.put('/api/site/settings/smtp', {
              value: { ...smtp, from_name: smtpName.value || null,
                from_email: smtpFrom.value || null },
            });
            toast('Saved.');
          }, { primary: true }),
        ])),
      ]),
    ]);
  }

  window.siteViews = { site };
}());
