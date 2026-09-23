/* global window, api, kit, ui, navigator */
/**
 * Forms & conversion (2.7) — the form builder, submission log,
 * transactional email templates and the conversion report.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool, number, percent,
    textInput, textarea, checkbox, formatDate, relativeTime, openDrawer, pager } = kit;

  async function forms(ctx) {
    mount(ctx.el, ui.spinner());

    // Email templates and Conversions are no longer tabs here, so
    // neither is fetched; both APIs and their panes stay in the file.
    const formsData = await api.get('/api/forms');

    ctx.setHead('Forms', 'Fields, submissions and where they go.',
      h('button.btn.btn-primary', { type: 'button', text: 'New form',
        onclick: () => editForm(ctx, formsData, null) }));

    mount(ctx.el, formsPane(ctx, formsData));
  }

  // =============================================================== forms
  // Every active form has a page of its own at /f/{site}/{form}: the
  // form rendered by the same code a page's Lead form block uses, so
  // "view the form" means seeing exactly what a visitor gets.
  const formUrl = (ctx, f) => `/f/${ctx.session.user.tenantSlug}/${f.slug}`;

  function formsPane(ctx, data) {
    return h('div.stack', {}, [
      panel(null, table([
        { label: 'Form', cell: (f) => [h('div.cell-name', { text: f.name }),
          h('div.cell-meta', { text: `${(f.fields || []).length} field(s) · ${f.slug}` })] },
        { label: 'Active', cell: (f) => bool(f.is_active, 'Live', 'Off') },
        { label: 'Submissions', class: 'cell-mono',
          cell: (f) => `${number(f.submissions_30d)} / 30d` },
        { label: 'Leads', class: 'cell-mono', cell: (f) => number(f.lead_count) },
        { label: 'Spam', class: 'cell-mono',
          cell: (f) => (f.submit_count
            ? percent((f.spam_count / f.submit_count) * 100, 0)
            : '—') },
        {
          label: 'Autoresponder',
          cell: (f) => bool((f.autoresponder || {}).enabled, 'On', 'Off'),
        },
        {
          label: '',
          cell: (f) => h('div.row-actions', {}, [
            f.is_active
              ? h('a.btn.btn-sm', { href: formUrl(ctx, f), target: '_blank', rel: 'noopener', text: 'View' })
              : null,
            actionButton('Edit', () => editForm(ctx, data, f), { small: true }),
            actionButton('Submissions', () => openSubmissions(ctx, f), { small: true }),
            confirmButton('Delete', async () => {
              const result = await api.del(`/api/forms/${f.id}`);
              toast(result.message || 'Form deleted.');
              ctx.reload();
            }, { small: true }),
          ]),
        },
      ], data.forms, { empty: 'No forms yet.' })),
      panelBody(h('p.muted', {
        text: 'View opens the form on its own page — share that link, or add the form '
          + 'to any built page with a Lead form block. Static frontends POST to '
          + '/api/public/{site}/forms/{form}; add the origin to PUBLIC_FORM_ORIGINS '
          + 'or the browser will block the request.',
      })),
    ]);
  }

  async function editForm(ctx, data, form) {
    const isNew = !form;
    const detail = isNew ? null : (await api.get(`/api/forms/${form.id}`)).form;
    const fields = (detail?.fields || data.forms[0]?.fields || []).map((f) => ({ ...f }));
    const settings = detail?.settings || {};
    const mapping = detail?.lead_mapping || {};
    const rules = (detail?.notification_rules || []).map((r) => ({ ...r }));
    const auto = detail?.autoresponder || {};

    const fieldHost = h('div.field-builder');

    const paintFields = () => {
      mount(fieldHost, fields.length
        ? fields.map((f, i) => h('div.menu-row', {}, [
          h('div.menu-row-main', {}, [
            h('strong', { text: f.label || f.name }),
            h('span.cell-meta', {
              text: [f.name, f.type, f.required ? 'required' : 'optional',
                data.coreFields.includes(f.name) ? 'maps to lead' : null]
                .filter(Boolean).join(' · '),
            }),
          ]),
          h('div.row-actions', {}, [
            h('button.icon-btn', { type: 'button', title: 'Move up', text: '↑',
              onclick: () => { if (i) { [fields[i - 1], fields[i]] = [fields[i], fields[i - 1]]; paintFields(); } } }),
            h('button.icon-btn', { type: 'button', title: 'Move down', text: '↓',
              onclick: () => { if (i < fields.length - 1) { [fields[i + 1], fields[i]] = [fields[i], fields[i + 1]]; paintFields(); } } }),
            h('button.icon-btn', { type: 'button', title: 'Edit', text: '✎',
              onclick: () => editField(i) }),
            h('button.icon-btn', { type: 'button', title: 'Remove', text: '×',
              onclick: () => { fields.splice(i, 1); paintFields(); } }),
          ]),
        ]))
        : h('p.muted', { text: 'No fields yet.' }));
    };

    const editField = (index) => {
      const f = index === null
        ? { name: '', label: '', type: 'text', required: false }
        : fields[index];
      const optionsInput = textInput('options', (f.options || []).join(', '));
      formDrawer({
        title: index === null ? 'Add field' : `Edit “${f.label || f.name}”`,
        fields: [
          { name: 'label', label: 'Label', control: textInput('label', f.label) },
          { name: 'name', label: 'Field name', control: textInput('name', f.name),
            help: 'Lowercase, digits and underscores. Use full_name / email / phone / '
              + 'company / message to fill the lead record directly.' },
          { name: 'type', label: 'Type',
            control: select(data.fieldTypes.map((t) => [t, t]), f.type, null) },
          { name: 'required', label: 'Required',
            control: checkbox('required', f.required, 'Must be filled in') },
          { name: 'max', label: 'Max length',
            control: h('input', { name: 'max', type: 'number', value: f.max || '' }) },
          { name: 'placeholder', label: 'Placeholder',
            control: textInput('placeholder', f.placeholder) },
          { name: 'options', label: 'Options', control: optionsInput,
            help: 'Comma separated. Required for select and radio.' },
        ],
        onSave: async (values) => {
          const next = {
            name: values.name.trim().toLowerCase(),
            label: values.label.trim(),
            type: values.type,
            required: Boolean(values.required),
          };
          if (!next.name || !next.label) throw new Error('Name and label are both required.');
          if (values.max) next.max = Number(values.max);
          if (values.placeholder) next.placeholder = values.placeholder;
          if (values.options.trim()) {
            next.options = values.options.split(',').map((s) => s.trim()).filter(Boolean);
          }
          if (['select', 'radio'].includes(next.type) && !next.options) {
            throw new Error(`“${next.label}” needs at least one option.`);
          }
          if (index === null) fields.push(next); else fields[index] = next;
          paintFields();
        },
        saveLabel: index === null ? 'Add field' : 'Update field',
      });
    };

    paintFields();

    const nameInput = textInput('name', detail?.name);
    const notifyInput = textInput('notify_emails', (detail?.notify_emails || []).join(', '));
    const successInput = textarea('success_message', settings.success_message, { rows: 2 });
    const redirectInput = textInput('redirect_url', settings.redirect_url);
    // Spam protection, storage/consent, the autoresponder, lead mapping
    // and notification rules are no longer edited here. Their stored
    // values are carried through untouched on save, and a new form gets
    // the defaults the old controls used to pre-select — so nothing
    // about how a form behaves changed when the controls went away.
    const carried = {
      honeypot: settings.honeypot !== false,
      captcha: settings.captcha || 'turnstile',
      min_fill_ms: settings.min_fill_ms ?? 2500,
      store_submission: settings.store_submission !== false,
      consent_required: Boolean(settings.consent_required),
    };

    formDrawer({
      title: isNew ? 'New form' : detail.name,
      subtitle: isNew ? null : `View at ${formUrl(ctx, detail)} · POST to ${detail.endpoint}`,
      fields: [
        { name: 'name', label: 'Name', control: nameInput },
        { label: 'Fields', control: h('div', {}, [
          fieldHost,
          h('button.btn.btn-sm', { type: 'button', text: 'Add field',
            onclick: () => editField(null) }),
        ]) },
        { name: 'notify_emails', label: 'Notify these addresses', control: notifyInput,
          help: 'Comma separated. The workspace-wide list in Settings is added to these.' },
        { name: 'success_message', label: 'Success message', control: successInput },
        { name: 'redirect_url', label: 'Redirect after submit', control: redirectInput,
          help: 'Optional. A path or https:// URL.' },
      ],
      onSave: async (values) => {
        if (!fields.length) throw new Error('Add at least one field.');
        const body = {
          name: values.name,
          fields,
          notify_emails: values.notify_emails
            ? values.notify_emails.split(',').map((s) => s.trim()).filter(Boolean)
            : [],
          settings: {
            success_message: values.success_message || null,
            redirect_url: values.redirect_url || null,
            ...carried,
          },
        };
        body.autoresponder = auto.enabled && auto.template_slug
          ? { enabled: true, template_slug: auto.template_slug }
          : { enabled: false };

        if (isNew) {
          const created = await api.post('/api/forms',
            { name: body.name, fields, notify_emails: body.notify_emails });
          // Settings and the autoresponder need the form to exist first.
          await api.patch(`/api/forms/${created.form.id}`, {
            settings: body.settings, autoresponder: body.autoresponder,
          });
        } else {
          await api.patch(`/api/forms/${detail.id}`, body);
        }
        toast('Saved.');
        ctx.reload();
      },
      // Lead mapping and notification rules editors (mappingEditor,
      // rulesEditor below) are kept but no longer shown.
      extra: null,
    });
  }

  function mappingEditor(form, fields, coreFields, ctx) {
    const mapping = { ...(form.lead_mapping || {}) };
    const host = h('div.stack');
    const paint = () => {
      mount(host, [
        ...Object.entries(mapping).map(([fieldName, column]) =>
          h('div.field-inline', {}, [
            h('code', { text: fieldName }),
            h('span.muted', { text: '→' }),
            h('code', { text: column }),
            h('button.icon-btn', { type: 'button', text: '×', title: 'Remove',
              onclick: () => { delete mapping[fieldName]; paint(); } }),
          ])),
        Object.keys(mapping).length ? null : h('p.muted', { text: 'No custom mapping.' }),
      ]);
    };
    paint();

    const fieldSelect = select(
      fields.filter((f) => !coreFields.includes(f.name)).map((f) => [f.name, f.label]),
      '', null,
    );
    const columnSelect = select(coreFields.map((c) => [c, c]), 'email', null);

    return h('div.stack', {}, [
      host,
      h('div.field-inline', {}, [
        fieldSelect, h('span.muted', { text: '→' }), columnSelect,
        actionButton('Add', async () => {
          if (!fieldSelect.value) throw new Error('This form has no unmapped custom fields.');
          mapping[fieldSelect.value] = columnSelect.value;
          paint();
          await api.patch(`/api/forms/${form.id}`, { lead_mapping: mapping });
          toast('Mapping saved.');
        }, { small: true }),
      ]),
    ]);
  }

  function rulesEditor(form, rules, fields, ctx) {
    const host = h('div.stack');
    const save = async () => {
      await api.patch(`/api/forms/${form.id}`, { notification_rules: rules });
      toast('Rules saved.');
    };
    const paint = () => {
      mount(host, [
        ...rules.map((rule, index) => h('div.field-inline', {}, [
          h('span', {
            text: rule.when
              ? `if ${rule.when.field} ${rule.when.op} ${rule.when.value}`
              : 'always',
          }),
          h('span.muted', { text: '→' }),
          h('code', { text: (rule.to || []).join(', ') }),
          h('button.icon-btn', { type: 'button', text: '×', title: 'Remove',
            onclick: async () => { rules.splice(index, 1); paint(); await save(); } }),
        ])),
        rules.length ? null : h('p.muted', {
          text: 'No rules. Add one to route certain submissions to a different inbox.',
        }),
      ]);
    };
    paint();

    return h('div.stack', {}, [
      host,
      actionButton('Add rule', () => {
        const fieldSelect = select(fields.map((f) => [f.name, f.label]), fields[0]?.name, null);
        const opSelect = select([['present', 'is filled in'], ['absent', 'is empty'],
          ['eq', 'equals'], ['ne', 'does not equal'], ['contains', 'contains'],
          ['gte', 'is at least'], ['lte', 'is at most'], ['gt', 'is more than'],
          ['lt', 'is less than']], 'gte', null);
        formDrawer({
          title: 'Notification rule',
          fields: [
            { name: 'to', label: 'Send to', control: textInput('to', ''),
              help: 'Comma separated addresses.' },
            { name: 'label', label: 'Label', control: textInput('label', ''),
              help: 'Shown in the email so you know which rule fired.' },
            { name: 'field', label: 'When field', control: fieldSelect },
            { name: 'op', label: 'Comparison', control: opSelect },
            { name: 'value', label: 'Value', control: textInput('value', '') },
          ],
          onSave: async (values) => {
            const to = values.to.split(',').map((s) => s.trim()).filter(Boolean);
            if (!to.length) throw new Error('Give at least one recipient.');
            rules.push({
              to,
              label: values.label || null,
              when: { field: values.field, op: values.op, value: values.value },
            });
            paint();
            await save();
          },
          saveLabel: 'Add rule',
        });
      }, { small: true }),
    ]);
  }

  async function openSubmissions(ctx, form) {
    const page = 1;
    const data = await api.get(`/api/forms/${form.id}/submissions${api.qs({ page })}`);
    // Field labels from the form definition, so a submission reads
    // "Phone", not "phone" — and a field since renamed still shows the
    // value it was submitted under rather than disappearing.
    const labels = Object.fromEntries((form.fields || []).map((f) => [f.name, f.label || f.name]));
    const nice = (key) => labels[key] || key.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
    const summary = (s) => Object.entries(s.payload || {})
      .map(([k, v]) => `${nice(k)}: ${String(v)}`).join(' · ');

    openDrawer({
      title: `${form.name} — submissions`,
      subtitle: `${data.total} logged`,
      actions: h('a.btn.btn-sm', {
        href: `/api/forms/${form.id}/submissions/export?include_spam=true`,
        text: 'Export CSV',
      }),
      body: table([
        { label: 'When', class: 'cell-mono', cell: (s) => formatDate(s.created_at, true) },
        {
          label: 'Submitted',
          cell: (s) => [
            h('div.cell-name', { text: s.payload?.full_name || s.payload?.name || s.payload?.email || '(no name)' }),
            h('div.cell-meta', { text: summary(s) || 'empty submission' }),
          ],
        },
        { label: 'Lead', cell: (s) => (s.lead_id ? s.full_name || `#${s.lead_id}` : '—') },
        {
          label: 'Status',
          cell: (s) => (s.is_spam ? badge(s.spam_reason || 'spam', 'off') : badge('ok', 'ok')),
        },
        {
          label: '',
          cell: (s) => actionButton('View', () => viewSubmission(form, s, nice), { small: true }),
        },
      ], data.submissions, { empty: 'No submissions yet.' }),
    });
  }

  /**
   * One submission, in full: every field the visitor filled in, then
   * where they came from and what the spam checks made of it. A nested
   * drawer, so closing it returns to the list.
   */
  function viewSubmission(form, s, nice) {
    const payload = s.payload || {};
    const rows = Object.entries(payload);
    const meta = [
      ['Submitted', formatDate(s.created_at, true)],
      ['Form', form.name],
      ['Lead', s.lead_id ? `${s.full_name || 'lead'} #${s.lead_id}${s.lead_status ? ` · ${s.lead_status}` : ''}` : 'Not saved as a lead'],
      ['Status', s.is_spam ? `Marked spam — ${s.spam_reason || 'no reason given'}` : 'Accepted'],
      ['IP address', s.ip || '—'],
      ['Browser', s.user_agent || '—'],
    ];

    openDrawer({
      title: payload.full_name || payload.name || payload.email || 'Submission',
      subtitle: `${form.name} · ${formatDate(s.created_at, true)}`,
      body: [
        h('h3', { text: 'What they filled in' }),
        rows.length
          ? h('dl.sub-fields', {}, rows.map(([key, value]) => h('div', {}, [
            h('dt', { text: nice(key) }),
            h('dd', {}, [
              // An email or phone is there to be acted on, so it is a link.
              /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(value))
                ? h('a', { href: `mailto:${value}`, text: String(value) })
                : (key === 'phone' && String(value).trim()
                  ? h('a', { href: `tel:${String(value).replace(/[^\d+]/g, '')}`, text: String(value) })
                  : h('span', { text: String(value) })),
            ]),
          ])))
          : h('p.muted', { text: 'This submission carried no fields.' }),
        h('hr'),
        h('h3', { text: 'Where it came from' }),
        h('dl.sub-fields.sub-meta', {}, meta.map(([label, value]) => h('div', {}, [
          h('dt', { text: label }), h('dd', { text: String(value) }),
        ]))),
      ],
    });
  }

  // =========================================================== templates
  function templatesPane(ctx, data) {
    return h('div.stack', {}, [
      panel(null, table([
        { label: 'Template', cell: (t) => [h('div.cell-name', { text: t.name }),
          h('div.cell-meta', { text: t.slug })] },
        { label: 'Kind', cell: (t) => badge(t.kind, 'neutral') },
        { label: 'Subject', cell: (t) => t.subject },
        { label: 'Active', cell: (t) => bool(t.is_active, 'On', 'Off') },
        {
          label: '',
          cell: (t) => h('div.row-actions', {}, [
            actionButton('Edit', () => editTemplate(ctx, t), { small: true }),
            actionButton('Preview', async () => {
              const preview = await api.post(`/api/templates/${t.id}/preview`, {});
              openDrawer({
                title: `Preview: ${t.name}`,
                subtitle: preview.subject,
                body: [
                  preview.missing.length
                    ? notice(`Placeholders with no data: ${preview.missing.join(', ')}`, 'warn')
                    : null,
                  h('pre.code-block', { text: preview.bodyText }),
                ],
              });
            }, { small: true }),
            confirmButton('Delete', async () => {
              await api.del(`/api/templates/${t.id}`);
              toast('Template deleted.');
              ctx.reload();
            }, { small: true }),
          ]),
        },
      ], data.templates, { empty: 'No templates yet.' })),
      panelBody(h('p.muted', {
        text: `Placeholders available: ${data.available.slice(0, 14).join(', ')}…`,
      })),
    ]);
  }

  function editTemplate(ctx, template) {
    const subject = textInput('subject', template?.subject);
    const bodyText = textarea('body_text', template?.body_text, { rows: 12, class: 'code' });

    formDrawer({
      title: template ? `Edit “${template.name}”` : 'New template',
      fields: [
        { name: 'name', label: 'Name', control: textInput('name', template?.name) },
        { name: 'kind', label: 'Kind',
          control: select([['transactional', 'Transactional'], ['autoresponder', 'Autoresponder'],
            ['notification', 'Internal notification'], ['campaign', 'Campaign']],
          template?.kind || 'transactional', null) },
        { name: 'subject', label: 'Subject', control: subject },
        { name: 'body_text', label: 'Body (plain text)', control: bodyText,
          help: 'Use {{lead.first_name}}-style placeholders. Plain text clears spam '
            + 'filters that distrust image-heavy HTML.' },
        template
          ? { name: 'is_active', label: 'Active',
            control: checkbox('is_active', template.is_active, 'Available for use') }
          : null,
      ],
      onSave: async (values) => {
        const body = {
          name: values.name, subject: values.subject,
          body_text: values.body_text, kind: values.kind,
        };
        if (template) {
          await api.patch(`/api/templates/${template.id}`,
            { ...body, is_active: Boolean(values.is_active) });
        } else {
          await api.post('/api/templates', body);
        }
        toast('Saved.');
        ctx.reload();
      },
    });
  }

  // ========================================================= conversions
  function conversionsPane(ctx, data) {
    const total = data.byKind.reduce((a, b) => a + b.n, 0);
    const peak = Math.max(1, ...data.byDay.map((d) => d.n));

    return h('div.stack', {}, [
      h('div.kpis', {}, [
        kit.kpi('Conversions', number(total), `last ${data.days} days`),
        ...data.byKind.slice(0, 4).map((k) => kit.kpi(k.kind, number(k.n),
          k.value ? `${number(k.value)} value` : null)),
      ]),
      panel('Daily', h('div.panel-body', {}, [
        h('div.ledger', {}, data.byDay.map((d) => h('div.ledger-bar', {
          title: `${d.day}: ${d.n}`,
          dataset: { empty: d.n === 0 ? '1' : '0' },
          style: `height:${Math.max(2, Math.round((d.n / peak) * 100))}%`,
        }))),
        h('div.ledger-axis', {}, [
          h('span', { text: data.byDay[0]?.day || '' }),
          h('span', { text: `peak ${peak}/day` }),
          h('span', { text: data.byDay[data.byDay.length - 1]?.day || '' }),
        ]),
      ])),
      h('div.split', {}, [
        panel('Top events', table([
          { label: 'Event', cell: (t) => t.name },
          { label: 'Kind', cell: (t) => badge(t.kind, 'neutral') },
          { label: 'Count', class: 'cell-mono', cell: (t) => number(t.n) },
        ], data.top, { empty: 'No conversion events recorded yet.' })),
        panel('By page', table([
          { label: 'Page', cell: (p) => h('code', { text: p.page }) },
          { label: 'Count', class: 'cell-mono', cell: (p) => number(p.n) },
        ], data.byPage, { empty: 'Nothing yet.' })),
      ]),
      panelBody(h('p.muted', {
        text: 'The public site records these by POSTing to '
          + '/api/public/{site}/conversions with a kind of cta, phone, email, whatsapp '
          + 'or download. Form submissions are recorded automatically.',
      })),
    ]);
  }

  window.formsViews = { forms };
}());
