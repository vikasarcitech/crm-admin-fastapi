/* global window, document, api, ui, views */
/** Bootstrap + hash router. */
(function () {
  'use strict';

  const { h, mount, toast, closeDrawer } = ui;

  const ROUTES = [
    { path: 'dashboard', label: 'Dashboard', view: 'dashboard', icon: 'M3 12h4l2 5 3-11 2 6h5' },
    { path: 'leads', label: 'Leads', view: 'leads', icon: 'M4 5h16M4 12h16M4 19h10', countKey: 'all' },
    { path: 'pages', label: 'Pages', view: 'pages', icon: 'M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6' },
    { path: 'users', label: 'Users', view: 'users', icon: 'M4 19a5 5 0 0110 0M9 4a3 3 0 100 6 3 3 0 000-6' },
    { path: 'webhooks', label: 'Webhooks', view: 'webhooks', icon: 'M6 8a4 4 0 106 3M12 20a4 4 0 10-2-7' },
    { path: 'activity', label: 'Activity', view: 'activity', icon: 'M12 6v6l4 2M12 3a9 9 0 100 18 9 9 0 000-18' },
    { path: 'settings', label: 'Settings', view: 'settings', icon: 'M12 9a3 3 0 100 6 3 3 0 000-6M4 12h2m12 0h2M12 4v2m0 12v2' },
  ];

  const session = { user: null, counts: {} };
  let current = { path: 'dashboard', params: {} };

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
    mount(nav, ROUTES.map((r) => {
      const count = r.countKey ? session.counts[r.countKey] : null;
      const link = h('a', {
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
      ]);
      return link;
    }));
  }

  async function render() {
    current = parseHash();
    renderNav();
    closeDrawer();

    const el = document.getElementById('view');
    const route = ROUTES.find((r) => r.path === current.path);
    const ctx = {
      el,
      params: current.params,
      session,
      setHead,
      navigate,
      /** Re-run the current view (after a save). */
      reload: (opts = {}) => {
        if (!opts.keepDrawer) closeDrawer();
        render();
      },
    };

    try {
      await views[route.view](ctx);
    } catch (err) {
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
    document.getElementById('user-name').textContent = me.user.name;
    document.getElementById('user-role').textContent = me.user.role;
    document.getElementById('sign-out').addEventListener('click', async () => {
      try { await api.post('/api/auth/logout'); } finally { window.location.href = '/login'; }
    });

    // Lead counts drive the sidebar badge; a failure here is not fatal.
    api.get('/api/leads/counts')
      .then((counts) => { session.counts = counts; renderNav(); })
      .catch(() => {});

    window.addEventListener('hashchange', render);
    render();
  }

  boot();
}());
