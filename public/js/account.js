/* global window, api, kit, ui, navigator */
/**
 * Account & access (2.4) — the signed-in user's own profile, 2FA and
 * sessions, plus the role/permission matrix and cross-site membership.
 */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, badge, select,
    formDrawer, confirmButton, actionButton, notice, tabs, bool, number,
    textInput, textarea, checkbox, formatDate, relativeTime, openDrawer } = kit;

  async function account(ctx) {
    const tab = ctx.params.tab === 'sessions' ? 'sessions' : 'security';
    mount(ctx.el, ui.spinner());

    // Profile, Roles & permissions and Sites are no longer tabs here.
    // /api/profile is still read: Security needs to know whether
    // two-factor is on, and the head needs the name and role.
    const [{ profile }, sessions] = await Promise.all([
      api.get('/api/profile'),
      api.get('/api/sessions'),
    ]);

    ctx.setHead('Account', `${profile.display_name} · ${profile.role}`);

    const strip = tabs(ctx, [
      ['security', 'Security'],
      ['sessions', 'Sessions', sessions.sessions.length],
    ], tab);

    const panes = {
      security: () => securityPane(ctx, profile),
      sessions: () => sessionsPane(ctx, sessions),
    };
    mount(ctx.el, [strip, (panes[tab] || panes.security)()]);
  }

  // ============================================================= profile
  function profilePane(ctx, profile) {
    const social = profile.social || {};
    const inputs = {
      display_name: textInput('display_name', profile.display_name),
      job_title: textInput('job_title', profile.job_title),
      phone: textInput('phone', profile.phone),
      bio: textarea('bio', profile.bio, { rows: 4 }),
      avatar_media_id: textInput('avatar_media_id', profile.avatar_media_id),
      timezone: textInput('timezone', profile.timezone),
      locale: textInput('locale', profile.locale),
    };
    const socialInputs = ['website', 'linkedin', 'x', 'github', 'instagram']
      .reduce((acc, key) => ({ ...acc, [key]: textInput(key, social[key]) }), {});

    const field = (label, control, help) =>
      h('label.field', {}, [h('span', { text: label }), control,
        help ? h('small.muted', { text: help }) : null]);

    return h('div.split', {}, [
      panel('Your details', panelBody([
        profile.avatar_url
          ? h('img.avatar-preview', { src: profile.avatar_url, alt: '' })
          : null,
        field('Name', inputs.display_name),
        field('Job title', inputs.job_title),
        field('Phone', inputs.phone),
        field('Bio', inputs.bio, 'Shown on author pages if your frontend renders them.'),
        field('Avatar (media id)', inputs.avatar_media_id, 'Copy the id from Media.'),
        field('Timezone', inputs.timezone),
        field('Language', inputs.locale),
        actionButton('Save profile', async () => {
          await api.patch('/api/profile', {
            display_name: inputs.display_name.value,
            job_title: inputs.job_title.value || null,
            phone: inputs.phone.value || null,
            bio: inputs.bio.value || null,
            avatar_media_id: Number(inputs.avatar_media_id.value) || null,
            timezone: inputs.timezone.value || 'UTC',
            locale: inputs.locale.value || 'en',
            social: Object.fromEntries(
              Object.entries(socialInputs)
                .map(([k, v]) => [k, v.value.trim()])
                .filter(([, v]) => v),
            ),
          });
          toast('Profile saved.');
          ctx.reload();
        }, { primary: true }),
      ])),
      h('div.stack', {}, [
        panel('Social links', panelBody([
          ...Object.entries(socialInputs).map(([key, control]) =>
            field(key === 'x' ? 'X / Twitter' : key[0].toUpperCase() + key.slice(1), control)),
          h('p.muted', { text: 'https:// only — anything else is dropped on save.' }),
        ])),
      ]),
    ]);
  }

  // ============================================================ security
  function securityPane(ctx, profile) {
    const current = h('input', { type: 'password', autocomplete: 'current-password' });
    const next = h('input', { type: 'password', autocomplete: 'new-password' });

    return h('div.split', {}, [
      panel('Password', panelBody([
        h('label.field', {}, [h('span', { text: 'Current password' }), current]),
        h('label.field', {}, [
          h('span', { text: 'New password' }), next,
          h('small.muted', { text: 'At least 12 characters.' }),
        ]),
        h('p.muted', { text: 'Changing your password signs out every other session.' }),
        actionButton('Change password', async () => {
          if (!current.value || !next.value) throw new Error('Fill in both fields.');
          const result = await api.post('/api/profile/password', {
            current_password: current.value, new_password: next.value,
          });
          current.value = '';
          next.value = '';
          toast(`Password changed. ${result.sessionsEnded} other session(s) ended.`);
        }, { primary: true }),
      ])),
      // The two-factor panel is not shown here any more. The 2FA API is
      // untouched: anyone who already turned it on is still asked for a
      // code at sign-in, and startTotp/disableTotp below still work.
    ]);
  }

  async function startTotp(ctx) {
    const setup = await api.post('/api/profile/2fa/start', {});
    const codeInput = textInput('code', '', { maxlength: 8, inputmode: 'numeric' });

    openDrawer({
      title: 'Set up two-factor authentication',
      subtitle: 'Add the secret to your authenticator app, then confirm a code.',
      body: [
        h('p', { text: '1. Add this to Google Authenticator, 1Password, Authy or similar:' }),
        h('pre.code-block', { text: setup.secret }),
        actionButton('Copy secret', async () => {
          await navigator.clipboard?.writeText(setup.secret).catch(() => {});
          toast('Copied.');
        }, { small: true }),
        h('p.muted', { text: `Or paste this URI: ${setup.uri}` }),
        h('hr'),
        h('p', { text: '2. Enter the six-digit code it shows:' }),
        h('label.field', {}, [h('span', { text: 'Code' }), codeInput]),
        actionButton('Confirm and turn on', async () => {
          const result = await api.post('/api/profile/2fa/confirm', { code: codeInput.value });
          showRecoveryCodes(ctx, result.recoveryCodes);
        }, { primary: true }),
        notice('Nothing changes about your sign-in until you confirm a code — a '
          + 'half-finished setup cannot lock you out.', 'info'),
      ],
    });
  }

  function showRecoveryCodes(ctx, codes) {
    openDrawer({
      title: 'Save your recovery codes',
      subtitle: 'Each works once, and they are shown only now.',
      body: [
        h('pre.code-block', { text: codes.join('\n') }),
        actionButton('Copy all', async () => {
          await navigator.clipboard?.writeText(codes.join('\n')).catch(() => {});
          toast('Copied.');
        }, { primary: true }),
        notice('Keep these somewhere other than the device with your authenticator — '
          + 'they are how you get back in if you lose it.', 'warn'),
        actionButton('Done', () => {
          kit.closeDrawer();
          ctx.reload();
        }),
      ],
    });
  }

  function regenerateCodes(ctx) {
    formDrawer({
      title: 'New recovery codes',
      subtitle: 'Your existing codes stop working.',
      fields: [
        { name: 'password', label: 'Confirm your password',
          control: h('input', { name: 'password', type: 'password' }) },
      ],
      onSave: async (values) => {
        const result = await api.post('/api/profile/2fa/recovery-codes',
          { password: values.password });
        showRecoveryCodes(ctx, result.recoveryCodes);
      },
      saveLabel: 'Generate',
    });
  }

  function disableTotp(ctx) {
    return new Promise((resolve) => {
      formDrawer({
        title: 'Turn off two-factor authentication',
        fields: [
          { name: 'password', label: 'Confirm your password',
            control: h('input', { name: 'password', type: 'password' }) },
        ],
        onSave: async (values) => {
          await api.post('/api/profile/2fa/disable', { password: values.password });
          toast('Two-factor authentication turned off.');
          resolve();
          ctx.reload();
        },
        saveLabel: 'Turn off',
      });
    });
  }

  // ============================================================ sessions
  function sessionsPane(ctx, data) {
    return panel(null, [
      toolbar([
        h('span.muted', { text: 'Everywhere this account is signed in.' }),
        h('div.spacer'),
        confirmButton('Sign out everywhere else', async () => {
          const result = await api.post('/api/sessions/revoke-others', {});
          toast(`${result.revoked} session(s) ended.`);
          ctx.reload();
        }, { small: true }),
      ]),
      table([
        {
          label: 'Device',
          cell: (s) => [
            h('div.cell-name', { text: describeAgent(s.user_agent) }),
            h('div.cell-meta', { text: s.user_agent || '' }),
          ],
        },
        { label: 'IP', class: 'cell-mono', cell: (s) => s.ip || '—' },
        { label: 'Started', class: 'cell-mono', cell: (s) => relativeTime(s.created_at) },
        { label: 'Expires', class: 'cell-mono', cell: (s) => formatDate(s.expires_at) },
        {
          label: '',
          cell: (s) => (s.is_current
            ? badge('this session', 'ok')
            : confirmButton('Revoke', async () => {
              await api.del(`/api/sessions/${s.id}`);
              toast('Session ended.');
              ctx.reload();
            }, { small: true })),
        },
      ], data.sessions, { empty: 'No active sessions.' }),
    ]);
  }

  function describeAgent(agent) {
    const ua = agent || '';
    const browser = /Firefox\//.test(ua) ? 'Firefox'
      : /Edg\//.test(ua) ? 'Edge'
        : /Chrome\//.test(ua) ? 'Chrome'
          : /Safari\//.test(ua) ? 'Safari' : 'Unknown browser';
    const os = /Windows/.test(ua) ? 'Windows'
      : /Macintosh|Mac OS/.test(ua) ? 'macOS'
        : /Android/.test(ua) ? 'Android'
          : /iPhone|iPad/.test(ua) ? 'iOS'
            : /Linux/.test(ua) ? 'Linux' : '';
    return os ? `${browser} on ${os}` : browser;
  }

  // =============================================================== roles
  function rolesPane(ctx, data) {
    const roleKeys = Object.keys(data.roles);
    const groups = Object.entries(data.groups);

    return h('div.stack', {}, [
      notice('Cells are the effective permission. Tick or untick to override the role '
        + 'default for this workspace only; “Reset” puts a role back to its defaults.', 'info'),
      panel(null, h('div.matrix-scroll', {}, h('table.list.matrix', {}, [
        h('thead', {}, h('tr', {}, [
          h('th', { text: 'Permission' }),
          ...roleKeys.map((role) => h('th', {}, [
            h('div', { text: data.roles[role].label }),
            h('div.cell-meta', { text: `${data.roles[role].effective.length} granted` }),
          ])),
        ])),
        h('tbody', {}, groups.flatMap(([group, permissions]) => [
          h('tr.matrix-group', {}, h('td', { colspan: roleKeys.length + 1, text: group })),
          ...permissions.map((permission) => h('tr', {}, [
            h('td', {}, h('code', { text: permission })),
            ...roleKeys.map((role) => {
              const info = data.roles[role];
              const granted = info.effective.includes(permission);
              const isDefault = info.default.includes(permission);
              const overridden = permission in (info.overrides || {});
              const box = h('input', {
                type: 'checkbox',
                checked: granted,
                disabled: role === 'super_admin',
                title: overridden ? `Overridden (default: ${isDefault ? 'on' : 'off'})` : '',
                onchange: async (e) => {
                  const wanted = e.target.checked;
                  try {
                    await api.put('/api/roles/permissions', {
                      role,
                      permission,
                      // Back to null when the tick matches the default,
                      // so the override table only holds real changes.
                      allowed: wanted === isDefault ? null : wanted,
                    });
                    toast(`${data.roles[role].label}: ${permission} ${wanted ? 'granted' : 'removed'}.`);
                  } catch (err) {
                    e.target.checked = !wanted;
                    toast(err.message, 'error');
                  }
                },
              });
              return h('td.matrix-cell', { dataset: { overridden: overridden ? '1' : '0' } }, box);
            }),
          ])),
        ])),
      ]))),
      panel('Reset a role', panelBody(h('div.row-actions', {},
        roleKeys.filter((r) => r !== 'super_admin').map((role) =>
          confirmButton(`Reset ${data.roles[role].label}`, async () => {
            await api.post(`/api/roles/permissions/reset?role=${role}`, {});
            toast('Reset to defaults.');
            ctx.reload();
          }, { small: true, danger: false }))))),
    ]);
  }

  // =============================================================== sites
  function sitesPane(ctx, data, profile) {
    return h('div.stack', {}, [
      panel(null, [
        toolbar([
          h('span.muted', {
            text: data.canManage
              ? 'Every site on this install — you can switch into any of them.'
              : 'Sites you have access to.',
          }),
          h('div.spacer'),
          data.canManage
            ? h('a.btn.btn-sm', { href: '#/platform', text: 'Manage the portfolio' })
            : null,
        ]),
        table([
          { label: 'Site', cell: (s) => [h('div.cell-name', { text: s.name }),
            h('div.cell-meta', { text: s.slug })] },
          { label: 'Your role', cell: (s) => badge(s.role, s.is_home ? 'ok' : 'neutral') },
          { label: 'Access', cell: (s) => (s.is_home ? 'home workspace'
            : data.canManage && s.role === null ? 'platform admin' : 'granted') },
          {
            label: '',
            cell: (s) => h('div.row-actions', {}, [
              s.is_current
                ? badge('current', 'ok')
                : actionButton('Switch to', async () => {
                  await api.post(`/api/platform/switch?tenant_slug=${s.slug}`, {});
                  toast(`Switched to ${s.name}.`);
                  window.location.reload();
                }, { small: true }),
              data.canManage
                ? actionButton('Members', () => showMembers(ctx, s), { small: true })
                : null,
            ]),
          },
        ], data.sites, { empty: 'No sites.' }),
      ]),
      panelBody(h('p.muted', {
        text: 'Switching re-points your current session at that site — every screen '
          + 'then shows its data, and it is logged in that site’s own audit trail. '
          + 'Only a Super Admin can grant access to another site.',
      })),
    ]);
  }

  async function showMembers(ctx, siteRow) {
    const { members } = await api.get(`/api/platform/sites/${siteRow.slug}/members`);
    openDrawer({
      title: `${siteRow.name} — members`,
      subtitle: 'Accounts granted access to this site in addition to their home workspace.',
      body: table([
        { label: 'User', cell: (m) => [h('div.cell-name', { text: m.display_name }),
          h('div.cell-meta', { text: m.email })] },
        { label: 'Role', cell: (m) => badge(m.role, 'neutral') },
        { label: 'Granted', class: 'cell-mono', cell: (m) => relativeTime(m.created_at) },
        {
          label: '',
          cell: (m) => confirmButton('Revoke', async () => {
            await api.del(`/api/platform/members/${m.id}`);
            toast('Access revoked and their sessions on this site ended.');
            ctx.reload();
          }, { small: true }),
        },
      ], members, { empty: 'No extra members — only the site’s own users can sign in.' }),
    });
  }

  // Granting cross-site access moved to the Platform screen, next to
  // site creation — it is a portfolio action, not an account one.

  window.accountViews = { account };
}());
