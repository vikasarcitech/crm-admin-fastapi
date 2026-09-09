/* global window, api, kit, ui, document */
/**
 * Integrations.
 *
 * Every setup form on this screen is generated from the provider
 * descriptor the API returns — `configFields`, `credentialFields`,
 * `targets`, `defaultMapping`. Adding a provider to
 * app/connectors/registry.py gives it a working screen here with no
 * frontend change, which is the whole point of the connector layer.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool, number,
    textInput, textarea, checkbox, formatDate, relativeTime, openDrawer } = kit;

  const STATUS_KINDS = {
    connected: 'ok', draft: 'neutral', error: 'off', expired: 'warn', disabled: 'off',
  };
  const DELIVERY_KINDS = {
    delivered: 'ok', pending: 'warn', failed: 'off', dead: 'off',
  };
  const KIND_LABELS = {
    crm: 'CRM', email: 'Email', automation: 'Automation',
    analytics: 'Analytics', storage: 'Storage & CDN',
  };

  async function integrations(ctx) {
    mount(ctx.el, ui.spinner());
    const [cat, mine] = await Promise.all([
      api.get('/api/integrations/providers'),
      api.get('/api/integrations'),
    ]);

    const connected = new Map(mine.connectors.map((c) => [c.provider, c]));
    const tab = ctx.params.tab || 'connected';

    ctx.setHead('Integrations',
      `${mine.connectors.length} configured · ${cat.providers.length} providers available`);

    const strip = tabs(ctx, [
      ['connected', 'Connected', mine.connectors.length],
      ['available', 'Available', cat.providers.length],
      ['activity', 'Delivery log'],
    ], tab);

    const panes = {
      connected: () => connectedPane(ctx, cat, mine),
      available: () => availablePane(ctx, cat, connected),
      activity: () => activityPane(ctx, mine),
    };
    mount(ctx.el, [
      cat.credentialsConfigured ? null
        : notice('CREDENTIALS_KEY is not set on this install, so integrations that '
          + 'need a secret cannot be saved. Generate one and restart before '
          + 'connecting anything.', 'warn'),
      strip,
      await (panes[tab] || panes.connected)(),
    ]);
  }

  // =========================================================== connected
  function connectedPane(ctx, cat, mine) {
    if (!mine.connectors.length) {
      return ui.emptyState('Nothing connected yet',
        'Browse Available to push leads into a CRM, trigger an automation, '
        + 'or send email through your own provider.',
        h('a.btn.btn-primary', { href: '#/integrations?tab=available',
          text: 'Browse providers' }));
    }

    const byKind = {};
    mine.connectors.forEach((c) => { (byKind[c.kind] ||= []).push(c); });

    return h('div.stack', {}, Object.entries(byKind).map(([kind, list]) =>
      panel(KIND_LABELS[kind] || kind, table([
        {
          label: 'Integration',
          cell: (c) => [
            h('div.cell-name', { text: c.name }),
            h('div.cell-meta', { text: c.providerLabel }),
          ],
        },
        {
          label: 'Status',
          cell: (c) => h('span', {}, [
            badge(c.status, STATUS_KINDS[c.status] || 'neutral'),
            c.is_active ? null : badge('paused', 'off'),
            c.warning ? badge('needs attention', 'warn') : null,
          ]),
        },
        { label: 'Events', cell: (c) => (c.events || []).join(', ') || '—' },
        {
          label: 'Delivered 30d',
          class: 'cell-mono',
          cell: (c) => number(c.delivered_30d),
        },
        {
          label: 'Queue',
          class: 'cell-mono',
          cell: (c) => (c.failed
            ? h('span', {}, [badge(`${c.failed} failed`, 'off')])
            : c.pending ? `${c.pending} pending` : 'clear'),
        },
        {
          label: 'Last OK',
          class: 'cell-mono',
          cell: (c) => relativeTime(c.last_ok_at),
        },
      ], list, {
        empty: 'None.',
        onRow: (c) => openConnector(ctx, cat, c.id),
      }))));
  }

  // =========================================================== available
  function availablePane(ctx, cat, connected) {
    const byKind = {};
    cat.providers.forEach((p) => { (byKind[p.kind] ||= []).push(p); });

    return h('div.stack', {}, Object.entries(byKind).map(([kind, list]) =>
      panel(KIND_LABELS[kind] || kind, h('div.provider-grid', {}, list.map((p) => {
        const existing = connected.get(p.key);
        return h('div.provider-card', {}, [
          h('div.provider-head', {}, [
            h('strong', { text: p.label }),
            existing
              ? badge(existing.status, STATUS_KINDS[existing.status] || 'neutral')
              : p.managedElsewhere ? badge('built in', 'neutral')
                : p.usesOAuth ? badge('OAuth', 'neutral') : null,
          ]),
          h('p.muted', { text: p.summary }),
          h('div.row-actions', {}, [
            p.managedElsewhere
              ? h('a.btn.btn-sm', { href: p.managedElsewhere, text: 'Configure' })
              : existing
                ? actionButton('Open', () => openConnector(ctx, cat, existing.id),
                  { small: true })
                : actionButton('Connect', () => setupDrawer(ctx, cat, p),
                  { small: true, primary: true }),
            p.docsUrl
              ? h('a.btn.btn-sm', { href: p.docsUrl, target: '_blank',
                rel: 'noopener noreferrer', text: 'Docs' })
              : null,
          ]),
        ]);
      })))));
  }

  /** Build the setup form from the provider descriptor. */
  function setupDrawer(ctx, cat, provider, existing) {
    const inputs = {};

    const control = (f) => {
      let node;
      if (f.type === 'select') {
        node = select(f.options.map((o) => [o.value, o.label]),
          (existing?.config || {})[f.name] ?? f.default, null);
      } else if (f.type === 'password') {
        node = h('input', {
          type: 'password',
          autocomplete: 'new-password',
          // A stored secret is never sent back, so the field starts
          // blank and an empty value means "leave it alone".
          placeholder: existing?.credentialsSet?.includes(f.name)
            ? 'stored — leave blank to keep' : '',
        });
      } else if (f.type === 'textarea') {
        node = textarea(f.name, (existing?.config || {})[f.name], { rows: 3 });
      } else {
        node = textInput(f.name, f.secret ? '' : ((existing?.config || {})[f.name] ?? f.default));
        if (f.secret && existing?.credentialsSet?.includes(f.name)) {
          node.placeholder = 'stored — leave blank to keep';
        }
      }
      inputs[f.name] = { node, field: f };
      return node;
    };

    const eventBoxes = h('div.chips', {}, provider.events.map((event) =>
      h('label.chip', {}, [
        h('input', {
          type: 'checkbox', value: event,
          checked: existing
            ? (existing.events || []).includes(event)
            : provider.events.slice(0, 2).includes(event),
        }),
        h('span', { text: event }),
      ])));

    const fields = [
      { name: 'name', label: 'Name',
        control: textInput('name', existing?.name || provider.label),
        help: 'Shown in the connector list and the delivery log.' },
      ...provider.configFields.map((f) => ({
        label: f.label + (f.required ? ' *' : ''),
        control: control(f), help: f.help,
      })),
      ...provider.credentialFields.map((f) => ({
        label: f.label + (f.required ? ' *' : ''),
        control: control(f),
        help: f.help,
      })),
      provider.events.length
        ? { label: 'Send on', control: eventBoxes }
        : null,
    ];

    formDrawer({
      title: existing ? `Edit ${existing.name}` : `Connect ${provider.label}`,
      subtitle: provider.summary,
      fields,
      saveLabel: existing ? 'Save' : (provider.usesOAuth ? 'Save and connect' : 'Connect'),
      onSave: async (values) => {
        const config = {};
        const credentials = {};
        Object.entries(inputs).forEach(([name, { node, field }]) => {
          const value = (node.value ?? '').trim();
          if (field.secret) {
            // Only send a secret that was actually typed, so saving
            // the form does not blank a stored key.
            if (value) credentials[name] = value;
          } else if (value !== '') {
            config[name] = value;
          }
        });
        const chosen = [...eventBoxes.querySelectorAll('input:checked')]
          .map((i) => i.value);

        if (existing) {
          await api.patch(`/api/integrations/${existing.id}`, {
            name: values.name, config,
            ...(Object.keys(credentials).length ? { credentials } : {}),
            events: chosen,
          });
          toast('Saved.');
          ctx.reload();
          return;
        }

        const created = await api.post('/api/integrations', {
          provider: provider.key, name: values.name,
          config, credentials, events: chosen,
        });
        const connector = created.connector;

        if (provider.usesOAuth) {
          toast('Saved. Authorise it to finish connecting.');
          await startOAuth(ctx, connector.id, provider.label);
        } else {
          toast(`${provider.label} connected.`);
        }
        ctx.navigate(`#/integrations?tab=connected&open=${connector.id}`);
      },
    });
  }

  /**
   * OAuth in a popup.
   *
   * The callback page posts a message back and closes itself; the
   * admin listens rather than polling, so the connector list refreshes
   * the moment the handshake finishes.
   */
  async function startOAuth(ctx, connectorId, label) {
    let start;
    try {
      start = await api.post(`/api/integrations/${connectorId}/oauth/start`, {});
    } catch (err) {
      toast(err.message, 'error');
      return;
    }

    const popup = window.open(start.authorizeUrl, 'crm-oauth',
      'width=620,height=760,menubar=no,toolbar=no');
    if (!popup) {
      // Popup blocked: fall back to a link the user can click.
      openDrawer({
        title: `Authorise ${label}`,
        body: [
          notice('Your browser blocked the popup. Open the link below instead.', 'warn'),
          h('a.btn.btn-primary', {
            href: start.authorizeUrl, target: '_blank', rel: 'noopener noreferrer',
            text: `Authorise ${label}`,
          }),
          h('p.muted', { text: `Redirect URI: ${start.redirectUri}` }),
        ],
      });
      return;
    }

    const onMessage = (event) => {
      if (event.data?.source !== 'crm-oauth') return;
      window.removeEventListener('message', onMessage);
      toast(event.data.ok ? `${label} connected.` : `${label} could not be connected.`,
        event.data.ok ? 'info' : 'error');
      ctx.reload();
    };
    window.addEventListener('message', onMessage);
  }

  // ============================================================== detail
  async function openConnector(ctx, cat, connectorId) {
    const d = await api.get(`/api/integrations/${connectorId}`);
    const c = d.connector;
    const provider = d.provider;

    openDrawer({
      title: c.name,
      subtitle: `${c.providerLabel} · ${c.status}`,
      actions: h('div.row-actions', {}, [
        actionButton('Test', async () => {
          const r = await api.post(`/api/integrations/${c.id}/test`, {});
          toast(r.ok ? 'Connection works.' : r.error, r.ok ? 'info' : 'error');
          if (r.ok && r.detail) {
            openDrawer({
              title: 'Connection test',
              subtitle: c.name,
              body: h('pre.code-block', { text: JSON.stringify(r.detail, null, 2) }),
            });
          }
        }, { small: true }),
      ]),
      body: [
        c.warning ? notice(c.warning, 'warn') : null,
        c.last_error
          ? notice(`Last error: ${c.last_error}`, 'error')
          : null,
        c.status === 'draft' && provider.usesOAuth
          ? notice('Saved but not authorised yet — nothing will be sent until you '
            + 'connect it.', 'warn')
          : null,

        panel('Status', panelBody([
          row('Provider', c.providerLabel),
          row('Authentication', provider.usesOAuth ? 'OAuth 2.0' : 'API key'),
          row('Secrets stored', (c.credentialsSet || []).join(', ') || 'none'),
          provider.usesOAuth ? row('Refresh token', c.hasRefreshToken ? 'yes' : 'no') : null,
          c.tokenExpiresAt ? row('Token expires', formatDate(c.tokenExpiresAt, true)) : null,
          c.connectedAt ? row('Connected', formatDate(c.connectedAt, true)) : null,
          row('Last successful call', c.last_ok_at ? relativeTime(c.last_ok_at) : 'never'),
          row('Sends on', (c.events || []).join(', ') || 'nothing'),
        ])),

        provider.targets.length
          ? panel('Field mapping', panelBody([
            h('p.muted', {
              text: c.usingDefaultMapping
                ? 'Using this provider’s default mapping.'
                : 'Custom mapping.',
            }),
            mappingEditor(ctx, c, provider),
          ]))
          : null,

        panel('Preview', panelBody([
          h('p.muted', { text: 'Exactly what a lead would be turned into, without '
            + 'sending anything.' }),
          h('div.row-actions', {}, [
            actionButton('Preview mapping', async () => {
              const p = await api.post(`/api/integrations/${c.id}/preview`, {});
              openDrawer({
                title: 'Mapping preview',
                subtitle: p.usingDefaults ? 'Provider defaults' : 'Custom mapping',
                body: [
                  h('h3', { text: 'Would send' }),
                  h('pre.code-block', { text: JSON.stringify(p.mapped, null, 2) }),
                  p.emptySources?.length
                    ? notice(`Mapped but empty on this sample: ${p.emptySources.join(', ')}`,
                      'info')
                    : null,
                  h('h3', { text: 'Available source fields' }),
                  h('pre.code-block', { text: JSON.stringify(p.sources, null, 2) }),
                ],
              });
            }, { small: true }),
            actionButton('Send a sample lead', async () => {
              await api.post(`/api/integrations/${c.id}/send-sample`, {});
              toast('Sample queued — it will really be sent. Check the delivery log.');
            }, { small: true }),
          ]),
        ])),

        panel('Recent deliveries', [
          table([
            { label: 'Event', cell: (x) => x.event },
            { label: 'Status',
              cell: (x) => badge(x.status, DELIVERY_KINDS[x.status] || 'neutral') },
            { label: 'Code', class: 'cell-mono', cell: (x) => x.response_code || '—' },
            { label: 'Record', class: 'cell-mono', cell: (x) => x.external_id || '—' },
            { label: 'Took', class: 'cell-mono',
              cell: (x) => (x.duration_ms ? `${x.duration_ms} ms` : '—') },
            { label: 'When', class: 'cell-mono', cell: (x) => relativeTime(x.created_at) },
            {
              label: '',
              cell: (x) => (x.status === 'dead'
                ? actionButton('Retry', async () => {
                  await api.post(`/api/integrations/${c.id}/replay`,
                    { delivery_id: x.id });
                  toast('Re-queued.');
                  ctx.reload();
                }, { small: true })
                : null),
            },
          ], d.deliveries, { empty: 'Nothing sent yet.' }),
          d.deliveries.some((x) => x.last_error)
            ? panelBody(h('p.muted', {
              text: `Most recent error: ${d.deliveries.find((x) => x.last_error).last_error}`,
            }))
            : null,
        ]),

        d.recentLinks.length
          ? panel('Synced records', table([
            { label: 'Object', cell: (l) => `${l.object_type} #${l.object_id}` },
            { label: 'In the provider', cell: (l) => (l.external_url
              ? h('a', { href: l.external_url, target: '_blank',
                rel: 'noopener noreferrer', text: l.external_id })
              : l.external_id) },
            { label: 'Synced', class: 'cell-mono', cell: (l) => relativeTime(l.synced_at) },
          ], d.recentLinks))
          : null,

        panel('Manage', panelBody(h('div.row-actions', {}, [
          actionButton('Edit settings', () => setupDrawer(ctx, cat, provider, c), {}),
          provider.usesOAuth
            ? actionButton(c.status === 'draft' ? 'Connect' : 'Reconnect',
              () => startOAuth(ctx, c.id, c.providerLabel), { primary: c.status === 'draft' })
            : null,
          actionButton(c.is_active ? 'Pause' : 'Resume', async () => {
            await api.patch(`/api/integrations/${c.id}`, { is_active: !c.is_active });
            toast(c.is_active ? 'Paused — nothing will be sent.' : 'Resumed.');
            ctx.reload();
          }, {}),
          confirmButton('Disconnect', async () => {
            await api.del(`/api/integrations/${c.id}`);
            toast('Disconnected. Stored credentials were deleted.');
            ctx.reload();
          }),
        ]))),
      ],
    });
  }

  const row = (label, value) =>
    h('div.field-inline', {}, [
      h('span.muted', { text: label }),
      h('span.cell-mono', { text: String(value ?? '—') }),
    ]);

  /** Source → target pickers, one per provider field. */
  function mappingEditor(ctx, connector, provider) {
    const current = { ...(connector.field_mapping || {}) };
    const usingDefaults = connector.usingDefaultMapping;
    const effective = usingDefaults ? provider.defaultMapping : current;

    const pickers = provider.targets.map((target) => {
      // Which source currently feeds this provider field.
      const source = Object.entries(effective)
        .find(([, t]) => t === target.value)?.[0] || '';
      const picker = select(
        [['', '— not sent —'], ...provider.sources.map((s) => [s.value, s.label])],
        source, null,
      );
      picker.dataset.target = target.value;
      return h('label.field', {}, [
        h('span', { text: target.label }),
        picker,
      ]);
    });

    const host = h('div.stack', {}, pickers);

    return h('div.stack', {}, [
      host,
      h('div.row-actions', {}, [
        actionButton('Save mapping', async () => {
          const mapping = {};
          host.querySelectorAll('select').forEach((el) => {
            if (el.value) mapping[el.value] = el.dataset.target;
          });
          await api.patch(`/api/integrations/${connector.id}`, { field_mapping: mapping });
          toast('Mapping saved.');
          ctx.reload();
        }, { primary: true }),
        actionButton('Back to defaults', async () => {
          await api.patch(`/api/integrations/${connector.id}`, { field_mapping: {} });
          toast('Using the provider defaults again.');
          ctx.reload();
        }, {}),
      ]),
      h('p.muted', {
        text: 'Empty values are never sent, so a blank company will not overwrite a '
          + 'good one in the destination.',
      }),
    ]);
  }

  // ============================================================ activity
  async function activityPane(ctx, mine) {
    if (!mine.connectors.length) {
      return ui.emptyState('No delivery log yet', 'Connect an integration first.');
    }
    const connectorId = Number(ctx.params.connector) || mine.connectors[0].id;
    const data = await api.get(
      `/api/integrations/${connectorId}/deliveries${api.qs({
        status: ctx.params.status, page: ctx.params.page || 1,
      })}`);

    return h('div.stack', {}, [
      panel(null, [
        toolbar([
          select(mine.connectors.map((c) => [c.id, c.name]), connectorId,
            (e) => ctx.navigate(`#/integrations${api.qs({
              ...ctx.params, connector: e.target.value, page: 1,
            })}`)),
          select([['', 'All statuses'], ['delivered', 'Delivered'],
            ['pending', 'Pending'], ['dead', 'Failed']], ctx.params.status || '',
          (e) => ctx.navigate(`#/integrations${api.qs({
            ...ctx.params, status: e.target.value, page: 1,
          })}`)),
          h('div.spacer'),
          actionButton('Retry all failed', async () => {
            const r = await api.post('/api/integrations/deliveries/retry-failed', {});
            toast(`${r.requeued} delivery(ies) re-queued.`);
            ctx.reload();
          }, { small: true }),
        ]),
        table([
          { label: 'Event', cell: (x) => x.event },
          { label: 'Status',
            cell: (x) => badge(x.status, DELIVERY_KINDS[x.status] || 'neutral') },
          { label: 'Attempts', class: 'cell-mono', cell: (x) => x.attempts },
          { label: 'Code', class: 'cell-mono', cell: (x) => x.response_code || '—' },
          { label: 'Record', class: 'cell-mono', cell: (x) => x.external_id || '—' },
          { label: 'Error', cell: (x) => (x.last_error || '').slice(0, 60) || '—' },
          { label: 'When', class: 'cell-mono', cell: (x) => formatDate(x.created_at, true) },
          {
            label: '',
            cell: (x) => actionButton('Inspect', () => openDrawer({
              title: `${x.event} · ${x.status}`,
              subtitle: `Idempotency key: ${x.idempotency_key}`,
              body: [
                x.last_error ? notice(x.last_error, 'error') : null,
                h('h3', { text: 'Sent' }),
                h('pre.code-block', { text: JSON.stringify(x.request || {}, null, 2) }),
                h('h3', { text: 'Response' }),
                h('pre.code-block', { text: JSON.stringify(x.response || {}, null, 2) }),
              ],
            }), { small: true }),
          },
        ], data.deliveries, { empty: 'No deliveries recorded.' }),
        kit.pager(ctx, data.page, data.pages),
      ]),
      panelBody(h('p.muted', {
        text: 'Idempotency keys are derived from the object, so a retry or a replay '
          + 'updates the same record in the destination rather than creating a '
          + 'second one. Delivered rows are kept for 30 days.',
      })),
    ]);
  }

  window.integrationsViews = { integrations };
}());
