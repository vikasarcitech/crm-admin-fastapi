/* global window, api, kit, ui, navigator */
/** Marketing & newsletter (2.8) — subscribers, campaigns, announcements, UTM links. */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, search, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool, number, kpi,
    textInput, textarea, checkbox, formatDate, relativeTime, openDrawer, pager } = kit;

  const SUB_KINDS = { subscribed: 'ok', pending: 'warn', unsubscribed: 'off',
    bounced: 'off', complained: 'off' };
  const CAMPAIGN_KINDS = { draft: 'neutral', scheduled: 'warn', sending: 'warn',
    sent: 'ok', failed: 'off', cancelled: 'off' };

  async function marketing(ctx) {
    const tab = ctx.params.tab || 'subscribers';
    mount(ctx.el, ui.spinner());

    const [subs, campaigns, announcements, links] = await Promise.all([
      api.get(`/api/marketing/subscribers${api.qs({
        q: ctx.params.q, status: ctx.params.status, tag: ctx.params.tag,
        page: ctx.params.page || 1,
      })}`),
      api.get('/api/marketing/campaigns'),
      api.get('/api/marketing/announcements'),
      api.get('/api/marketing/utm-links'),
    ]);

    const actions = {
      subscribers: h('div.row-actions', {}, [
        h('a.btn.btn-sm', { href: '/api/marketing/subscribers/export', text: 'Export CSV' }),
        h('button.btn', { type: 'button', text: 'Import',
          onclick: () => importSubscribers(ctx) }),
        h('button.btn.btn-primary', { type: 'button', text: 'Add subscriber',
          onclick: () => editSubscriber(ctx, null) }),
      ]),
      campaigns: h('button.btn.btn-primary', { type: 'button', text: 'New campaign',
        onclick: () => editCampaign(ctx, null) }),
      announcements: h('button.btn.btn-primary', { type: 'button', text: 'New announcement',
        onclick: () => editAnnouncement(ctx, null) }),
      utm: h('button.btn.btn-primary', { type: 'button', text: 'New UTM link',
        onclick: () => editUtm(ctx) }),
    };

    ctx.setHead('Marketing',
      `${subs.counts.subscribed || 0} subscriber(s) · ${campaigns.campaigns.length} campaign(s)`,
      actions[tab] || null);

    const strip = tabs(ctx, [
      ['subscribers', 'Subscribers', subs.counts.subscribed || 0],
      ['campaigns', 'Campaigns', campaigns.campaigns.length],
      ['announcements', 'Announcements', announcements.announcements.length],
      ['utm', 'UTM links', links.links.length],
    ], tab);

    const panes = {
      subscribers: () => subscribersPane(ctx, subs),
      campaigns: () => campaignsPane(ctx, campaigns),
      announcements: () => announcementsPane(ctx, announcements),
      utm: () => utmPane(ctx, links),
    };
    mount(ctx.el, [strip, (panes[tab] || panes.subscribers)()]);
  }

  // ========================================================= subscribers
  function subscribersPane(ctx, data) {
    return h('div.stack', {}, [
      h('div.kpis', {}, [
        kpi('Subscribed', number(data.counts.subscribed || 0)),
        kpi('Awaiting confirmation', number(data.counts.pending || 0),
          'Double opt-in not completed'),
        kpi('Unsubscribed', number(data.counts.unsubscribed || 0)),
        kpi('Bounced', number((data.counts.bounced || 0) + (data.counts.complained || 0))),
      ]),
      panel(null, [
        toolbar([
          search(ctx.params.q, (q) =>
            ctx.navigate(`#/marketing${api.qs({ ...ctx.params, q, page: 1 })}`),
          'Search email or name…'),
          select([['', 'All statuses'], ['subscribed', 'Subscribed'], ['pending', 'Pending'],
            ['unsubscribed', 'Unsubscribed'], ['bounced', 'Bounced']], ctx.params.status || '',
          (e) => ctx.navigate(`#/marketing${api.qs({ ...ctx.params, status: e.target.value, page: 1 })}`)),
          select([['', 'All tags'], ...data.tags.map((t) => [t.tag, `${t.tag} (${t.n})`])],
            ctx.params.tag || '',
            (e) => ctx.navigate(`#/marketing${api.qs({ ...ctx.params, tag: e.target.value, page: 1 })}`)),
        ]),
        table([
          { label: 'Email', cell: (s) => [h('div.cell-name', { text: s.email }),
            h('div.cell-meta', { text: s.name || s.source || '' })] },
          { label: 'Status', cell: (s) => badge(s.status, SUB_KINDS[s.status] || 'neutral') },
          { label: 'Tags', cell: (s) => (s.tags || []).join(', ') || '—' },
          { label: 'Joined', class: 'cell-mono', cell: (s) => formatDate(s.created_at) },
          {
            label: '',
            cell: (s) => h('div.row-actions', {}, [
              actionButton('Edit', () => editSubscriber(ctx, s), { small: true }),
              confirmButton('Delete', async () => {
                await api.del(`/api/marketing/subscribers/${s.id}`);
                toast('Subscriber deleted.');
                ctx.reload();
              }, { small: true }),
            ]),
          },
        ], data.subscribers, { empty: 'No subscribers yet.' }),
        pager(ctx, Number(ctx.params.page) || 1, data.pages),
      ]),
      panelBody(h('p.muted', {
        text: 'Public sign-ups always go through double opt-in — the confirmation email '
          + 'is sent from the subscriber-confirm template.',
      })),
    ]);
  }

  function editSubscriber(ctx, subscriber) {
    formDrawer({
      title: subscriber ? subscriber.email : 'Add subscriber',
      fields: [
        subscriber ? null
          : { name: 'email', label: 'Email', control: textInput('email', '') },
        { name: 'name', label: 'Name', control: textInput('name', subscriber?.name) },
        { name: 'tags', label: 'Tags',
          control: textInput('tags', (subscriber?.tags || []).join(', ')),
          help: 'Comma separated. Campaign audiences are built from these.' },
        subscriber
          ? { name: 'status', label: 'Status',
            control: select([['subscribed', 'Subscribed'], ['pending', 'Pending'],
              ['unsubscribed', 'Unsubscribed'], ['bounced', 'Bounced'],
              ['complained', 'Complained']], subscriber.status, null) }
          : { name: 'confirmed', label: 'Consent',
            control: checkbox('confirmed', false,
              'I already have consent — skip double opt-in') },
      ],
      onSave: async (values) => {
        const tags = values.tags
          ? values.tags.split(',').map((s) => s.trim()).filter(Boolean) : [];
        if (subscriber) {
          await api.patch(`/api/marketing/subscribers/${subscriber.id}`, {
            name: values.name || null, status: values.status, tags,
          });
        } else {
          await api.post('/api/marketing/subscribers', {
            email: values.email, name: values.name || null, tags,
            confirmed: Boolean(values.confirmed),
          });
        }
        toast('Saved.');
        ctx.reload();
      },
    });
  }

  function importSubscribers(ctx) {
    const rows = textarea('rows', '', { rows: 12, class: 'code',
      placeholder: 'email,name\npriya@example.com,Priya Nair' });
    formDrawer({
      title: 'Import subscribers',
      subtitle: 'Paste CSV rows. A header line is skipped automatically.',
      fields: [
        { name: 'rows', label: 'Rows', control: rows },
        { name: 'tags', label: 'Tag these imports', control: textInput('tags', 'imported') },
        { name: 'confirmed', label: 'Consent',
          control: checkbox('confirmed', false,
            'These people already consented — mark them subscribed'),
          help: 'Leave off and everyone lands as pending until they confirm.' },
      ],
      onSave: async (values) => {
        const result = await api.post('/api/marketing/subscribers/import', {
          rows: values.rows,
          tags: values.tags ? values.tags.split(',').map((s) => s.trim()).filter(Boolean) : [],
          confirmed: Boolean(values.confirmed),
        });
        toast(`${result.added} added, ${result.updated} updated, ${result.skipped} skipped.`);
        if (result.invalidSamples?.length) {
          toast(`Skipped: ${result.invalidSamples.join(', ')}`, 'error');
        }
        ctx.reload();
      },
      saveLabel: 'Import',
    });
  }

  // =========================================================== campaigns
  function campaignsPane(ctx, data) {
    return h('div.stack', {}, [
      panel(null, table([
        { label: 'Campaign', cell: (c) => [h('div.cell-name', { text: c.name }),
          h('div.cell-meta', { text: c.subject })] },
        { label: 'Status', cell: (c) => badge(c.status, CAMPAIGN_KINDS[c.status] || 'neutral') },
        { label: 'Recipients', class: 'cell-mono', cell: (c) => number(c.recipient_count) },
        {
          label: 'When',
          class: 'cell-mono',
          cell: (c) => (c.sent_at ? `sent ${relativeTime(c.sent_at)}`
            : c.scheduled_for ? `for ${formatDate(c.scheduled_for, true)}` : '—'),
        },
        {
          label: '',
          cell: (c) => h('div.row-actions', {}, [
            ['draft', 'scheduled'].includes(c.status)
              ? actionButton('Edit', () => editCampaign(ctx, c), { small: true })
              : actionButton('View', () => viewCampaign(ctx, c), { small: true }),
            c.status === 'scheduled'
              ? actionButton('Cancel', async () => {
                await api.post(`/api/marketing/campaigns/${c.id}/cancel`, {});
                toast('Schedule cancelled.');
                ctx.reload();
              }, { small: true })
              : null,
            c.status === 'sending' ? null : confirmButton('Delete', async () => {
              await api.del(`/api/marketing/campaigns/${c.id}`);
              toast('Campaign deleted.');
              ctx.reload();
            }, { small: true }),
          ]),
        },
      ], data.campaigns, { empty: 'No campaigns yet.' })),
      panelBody(h('p.muted', {
        text: 'Campaigns fan out into the same outbox that sends password resets, so '
          + 'retries and provider settings are shared. An unsubscribe link is appended '
          + 'automatically when the body does not have one.',
      })),
    ]);
  }

  async function editCampaign(ctx, campaign) {
    const detail = campaign ? (await api.get(`/api/marketing/campaigns/${campaign.id}`)).campaign : null;
    const audience = detail?.audience || { status: 'subscribed', tags: [], exclude_tags: [] };
    const subject = textInput('subject', detail?.subject);
    const body = textarea('body_text', detail?.body_text, { rows: 14, class: 'code' });
    const scheduleInput = h('input', { type: 'datetime-local' });

    formDrawer({
      title: campaign ? detail.name : 'New campaign',
      subtitle: detail ? `Audience: ${detail.audienceSize} subscriber(s)` : null,
      fields: [
        { name: 'name', label: 'Internal name', control: textInput('name', detail?.name) },
        { name: 'subject', label: 'Subject', control: subject },
        { name: 'preheader', label: 'Preheader', control: textInput('preheader', detail?.preheader) },
        { name: 'body_text', label: 'Body', control: body,
          help: 'Placeholders: {{subscriber.name}}, {{site.name}}, '
            + '{{subscriber.unsubscribe_url}}.' },
        { name: 'audience_status', label: 'Send to',
          control: select([['subscribed', 'Confirmed subscribers only'],
            ['pending', 'Pending only'], ['all', 'Subscribed and pending']],
          audience.status, null) },
        { name: 'audience_tags', label: 'With these tags',
          control: textInput('audience_tags', (audience.tags || []).join(', ')),
          help: 'Blank means everyone. Comma separated.' },
        { name: 'exclude_tags', label: 'Excluding these tags',
          control: textInput('exclude_tags', (audience.exclude_tags || []).join(', ')) },
      ],
      onSave: async (values) => {
        const split = (s) => (s ? s.split(',').map((x) => x.trim()).filter(Boolean) : []);
        const payload = {
          name: values.name,
          subject: values.subject,
          preheader: values.preheader || null,
          body_text: values.body_text,
          audience: {
            status: values.audience_status,
            tags: split(values.audience_tags),
            exclude_tags: split(values.exclude_tags),
          },
        };
        if (campaign) await api.patch(`/api/marketing/campaigns/${campaign.id}`, payload);
        else await api.post('/api/marketing/campaigns', payload);
        toast('Saved.');
        ctx.reload();
      },
      extra: campaign ? h('div.stack', {}, [
        h('hr'),
        h('h3', { text: 'Send' }),
        h('p.muted', { text: `${detail.audienceSize} subscriber(s) match this audience.` }),
        h('div.row-actions', {}, [
          actionButton('Send a test to me', async () => {
            const result = await api.post(`/api/marketing/campaigns/${campaign.id}/test`, {});
            toast(`Test queued to ${result.to}.`);
          }),
          confirmButton('Send now', async () => {
            const result = await api.post(`/api/marketing/campaigns/${campaign.id}/send`, {});
            toast(`Queued to ${result.queued} recipient(s).`);
            ctx.reload();
          }, { confirmLabel: `Click again to send to ${detail.audienceSize} people` }),
        ]),
        h('div.field-inline', {}, [
          scheduleInput,
          actionButton('Schedule', async () => {
            if (!scheduleInput.value) throw new Error('Pick a date and time.');
            await api.post(`/api/marketing/campaigns/${campaign.id}/send`,
              { scheduled_for: new Date(scheduleInput.value).toISOString() });
            toast('Scheduled.');
            ctx.reload();
          }),
        ]),
      ]) : null,
    });
  }

  async function viewCampaign(ctx, campaign) {
    const { campaign: detail } = await api.get(`/api/marketing/campaigns/${campaign.id}`);
    openDrawer({
      title: detail.name,
      subtitle: `${detail.status} · ${detail.sent_at ? formatDate(detail.sent_at, true) : '—'}`,
      body: [
        h('div.kpis', {}, [
          kpi('Recipients', number((detail.stats || {}).recipients)),
          kpi('Queued', number((detail.stats || {}).queued)),
          ...detail.delivery.map((d) => kpi(d.status, number(d.n))),
        ]),
        panel('Subject', panelBody(h('p', { text: detail.subject }))),
        panel('Body', panelBody(h('pre.code-block', { text: detail.body_text }))),
      ],
    });
  }

  // ======================================================= announcements
  function announcementsPane(ctx, data) {
    return h('div.stack', {}, [
      panel(null, table([
        { label: 'Announcement', cell: (a) => [h('div.cell-name', { text: a.name }),
          h('div.cell-meta', { text: (a.content || {}).heading || '' })] },
        { label: 'Kind', cell: (a) => badge(a.kind, 'neutral') },
        { label: 'Live', cell: (a) => bool(a.is_live, 'Showing', 'Not showing') },
        {
          label: 'Window',
          class: 'cell-mono',
          cell: (a) => [a.starts_at ? formatDate(a.starts_at) : 'now',
            '→', a.ends_at ? formatDate(a.ends_at) : 'forever'].join(' '),
        },
        { label: 'Priority', class: 'cell-mono', cell: (a) => a.priority },
        {
          label: '',
          cell: (a) => h('div.row-actions', {}, [
            actionButton('Edit', () => editAnnouncement(ctx, a), { small: true }),
            confirmButton('Delete', async () => {
              await api.del(`/api/marketing/announcements/${a.id}`);
              toast('Deleted.');
              ctx.reload();
            }, { small: true }),
          ]),
        },
      ], data.announcements, {
        empty: 'No announcements. Use these for promo bars, popups and banners — the '
          + 'frontend reads whatever is live from /api/v1/{site}/config.',
      })),
    ]);
  }

  function editAnnouncement(ctx, announcement) {
    const content = announcement?.content || {};
    const placement = announcement?.placement || {};
    const startsAt = h('input', { type: 'datetime-local',
      value: announcement?.starts_at ? String(announcement.starts_at).slice(0, 16) : '' });
    const endsAt = h('input', { type: 'datetime-local',
      value: announcement?.ends_at ? String(announcement.ends_at).slice(0, 16) : '' });

    formDrawer({
      title: announcement ? `Edit “${announcement.name}”` : 'New announcement',
      fields: [
        { name: 'name', label: 'Internal name', control: textInput('name', announcement?.name) },
        { name: 'kind', label: 'Kind',
          control: select([['bar', 'Top bar'], ['popup', 'Popup'], ['banner', 'Banner'],
            ['slide-in', 'Slide-in']], announcement?.kind || 'bar', null) },
        { name: 'heading', label: 'Heading', control: textInput('heading', content.heading) },
        { name: 'body', label: 'Body', control: textarea('body', content.body, { rows: 3 }),
          help: 'Sanitized on save.' },
        { name: 'cta_label', label: 'CTA label', control: textInput('cta_label', content.cta_label) },
        { name: 'cta_href', label: 'CTA link', control: textInput('cta_href', content.cta_href) },
        { name: 'dismissible', label: 'Dismissible',
          control: checkbox('dismissible', content.dismissible !== false,
            'Visitors can close it') },
        { name: 'paths', label: 'Only on these paths',
          control: textInput('paths', (placement.paths || []).join(', ')),
          help: 'Comma separated. Blank means every page.' },
        { name: 'exclude_paths', label: 'Except these paths',
          control: textInput('exclude_paths', (placement.exclude_paths || []).join(', ')) },
        { name: 'delay_ms', label: 'Delay (ms)',
          control: h('input', { name: 'delay_ms', type: 'number',
            value: placement.delay_ms ?? 0 }) },
        { name: 'frequency', label: 'Show',
          control: select([['session', 'Once per session'], ['once', 'Once ever'],
            ['always', 'Every page view']], placement.frequency || 'session', null) },
        { name: 'priority', label: 'Priority',
          control: h('input', { name: 'priority', type: 'number',
            value: announcement?.priority ?? 0 }),
          help: 'Higher wins when several would show at once.' },
        { label: 'Starts', control: startsAt },
        { label: 'Ends', control: endsAt },
        announcement
          ? { name: 'is_active', label: 'Active',
            control: checkbox('is_active', announcement.is_active, 'Enabled') }
          : null,
      ],
      onSave: async (values) => {
        const split = (s) => (s ? s.split(',').map((x) => x.trim()).filter(Boolean) : []);
        const body = {
          name: values.name,
          kind: values.kind,
          content: {
            heading: values.heading || null,
            body: values.body || null,
            cta_label: values.cta_label || null,
            cta_href: values.cta_href || null,
            dismissible: Boolean(values.dismissible),
          },
          placement: {
            paths: split(values.paths),
            exclude_paths: split(values.exclude_paths),
            delay_ms: Number(values.delay_ms) || 0,
            frequency: values.frequency,
          },
          priority: Number(values.priority) || 0,
          starts_at: startsAt.value ? new Date(startsAt.value).toISOString() : null,
          ends_at: endsAt.value ? new Date(endsAt.value).toISOString() : null,
        };
        if (announcement) {
          await api.patch(`/api/marketing/announcements/${announcement.id}`,
            { ...body, is_active: Boolean(values.is_active) });
        } else {
          await api.post('/api/marketing/announcements', body);
        }
        toast('Saved.');
        ctx.reload();
      },
    });
  }

  // ================================================================ UTM
  function utmPane(ctx, data) {
    return h('div.stack', {}, [
      panel(null, table([
        { label: 'Name', cell: (l) => [h('div.cell-name', { text: l.name }),
          h('div.cell-meta', { text: l.url })] },
        { label: 'Campaign', cell: (l) => `${l.utm_source} / ${l.utm_medium} / ${l.utm_campaign}` },
        { label: 'Clicks', class: 'cell-mono', cell: (l) => number(l.clicks) },
        { label: 'Leads', class: 'cell-mono', cell: (l) => number(l.leads) },
        {
          label: '',
          cell: (l) => h('div.row-actions', {}, [
            actionButton('Copy link', async () => {
              await navigator.clipboard?.writeText(l.url).catch(() => {});
              toast('Full UTM link copied.');
            }, { small: true }),
            actionButton('Copy short', async () => {
              await navigator.clipboard?.writeText(window.location.origin + l.shortUrl)
                .catch(() => {});
              toast('Short link copied — clicks through it are counted.');
            }, { small: true }),
            confirmButton('Delete', async () => {
              await api.del(`/api/marketing/utm-links/${l.id}`);
              toast('Deleted.');
              ctx.reload();
            }, { small: true }),
          ]),
        },
      ], data.links, { empty: 'No campaign links yet.' })),
      panelBody(h('p.muted', {
        text: 'The lead count is real attribution: leads that arrived carrying that '
          + 'utm_campaign, not a click estimate.',
      })),
    ]);
  }

  function editUtm(ctx) {
    formDrawer({
      title: 'New UTM link',
      fields: [
        { name: 'name', label: 'Internal name', control: textInput('name', '') },
        { name: 'base_url', label: 'Destination',
          control: textInput('base_url', '', { placeholder: 'https://example.com/pricing' }) },
        { name: 'utm_source', label: 'Source', control: textInput('utm_source', '') },
        { name: 'utm_medium', label: 'Medium', control: textInput('utm_medium', '') },
        { name: 'utm_campaign', label: 'Campaign', control: textInput('utm_campaign', '') },
        { name: 'utm_term', label: 'Term', control: textInput('utm_term', '') },
        { name: 'utm_content', label: 'Content', control: textInput('utm_content', '') },
      ],
      onSave: async (values) => {
        await api.post('/api/marketing/utm-links', {
          name: values.name, base_url: values.base_url,
          utm_source: values.utm_source, utm_medium: values.utm_medium,
          utm_campaign: values.utm_campaign,
          utm_term: values.utm_term || null, utm_content: values.utm_content || null,
        });
        toast('Link created.');
        ctx.reload();
      },
    });
  }

  window.marketingViews = { marketing };
}());
