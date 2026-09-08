/* global window, api, kit, ui, navigator */
/** Analytics (2.9) — first-party KPIs plus GA4/GTM/Search Console setup. */
(function () {
  'use strict';

  const { h, mount, toast, table, panel, panelBody, toolbar, badge, select,
    actionButton, notice, tabs, number, percent, kpi, formatDate,
    textInput, checkbox, confirmButton } = kit;

  async function insights(ctx) {
    const tab = ctx.params.tab || 'overview';
    const days = Number(ctx.params.days) || 30;
    mount(ctx.el, ui.spinner());

    const [overview, pages, portfolio, integrations] = await Promise.all([
      api.get(`/api/analytics/overview${api.qs({ days })}`),
      api.get(`/api/analytics/pages${api.qs({ days })}`),
      api.get(`/api/analytics/portfolio${api.qs({ days })}`),
      api.get('/api/analytics/integrations'),
    ]);

    ctx.setHead('Analytics', `Last ${days} days`,
      select([[7, 'Last 7 days'], [30, 'Last 30 days'], [90, 'Last 90 days'],
        [365, 'Last 12 months']], days,
      (e) => ctx.navigate(`#/insights${api.qs({ ...ctx.params, days: e.target.value })}`)));

    const strip = tabs(ctx, [
      ['overview', 'Overview'],
      ['pages', 'Pages', pages.pages.length],
      ['portfolio', 'Portfolio', portfolio.sites.length],
      ['integrations', 'Integrations'],
    ], tab);

    const panes = {
      overview: () => overviewPane(ctx, overview),
      pages: () => pagesPane(ctx, pages),
      portfolio: () => portfolioPane(ctx, portfolio),
      integrations: () => integrationsPane(ctx, integrations),
    };
    mount(ctx.el, [strip, (panes[tab] || panes.overview)()]);
  }

  const delta = (value) => (value === null || value === undefined
    ? null
    : `${value >= 0 ? '+' : ''}${value}% vs previous period`);

  function overviewPane(ctx, data) {
    const k = data.kpis;
    const peak = Math.max(1, ...data.byDay.map((d) => Math.max(d.views, d.leads)));

    return h('div.stack', {}, [
      data.hasTrafficData
        ? null
        : notice('No traffic data yet. Add the beacon to your frontend — '
          + 'POST /api/public/{site}/collect on each page view — or turn on GA4 '
          + 'in Integrations.', 'info'),
      h('div.kpis', {}, [
        kpi('Visitors', number(k.visitors), delta(k.visitorsChange)),
        kpi('Page views', number(k.views), delta(k.viewsChange)),
        kpi('Leads', number(k.leads), `${number(k.leadsOpen)} still open`),
        kpi('Conversion rate', percent(k.conversionRate, 2), 'Leads per visitor'),
        kpi('Conversions', number(k.conversions), 'Forms, calls, WhatsApp, CTAs'),
        kpi('Subscribers', number(k.subscribers),
          `${k.subscribersNew >= 0 ? '+' : ''}${k.subscribersNew} new, `
          + `${k.subscribersChurned} left`),
        kpi('Blog views', number(k.blogViews)),
        kpi('Won value', number(k.wonValue), `${number(k.leadsWon)} deals`),
      ]),
      panel('Traffic and leads', panelBody([
        h('div.ledger', {}, data.byDay.map((d) => h('div.ledger-stack', {
          title: `${d.day}: ${d.views} views, ${d.visitors} visitors, ${d.leads} leads`,
        }, [
          h('div.ledger-bar', {
            dataset: { empty: d.views === 0 ? '1' : '0' },
            style: `height:${Math.max(2, Math.round((d.views / peak) * 100))}%`,
          }),
          h('div.ledger-bar.is-leads', {
            dataset: { empty: d.leads === 0 ? '1' : '0' },
            style: `height:${Math.max(2, Math.round((d.leads / peak) * 100))}%`,
          }),
        ]))),
        h('div.ledger-axis', {}, [
          h('span', { text: data.byDay[0]?.day || '' }),
          h('span', { text: `peak ${peak}/day · views vs leads` }),
          h('span', { text: data.byDay[data.byDay.length - 1]?.day || '' }),
        ]),
      ])),
      h('div.split', {}, [
        panel('Top pages', table([
          { label: 'Path', cell: (p) => h('code', { text: p.path }) },
          { label: 'Views', class: 'cell-mono', cell: (p) => number(p.views) },
          { label: 'Visitors', class: 'cell-mono', cell: (p) => number(p.visitors) },
        ], data.topPages, { empty: 'No page views recorded yet.' })),
        h('div.stack', {}, [
          panel('Traffic sources', table([
            { label: 'Source', cell: (s) => s.source },
            { label: 'Medium', cell: (s) => badge(s.medium, 'neutral') },
            { label: 'Sessions', class: 'cell-mono', cell: (s) => number(s.sessions) },
          ], data.trafficSources, { empty: 'No sessions recorded yet.' })),
          panel('Lead sources', table([
            { label: 'Source', cell: (s) => s.source },
            { label: 'Leads', class: 'cell-mono', cell: (s) => number(s.n) },
          ], data.leadSources, { empty: 'No attributed leads yet.' })),
          panel('Conversions by type', table([
            { label: 'Type', cell: ([key]) => key },
            { label: 'Count', class: 'cell-mono', cell: ([, value]) => number(value) },
          ], Object.entries(data.conversionsByKind), { empty: 'Nothing yet.' })),
        ]),
      ]),
    ]);
  }

  function pagesPane(ctx, data) {
    return panel(null, [
      table([
        { label: 'Path', cell: (p) => h('code', { text: p.path }) },
        { label: 'Content', cell: (p) => (p.title
          ? h('span', {}, [h('span', { text: p.title }),
            h('span.muted', { text: ` (${p.type_slug})` })])
          : h('span.muted', { text: 'not a CMS page' })) },
        { label: 'Views', class: 'cell-mono', cell: (p) => number(p.views) },
        { label: 'Visitors', class: 'cell-mono', cell: (p) => number(p.visitors) },
        { label: 'Conversions', class: 'cell-mono', cell: (p) => number(p.conversions) },
        {
          label: 'Rate',
          class: 'cell-mono',
          cell: (p) => (p.visitors ? percent((p.conversions / p.visitors) * 100, 1) : '—'),
        },
      ], data.pages, { empty: 'No page-level traffic yet.' }),
      panelBody(h('p.muted', {
        text: 'Rows are matched back to content by path, so a renamed page starts a new row.',
      })),
    ]);
  }

  function portfolioPane(ctx, data) {
    return h('div.stack', {}, [
      h('div.kpis', {}, [
        kpi('Sites', data.sites.length),
        kpi('Visitors', number(data.totals.visitors)),
        kpi('Leads', number(data.totals.leads)),
        kpi('Subscribers', number(data.totals.subscribers)),
      ]),
      panel(null, table([
        { label: 'Site', cell: (s) => [h('div.cell-name', { text: s.name }),
          h('div.cell-meta', { text: s.slug })] },
        { label: 'Visitors', class: 'cell-mono', cell: (s) => number(s.visitors) },
        { label: 'Views', class: 'cell-mono', cell: (s) => number(s.views) },
        { label: 'Leads', class: 'cell-mono', cell: (s) => number(s.leads) },
        { label: 'Won', class: 'cell-mono', cell: (s) => number(s.won) },
        { label: 'Conv. rate', class: 'cell-mono', cell: (s) => percent(s.conversionRate, 2) },
        { label: 'Subscribers', class: 'cell-mono', cell: (s) => number(s.subscribers) },
        { label: 'Published', class: 'cell-mono', cell: (s) => number(s.published_content) },
      ], data.sites, { empty: 'No sites you can see.' })),
      panelBody(h('p.muted', {
        text: 'Only sites this account has access to. A Super Admin sees the whole install.',
      })),
    ]);
  }

  function integrationsPane(ctx, data) {
    const ga4 = textInput('ga4', data.config.ga4_measurement_id, { placeholder: 'G-XXXXXXXXXX' });
    const gtm = textInput('gtm', data.config.gtm_container_id, { placeholder: 'GTM-XXXXXXX' });
    const gsc = textInput('gsc', data.config.search_console_verification);
    const firstParty = checkbox('first_party', data.config.first_party_beacon !== false,
      'Collect first-party page views');

    const snippet = (label, value) => (value
      ? panel(label, panelBody([
        h('pre.code-block', { text: value }),
        actionButton('Copy', async () => {
          await navigator.clipboard?.writeText(value).catch(() => {});
          toast('Copied — paste it into your frontend’s <head>.');
        }, { small: true }),
      ]))
      : null);

    return h('div.split', {}, [
      panel('Configuration', panelBody([
        h('label.field', {}, [h('span', { text: 'GA4 measurement id' }), ga4]),
        h('label.field', {}, [h('span', { text: 'GTM container id' }), gtm]),
        h('label.field', {}, [
          h('span', { text: 'Search Console verification token' }), gsc,
          h('small.muted', { text: 'The content value of the google-site-verification meta tag.' }),
        ]),
        h('label.field', {}, [h('span', { text: 'First-party analytics' }), firstParty]),
        actionButton('Save integrations', async () => {
          await api.put('/api/site/settings/analytics', {
            value: {
              ...data.config,
              ga4_measurement_id: ga4.value.trim() || null,
              gtm_container_id: gtm.value.trim() || null,
              search_console_verification: gsc.value.trim() || null,
              first_party_beacon: firstParty.querySelector('input').checked,
            },
          });
          toast('Saved.');
          ctx.reload();
        }, { primary: true }),
        h('hr'),
        h('h3', { text: 'Endpoints for your frontend' }),
        h('p.muted', { text: `Page views: POST ${data.beaconEndpoint}` }),
        h('p.muted', { text: `Conversions: POST ${data.conversionEndpoint}` }),
        h('hr'),
        confirmButton('Delete all first-party analytics', async () => {
          const result = await api.del('/api/analytics/data?confirm=true');
          toast(`Deleted ${Object.values(result.deleted).reduce((a, b) => a + b, 0)} row(s).`);
          ctx.reload();
        }),
      ])),
      h('div.stack', {}, [
        snippet('Google Tag Manager', data.snippets.gtm),
        snippet('GA4 (gtag.js)', data.snippets.ga4),
        snippet('Search Console verification', data.snippets.searchConsole),
        data.snippets.gtm || data.snippets.ga4
          ? null
          : panelBody(h('p.muted', {
            text: 'Add an id on the left and the snippet to paste appears here.',
          })),
      ]),
    ]);
  }

  window.insightsViews = { insights };
}());
