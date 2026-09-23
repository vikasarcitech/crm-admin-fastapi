/* global window, document, api, ui, views, contentViews, mediaViews,
   seoViews, siteViews, formsViews, marketingViews, insightsViews,
   publishingViews, operationsViews, accountViews, platformViews,
   integrationsViews */
/** Bootstrap + hash router. */
(function () {
  'use strict';

  const { h, mount, toast, closeDrawer, closeAllDrawers } = ui;

  // Grouped so the sidebar reads as sections rather than one long list.
  // `perm` hides an entry the account cannot use — the API enforces the
  // same permission, so hiding it is a convenience, not the control.
  const ROUTES = [
    { path: 'dashboard', label: 'Dashboard', view: 'dashboard', group: 'Overview',
      icon: 'M3 12h4l2 5 3-11 2 6h5' },
    { path: 'insights', label: 'Analytics', view: 'insights', group: 'Overview',
      perm: 'analytics.view',
      icon: 'M4 20V10m5 10V4m5 16v-7m5 7V8' },

    // The content-items and taxonomy screens are deliberately off the
    // menu: the page builder is the way this platform makes pages now.
    // Their API and modules stay, so existing data and the menu editor's
    // "Content item" links keep working.
    { path: 'media', label: 'Media', view: 'media', group: 'Content',
      perm: 'media.view',
      icon: 'M4 5h16v14H4zM8 11a2 2 0 100-4 2 2 0 000 4M4 16l5-4 4 3 3-2 4 3' },
    { path: 'pages', label: 'Page builder', view: 'pages', group: 'Content',
      icon: 'M4 5h16v4H4zM4 12h7v7H4zM14 12h6v7h-6z' },

    // Leads and Marketing are off the menu, like the content screens:
    // the API, the lead intake and the modules stay, so forms keep
    // collecting and nothing stored is touched.
    { path: 'forms', label: 'Forms', view: 'forms', group: 'Pipeline',
      perm: 'forms.manage',
      icon: 'M5 3h14v18H5zM9 8h6M9 12h6M9 16h3' },

    { path: 'seo', label: 'SEO', view: 'seo', group: 'Site',
      perm: 'seo.manage',
      icon: 'M11 4a7 7 0 100 14 7 7 0 000-14M20 20l-4-4' },
    { path: 'site', label: 'Site settings', view: 'site', group: 'Site',
      icon: 'M12 9a3 3 0 100 6 3 3 0 000-6M4 12h2m12 0h2M12 4v2m0 12v2' },
    { path: 'publishing', label: 'Publishing', view: 'publishing', group: 'Site',
      perm: 'deploy.trigger',
      icon: 'M12 19V5m0 0l-5 5m5-5l5 5M5 21h14' },

    { path: 'users', label: 'Users', view: 'users', group: 'Admin',
      perm: 'users.view',
      icon: 'M4 19a5 5 0 0110 0M9 4a3 3 0 100 6 3 3 0 000-6' },
    { path: 'account', label: 'Account & roles', view: 'account', group: 'Admin',
      icon: 'M12 12a4 4 0 100-8 4 4 0 000 8M5 21a7 7 0 0114 0' },
    // Operations, Integrations, Raw webhooks, Activity and Lead settings
    // are off the menu with the lead-desk screens. Their APIs and
    // modules stay: connectors keep syncing, webhooks keep firing and
    // the activity log keeps being written — they are just not screens
    // in this admin any more.

    { path: 'platform', label: 'All sites', view: 'platform', group: 'Platform',
      perm: 'sites.manage',
      icon: 'M4 5h6v6H4zM14 5h6v6h-6zM4 13h6v6H4zM14 13h6v6h-6z' },
  ];

  const session = { user: null, counts: {}, permissions: null, unread: 0 };
  let current = { path: 'dashboard', params: {} };

  /** Every view module, merged into one lookup for the router. */
  function allViews() {
    return {
      ...window.views,
      ...(window.contentViews || {}),
      ...(window.mediaViews || {}),
      ...(window.seoViews || {}),
      ...(window.siteViews || {}),
      ...(window.formsViews || {}),
      ...(window.marketingViews || {}),
      ...(window.insightsViews || {}),
      ...(window.publishingViews || {}),
      ...(window.operationsViews || {}),
      ...(window.accountViews || {}),
      ...(window.platformViews || {}),
      ...(window.integrationsViews || {}),
    };
  }

  /** Routes this account can actually open. */
  function visibleRoutes() {
    const granted = session.permissions;
    // Before /api/profile answers, show everything rather than flashing
    // an empty sidebar; the API is the real gate either way.
    if (!granted) return ROUTES;
    return ROUTES.filter((r) => !r.perm || granted.includes(r.perm));
  }

  // -------------------------------------------------------------- route
  function parseHash() {
    const raw = window.location.hash.replace(/^#\/?/, '') || 'dashboard';
    const [path, queryString] = raw.split('?');
    return {
      path: ROUTES.some((r) => r.path === path) ? path : 'dashboard',
      params: Object.fromEntries(new URLSearchParams(queryString || '')),
    };
  }

  const navigate = (hash) => { window.location.hash = hash; };

  function setHead(title, subtitle, actions) {
    document.getElementById('page-title').textContent = title;
    document.getElementById('page-sub').textContent = subtitle || '';
    document.title = `${title} — CRM Admin`;
    mount(document.getElementById('page-actions'), actions ? [actions] : []);
  }

  function renderNav() {
    const nav = document.getElementById('nav');
    const routes = visibleRoutes();
    const nodes = [];
    let group = null;

    routes.forEach((r) => {
      if (r.group !== group) {
        group = r.group;
        nodes.push(h('div.nav-group', { text: group }));
      }
      const count = r.countKey ? session.counts[r.countKey] : null;
      nodes.push(h('a', {
        href: `#/${r.path}`,
        class: current.path === r.path ? 'is-current' : '',
        'aria-current': current.path === r.path ? 'page' : null,
      }, [
        // Inline icon markup is authored here, never from server data.
        h('span', {
          html: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
                  stroke-linecap="round" stroke-linejoin="round"><path d="${r.icon}"/></svg>`,
          style: 'display:flex',
        }),
        r.label,
        count ? h('span.count', { text: String(count) }) : null,
      ]));
    });
    mount(nav, nodes);
  }

  async function render() {
    current = parseHash();
    renderNav();
    closeAllDrawers();

    const el = document.getElementById('view');
    const route = ROUTES.find((r) => r.path === current.path);
    const ctx = {
      el,
      // Views build their own hash links from this, so a tab or a pager
      // does not have to know which route it is inside.
      path: current.path,
      params: current.params,
      session,
      setHead,
      navigate,
      /** Re-run the current view (after a save). */
      reload: (opts = {}) => {
        if (!opts.keepDrawer) closeAllDrawers();
        render();
      },
    };

    try {
      const view = allViews()[route.view];
      if (!view) throw new Error(`No view registered for “${route.view}”.`);
      await view(ctx);
    } catch (err) {
      if (err.status === 403) {
        mount(el, ui.emptyState('You do not have access to this',
          err.message || 'Ask an owner or admin for the permission.'));
        return;
      }
      if (err.message === 'Session expired') return;
      mount(el, ui.emptyState('This screen could not load', err.message,
        h('button.btn', { type: 'button', text: 'Try again', onclick: () => render() })));
      toast(err.message, 'error');
    }
  }

  // --------------------------------------------------------------- boot
  async function boot() {
    let me;
    try {
      me = await api.get('/api/auth/me');
    } catch (err) {
      window.location.href = '/login';
      return;
    }

    api.setCsrf(me.csrfToken);
    session.user = me.user;

    document.getElementById('tenant-name').textContent = me.user.tenantName;
    renderSiteSwitcher(me.user);
    document.getElementById('user-name').textContent = me.user.name;
    document.getElementById('user-role').textContent = me.user.role;
    document.getElementById('sign-out').addEventListener('click', async () => {
      try { await api.post('/api/auth/logout'); } finally { window.location.href = '/login'; }
    });

    // Lead counts drive the sidebar badge; a failure here is not fatal.
    api.get('/api/leads/counts')
      .then((counts) => { session.counts = counts; renderNav(); })
      .catch(() => {});

    // Permissions decide which sidebar entries are worth showing. The
    // API enforces them regardless, so a slow or failed load only means
    // the sidebar shows more than it needs to.
    api.get('/api/profile')
      .then(({ profile }) => {
        session.permissions = profile.permissions || [];
        session.profile = profile;
        renderNav();
      })
      .catch(() => {});

    startNotificationBadge();

    window.addEventListener('hashchange', render);
    render();
  }

  /**
   * Site switcher.
   *
   * Only rendered when the account can reach more than one site. In a
   * multi-site console, "which client am I editing" is the question
   * that causes real damage when the answer is wrong, so it sits in
   * the brand block rather than behind a screen.
   */
  async function renderSiteSwitcher(user) {
    let sites;
    try {
      ({ sites } = await api.get('/api/platform/my-sites'));
    } catch (err) {
      return;   // single-site account, or no access — nothing to switch
    }
    if (!sites || sites.length < 2) return;

    const host = document.getElementById('site-switcher');
    if (!host) return;

    const picker = h('select', {
      'aria-label': 'Switch site',
      onchange: async (e) => {
        const slug = e.target.value;
        if (slug === user.tenantSlug) return;
        e.target.disabled = true;
        try {
          await api.post(`/api/platform/switch?tenant_slug=${encodeURIComponent(slug)}`, {});
          // Full reload: every cached list on screen belongs to the
          // site we just left.
          window.location.reload();
        } catch (err) {
          toast(err.message, 'error');
          e.target.disabled = false;
          e.target.value = user.tenantSlug;
        }
      },
    }, sites.map((s) => h('option', {
      value: s.slug,
      selected: s.slug === user.tenantSlug,
      text: s.is_home ? `${s.name} (home)` : s.name,
    })));

    mount(host, picker);
    host.classList.remove('hidden');
  }

  /**
   * Unread count in the sidebar. Polled rather than pushed: this admin
   * has no websocket, and a 60-second poll is cheap next to adding one.
   */
  function startNotificationBadge() {
    const paint = async () => {
      try {
        const { unread } = await api.get('/api/ops/notifications?unread_only=true&limit=10');
        session.unread = unread;
        const host = document.getElementById('notify-badge');
        if (!host) return;
        host.textContent = unread ? String(unread) : '';
        host.classList.toggle('hidden', !unread);
      } catch (err) {
        // A role without ops.view gets a 403 here; stop asking.
        if (err.status === 403) clearInterval(timer);
      }
    };
    const timer = setInterval(paint, 60000);
    paint();
  }

  boot();
}());
