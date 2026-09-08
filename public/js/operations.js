/* global window, api, kit, ui */
/**
 * Operations (2.11) and compliance (2.12) — one section, because both
 * answer "is this install healthy and lawful?" and neither is used
 * often enough to earn a top-level entry of its own.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool, number, kpi, bytes,
    textInput, textarea, checkbox, formatDate, relativeTime, openDrawer } = kit;

  const JOB_KINDS = { complete: 'ok', pending: 'warn', running: 'warn', failed: 'off' };
  const LEVEL_KINDS = { critical: 'off', error: 'off', warning: 'warn', info: 'neutral',
    success: 'ok' };

  async function operations(ctx) {
    const tab = ctx.params.tab || 'overview';
    mount(ctx.el, ui.spinner());

    const [overview, errors, notifications, trash, retention, consent, requests] =
      await Promise.all([
        api.get('/api/ops/overview'),
        api.get('/api/ops/errors'),
        api.get('/api/ops/notifications'),
        api.get('/api/ops/trash'),
        api.get('/api/compliance/retention'),
        api.get('/api/compliance/consent?days=90'),
        api.get('/api/compliance/requests'),
      ]);

    ctx.setHead('Operations', 'Queues, logs, backups, notifications and data protection.');

    const strip = tabs(ctx, [
      ['overview', 'Overview'],
      ['notifications', 'Notifications', notifications.unread || null],
      ['errors', 'Errors', errors.errors.length || null],
      ['trash', 'Trash', (trash.content.length + trash.media.length) || null],
      ['backups', 'Backups', overview.backups.length],
      ['compliance', 'Compliance', requests.requests.filter((r) => r.status === 'pending').length || null],
    ], tab);

    const panes = {
      overview: () => overviewPane(ctx, overview),
      notifications: () => notificationsPane(ctx, notifications),
      errors: () => errorsPane(ctx, errors),
      trash: () => trashPane(ctx, trash),
      backups: () => backupsPane(ctx, overview),
      compliance: () => compliancePane(ctx, { retention, consent, requests }),
    };
    mount(ctx.el, [strip, (panes[tab] || panes.overview)()]);
  }

  // ============================================================ overview
  function overviewPane(ctx, data) {
    const q = data.queues;
    const stuck = q.email_dead + q.hooks_dead + q.builds_failed;

    return h('div.stack', {}, [
      data.workersEnabled
        ? null
        : notice('RUN_WORKERS is 0 in this process. Scheduled publishing, campaigns, '
          + 'build hooks, health checks and retention will not run unless another '
          + 'process has it set to 1.', 'warn'),
      stuck
        ? notice(`${stuck} queue item(s) have given up after retrying. Check the tabs above.`,
          'warn')
        : null,
      h('div.kpis', {}, [
        kpi('Email queued', number(q.email_pending), q.email_dead
          ? `${q.email_dead} gave up` : 'all healthy'),
        kpi('Webhooks queued', number(q.hooks_pending), q.hooks_dead
          ? `${q.hooks_dead} gave up` : 'all healthy'),
        kpi('Builds queued', number(q.builds_pending), q.builds_failed
          ? `${q.builds_failed} failed this week` : 'all healthy'),
        kpi('CDN purges queued', number(q.cdn_pending)),
        kpi('Open errors', number(data.errors.open),
          data.errors.last_seen_at ? `last ${relativeTime(data.errors.last_seen_at)}` : null),
        kpi('Trash', number(data.trash.content + data.trash.media),
          `${data.trash.media} file(s)`),
      ]),
      h('div.split', {}, [
        panel('Health checks', [
          table([
            { label: 'Check', cell: (c) => [h('div.cell-name', { text: c.name }),
              h('div.cell-meta', { text: c.url })] },
            {
              label: 'Status',
              cell: (c) => (c.last_checked_at
                ? (c.last_error ? badge('failing', 'off') : badge(`HTTP ${c.last_status}`, 'ok'))
                : badge('not checked yet', 'neutral')),
            },
            { label: 'Latency', class: 'cell-mono',
              cell: (c) => (c.last_latency_ms ? `${c.last_latency_ms} ms` : '—') },
            { label: 'Failures', class: 'cell-mono', cell: (c) => c.consecutive_failures },
            {
              label: '',
              cell: (c) => h('div.row-actions', {}, [
                actionButton('Run now', async () => {
                  const result = await api.post(`/api/ops/health-checks/${c.id}/run`, {});
                  toast(result.result.ok
                    ? `OK in ${result.result.latencyMs} ms.`
                    : `Failing: ${result.result.error}`, result.result.ok ? 'info' : 'error');
                  ctx.reload();
                }, { small: true }),
                confirmButton('Delete', async () => {
                  await api.del(`/api/ops/health-checks/${c.id}`);
                  ctx.reload();
                }, { small: true }),
              ]),
            },
          ], data.health, { empty: 'No health checks. Add one to watch the published site.' }),
          panelBody(actionButton('Add health check', () => editHealthCheck(ctx), { small: true })),
        ]),
        panel('Storage', panelBody([
          h('p', { text: `Media backend: ${data.storage.backend}` }),
          data.storage.bucket ? h('p.muted', { text: `Bucket: ${data.storage.bucket}` }) : null,
          data.storage.publicBaseUrl
            ? h('p.muted', { text: `Served from: ${data.storage.publicBaseUrl}` })
            : null,
          data.storage.localRoot
            ? h('p.muted', { text: `Local path: ${data.storage.localRoot}` })
            : null,
          h('hr'),
          h('p', { text: `Backup target: ${data.backupTarget || 'this server’s disk'}` }),
        ])),
      ]),
    ]);
  }

  function editHealthCheck(ctx) {
    formDrawer({
      title: 'New health check',
      fields: [
        { name: 'name', label: 'Name', control: textInput('name', '') },
        { name: 'url', label: 'URL', control: textInput('url', '',
          { placeholder: 'https://example.com/' }) },
        { name: 'expect_status', label: 'Expect HTTP status',
          control: h('input', { name: 'expect_status', type: 'number', value: 200 }) },
        { name: 'expect_text', label: 'Expect this text', control: textInput('expect_text', ''),
          help: 'Optional. Catches a page that returns 200 but renders an error.' },
        { name: 'interval_seconds', label: 'Check every (seconds)',
          control: h('input', { name: 'interval_seconds', type: 'number', value: 300 }) },
      ],
      onSave: async (values) => {
        await api.post('/api/ops/health-checks', {
          name: values.name, url: values.url,
          expect_status: Number(values.expect_status) || 200,
          expect_text: values.expect_text || null,
          interval_seconds: Number(values.interval_seconds) || 300,
        });
        toast('Check added. The worker alerts after two consecutive failures.');
        ctx.reload();
      },
    });
  }

  // ======================================================= notifications
  function notificationsPane(ctx, data) {
    return panel(null, [
      toolbar([
        h('span.muted', { text: `${data.unread} unread` }),
        h('div.spacer'),
        actionButton('Mark all read', async () => {
          const result = await api.post('/api/ops/notifications/read-all', {});
          toast(`${result.marked} marked read.`);
          ctx.reload();
        }, { small: true }),
      ]),
      table([
        { label: '', width: '90px', cell: (n) => badge(n.level, LEVEL_KINDS[n.level] || 'neutral') },
        {
          label: 'Notification',
          cell: (n) => [
            h('div.cell-name', { text: n.title, style: n.read_at ? 'font-weight:400' : '' }),
            n.body ? h('div.cell-meta', { text: n.body }) : null,
          ],
        },
        { label: 'When', class: 'cell-mono', cell: (n) => relativeTime(n.created_at) },
        {
          label: '',
          cell: (n) => h('div.row-actions', {}, [
            n.link
              ? h('a.btn.btn-sm', { href: n.link, text: 'Open' })
              : null,
            n.read_at ? null : actionButton('Mark read', async () => {
              await api.post(`/api/ops/notifications/${n.id}/read`, {});
              ctx.reload();
            }, { small: true }),
          ]),
        },
      ], data.notifications, { empty: 'Nothing to report.' }),
    ]);
  }

  // ============================================================== errors
  function errorsPane(ctx, data) {
    return h('div.stack', {}, [
      panel(null, [
        toolbar([
          h('span.muted', { text: 'Similar errors are folded into one row with a counter.' }),
          h('div.spacer'),
          confirmButton('Clear resolved', async () => {
            const result = await api.del('/api/ops/errors?resolved_only=true');
            toast(`${result.deleted} cleared.`);
            ctx.reload();
          }, { small: true, danger: false }),
        ]),
        table([
          { label: 'Level', cell: (e) => badge(e.level, LEVEL_KINDS[e.level] || 'neutral') },
          {
            label: 'Error',
            cell: (e) => [
              h('div.cell-name', { text: e.message }),
              h('div.cell-meta', {
                text: [e.source, e.request_path ? `${e.request_method} ${e.request_path}` : null]
                  .filter(Boolean).join(' · '),
              }),
            ],
          },
          { label: 'Count', class: 'cell-mono', cell: (e) => number(e.count) },
          { label: 'First seen', class: 'cell-mono', cell: (e) => relativeTime(e.first_seen_at) },
          { label: 'Last seen', class: 'cell-mono', cell: (e) => relativeTime(e.last_seen_at) },
          {
            label: '',
            cell: (e) => actionButton('Resolve', async () => {
              await api.post(`/api/ops/errors/${e.id}/resolve`, {});
              toast('Resolved. A new occurrence reopens it.');
              ctx.reload();
            }, { small: true }),
          },
        ], data.errors, { empty: 'No errors logged. Good.' }),
      ]),
    ]);
  }

  // =============================================================== trash
  function trashPane(ctx, data) {
    const days = h('input', { type: 'number', value: 0, min: 0, style: 'width:6rem' });
    return h('div.stack', {}, [
      notice('Emptying the trash is permanent and deletes the stored files as well.', 'warn'),
      panel('Content', table([
        { label: 'Title', cell: (i) => i.title },
        { label: 'Type', cell: (i) => badge(i.type_slug, 'neutral') },
        { label: 'Author', cell: (i) => i.author_name },
        { label: 'Trashed', class: 'cell-mono', cell: (i) => relativeTime(i.trashed_at) },
        {
          label: '',
          cell: (i) => actionButton('Restore', async () => {
            await api.post(`/api/content/items/${i.id}/restore`, {});
            toast('Restored as a draft.');
            ctx.reload();
          }, { small: true }),
        },
      ], data.content, { empty: 'No content in the trash.' })),
      panel('Media', table([
        { label: 'File', cell: (m) => m.original_filename },
        { label: 'Size', class: 'cell-mono', cell: (m) => bytes(m.byte_size) },
        { label: 'Still used', cell: (m) => (m.usage_count
          ? badge(`${m.usage_count} reference(s)`, 'warn') : badge('unused', 'ok')) },
        { label: 'Trashed', class: 'cell-mono', cell: (m) => relativeTime(m.deleted_at) },
        {
          label: '',
          cell: (m) => actionButton('Restore', async () => {
            await api.post(`/api/media/${m.id}/restore`, {});
            toast('Restored.');
            ctx.reload();
          }, { small: true }),
        },
      ], data.media, { empty: 'No media in the trash.' })),
      panel('Empty the trash', panelBody([
        h('p.muted', { text: `${bytes(data.reclaimableBytes)} of media storage would be freed.` }),
        h('div.field-inline', {}, [
          h('span', { text: 'Only items trashed more than' }),
          days,
          h('span', { text: 'days ago (0 = everything)' }),
        ]),
        confirmButton('Empty trash permanently', async () => {
          const result = await api.post(
            `/api/ops/trash/empty?confirm=true&older_than_days=${Number(days.value) || 0}`, {});
          toast(`Deleted ${result.contentDeleted} item(s) and ${result.mediaDeleted} file(s).`);
          ctx.reload();
        }),
      ])),
    ]);
  }

  // ============================================================= backups
  function backupsPane(ctx, data) {
    return h('div.stack', {}, [
      data.backupTarget
        ? null
        : notice('BACKUP_S3_BUCKET is not set, so dumps stay on this server’s disk. '
          + 'A backup on the same machine as the database is not a backup.', 'warn'),
      panel(null, [
        toolbar([
          h('span.muted', { text: `Target: ${data.backupTarget || 'local disk'}` }),
          h('div.spacer'),
          actionButton('Back up database now', async () => {
            await api.post('/api/ops/backups', { kind: 'database' });
            toast('Backup started. Refresh to see progress.');
            ctx.reload();
          }, { primary: true }),
          actionButton('Back up media now', async () => {
            await api.post('/api/ops/backups', { kind: 'media' });
            toast('Media backup started.');
            ctx.reload();
          }),
        ]),
        table([
          { label: 'Kind', cell: (b) => badge(b.kind, 'neutral') },
          { label: 'Status', cell: (b) => badge(b.status, JOB_KINDS[b.status] || 'neutral') },
          { label: 'Size', class: 'cell-mono', cell: (b) => (b.byte_size ? bytes(b.byte_size) : '—') },
          { label: 'Destination', cell: (b) => h('code', { text: b.object_key || b.destination || '—' }) },
          { label: 'Trigger', cell: (b) => b.trigger },
          { label: 'Started', class: 'cell-mono', cell: (b) => relativeTime(b.created_at) },
          { label: 'Error', cell: (b) => b.error || '—' },
        ], data.backups, { empty: 'No backups yet.' }),
      ]),
      panelBody(h('p.muted', {
        text: 'On AWS, prefer RDS automated backups and an EventBridge schedule — this '
          + 'in-process loop cannot survive its task being replaced mid-dump.',
      })),
    ]);
  }

  // ========================================================== compliance
  function compliancePane(ctx, data) {
    const lookupInput = textInput('email', '', { placeholder: 'someone@example.com' });

    return h('div.stack', {}, [
      h('div.split', {}, [
        h('div.stack', {}, [
          panel('Data-subject requests', [
            toolbar([
              h('div.spacer'),
              actionButton('New request', () => newRequest(ctx), { small: true, primary: true }),
            ]),
            table([
              { label: 'Kind', cell: (r) => badge(r.kind, 'neutral') },
              { label: 'Subject', cell: (r) => r.subject_email },
              { label: 'Status', cell: (r) => badge(r.status, JOB_KINDS[r.status] || 'neutral') },
              { label: 'Raised', class: 'cell-mono', cell: (r) => relativeTime(r.created_at) },
              {
                label: '',
                cell: (r) => (r.status === 'complete' ? null : h('div.row-actions', {}, [
                  r.kind === 'export'
                    ? h('a.btn.btn-sm', {
                      href: `/api/compliance/requests/${r.id}/export`,
                      text: 'Download export',
                    })
                    : null,
                  r.kind === 'deletion'
                    ? actionButton('Erase…', () => eraseSubject(ctx, r), { small: true })
                    : null,
                ])),
              },
            ], data.requests.requests, { empty: 'No requests logged.' }),
          ]),
          panel('Look up a subject', panelBody([
            h('div.field-inline', {}, [
              lookupInput,
              actionButton('Look up', async () => {
                const email = lookupInput.value.trim();
                if (!email) throw new Error('Enter an email address.');
                const result = await api.get(`/api/compliance/lookup${api.qs({ email })}`);
                openDrawer({
                  title: result.email,
                  subtitle: 'Everything this workspace holds',
                  body: [
                    table([
                      { label: 'Record type', cell: ([key]) => key.replace(/_/g, ' ') },
                      { label: 'Count', class: 'cell-mono', cell: ([, v]) => number(v) },
                    ], Object.entries(result.found)),
                    panel('Current consent', table([
                      { label: 'Purpose', cell: (c) => c.purpose },
                      { label: 'State', cell: (c) => bool(c.granted, 'Granted', 'Withdrawn') },
                      { label: 'When', class: 'cell-mono', cell: (c) => formatDate(c.created_at, true) },
                    ], result.consent, { empty: 'No consent recorded.' })),
                  ],
                });
              }, { small: true }),
            ]),
          ])),
        ]),
        h('div.stack', {}, [
          panel('Consent by purpose', table([
            { label: 'Purpose', cell: (s) => s.purpose },
            { label: 'Granted', class: 'cell-mono', cell: (s) => number(s.granted) },
            { label: 'Withdrawn', class: 'cell-mono', cell: (s) => number(s.withdrawn) },
            { label: 'Last', class: 'cell-mono', cell: (s) => relativeTime(s.last_at) },
          ], data.consent.summary, {
            empty: 'No consent recorded yet. The cookie banner posts to '
              + '/api/public/{site}/consent.',
          })),
          panel('Retention', [
            table([
              { label: 'Scope', cell: (p) => p.scope.replace(/_/g, ' ') },
              { label: 'Keep for', class: 'cell-mono', cell: (p) => `${p.days} days` },
              { label: 'Then', cell: (p) => badge(p.action, p.action === 'delete' ? 'off' : 'warn') },
              { label: 'Active', cell: (p) => bool(p.is_active, 'On', 'Off') },
              { label: 'Last run', class: 'cell-mono',
                cell: (p) => (p.last_run_at
                  ? `${relativeTime(p.last_run_at)} (${number(p.last_affected)})` : 'never') },
              {
                label: '',
                cell: (p) => actionButton('Edit', () => editRetention(ctx, data.retention, p),
                  { small: true }),
              },
            ], data.retention.policies, { empty: 'No policies configured.' }),
            panelBody(h('div.row-actions', {}, [
              actionButton('Add policy', () => editRetention(ctx, data.retention, null),
                { small: true }),
              confirmButton('Run sweep now', async () => {
                const result = await api.post('/api/compliance/retention/run', {});
                const total = Object.values(result.affected || {}).reduce((a, b) => a + b, 0);
                toast(`Retention applied to ${total} row(s).`);
                ctx.reload();
              }, { small: true }),
            ])),
          ]),
        ]),
      ]),
    ]);
  }

  function newRequest(ctx) {
    formDrawer({
      title: 'New data-subject request',
      fields: [
        { name: 'kind', label: 'Kind',
          control: select([['export', 'Export — send them their data'],
            ['deletion', 'Deletion — erase their data'],
            ['rectification', 'Rectification — correct their data']], 'export', null) },
        { name: 'subject_email', label: 'Subject email',
          control: textInput('subject_email', '') },
        { name: 'note', label: 'Note', control: textarea('note', '', { rows: 3 }),
          help: 'How the request arrived and how identity was verified.' },
      ],
      onSave: async (values) => {
        const result = await api.post('/api/compliance/requests', {
          kind: values.kind,
          subject_email: values.subject_email,
          note: values.note || null,
        });
        const found = result.request.found || {};
        toast(`Logged. Found ${Object.values(found).reduce((a, b) => a + b, 0)} record(s).`);
        ctx.reload();
      },
      saveLabel: 'Log request',
    });
  }

  function eraseSubject(ctx, request) {
    const modeSelect = select([
      ['anonymize', 'Anonymize — strip identifiers, keep the pipeline history'],
      ['delete', 'Delete — remove the rows entirely'],
    ], 'anonymize', null);

    openDrawer({
      title: `Erase data for ${request.subject_email}`,
      subtitle: 'This cannot be undone.',
      body: [
        h('label.field', {}, [h('span', { text: 'Method' }), modeSelect]),
        notice('Anonymizing keeps lead rows so revenue reporting and pipeline history '
          + 'stay correct, which is usually both the lawful and the sensible choice. '
          + 'Consent records are always kept — they are the evidence the erasure was '
          + 'requested.', 'info'),
        confirmButton('Erase now', async () => {
          const result = await api.post(
            `/api/compliance/requests/${request.id}/erase`
            + `?confirm=true&mode=${modeSelect.value}`, {});
          const total = Object.values(result.affected || {}).reduce((a, b) => a + b, 0);
          toast(`Done. ${total} row(s) affected.`);
          ctx.reload();
        }, { confirmLabel: 'Click again to erase permanently' }),
      ],
    });
  }

  function editRetention(ctx, data, policy) {
    const scopeSelect = select(
      (policy ? [policy.scope] : data.scopes).map((s) => [s, s.replace(/_/g, ' ')]),
      policy?.scope, null,
    );
    const daysInput = h('input', { type: 'number', value: policy?.days ?? 365, min: 1 });
    const actionSelect = select([['delete', 'Delete the rows'],
      ['anonymize', 'Strip identifiers, keep the rows']], policy?.action || 'delete', null);
    const activeBox = checkbox('is_active', policy?.is_active ?? false, 'Enforce this policy');
    const preview = h('p.muted', { text: 'Preview to see how many rows this would affect.' });

    openDrawer({
      title: policy ? `Retention: ${policy.scope}` : 'New retention policy',
      body: [
        h('label.field', {}, [h('span', { text: 'Applies to' }), scopeSelect]),
        h('label.field', {}, [h('span', { text: 'Keep for (days)' }), daysInput]),
        h('label.field', {}, [
          h('span', { text: 'Then' }), actionSelect,
          h('small.muted', {
            text: `Only ${data.anonymizable.join(' and ')} can be anonymized; `
              + 'everything else is deleted.',
          }),
        ]),
        h('label.field', {}, [h('span', { text: 'Status' }), activeBox]),
        preview,
        h('div.row-actions', {}, [
          actionButton('Preview', async () => {
            const result = await api.post('/api/compliance/retention/preview'
              + `?scope=${scopeSelect.value}&days=${Number(daysInput.value) || 1}`, {});
            preview.textContent = `${number(result.wouldAffect)} row(s) are older than `
              + `${result.days} days right now.`;
          }, { small: true }),
          actionButton('Save policy', async () => {
            await api.put('/api/compliance/retention', {
              scope: scopeSelect.value,
              days: Number(daysInput.value) || 365,
              action: actionSelect.value,
              is_active: activeBox.querySelector('input').checked,
            });
            toast('Policy saved.');
            ctx.reload();
          }, { primary: true }),
        ]),
      ],
    });
  }

  window.operationsViews = { operations };
}());
