/* global window, document, sessionStorage */
/**
 * Lead form handler for builder-published pages (/p/{tenant}/{slug}).
 *
 * The page CSP has no 'unsafe-inline', so configuration travels on the
 * form element itself: data-tenant + data-form-slug. Mirrors
 * form-embed.js: first-touch UTM attribution, honeypot and fill timing.
 */
(function () {
  'use strict';

  var UTM_KEYS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'];
  var STORAGE_KEY = 'crm_attribution';
  var renderedAt = Date.now();

  function attribution() {
    var stored = null;
    try { stored = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null'); } catch (e) { /* private mode */ }

    var params = new URLSearchParams(window.location.search);
    var fresh = {};
    UTM_KEYS.forEach(function (key) {
      var value = params.get(key);
      if (value) fresh[key] = value.slice(0, 160);
    });

    if (Object.keys(fresh).length || !stored) {
      stored = Object.assign({
        referrer: document.referrer || null,
        landing_page: window.location.pathname + window.location.search,
      }, fresh);
      try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(stored)); } catch (e) { /* ignore */ }
    }

    return Object.assign({}, stored, {
      source_page: window.location.pathname + window.location.search,
    });
  }

  function wire(form) {
    var tenant = form.dataset.tenant;
    var slug = form.dataset.formSlug;
    if (!tenant || !slug) return;

    var section = form.parentElement;
    var errorBox = section ? section.querySelector('.form-error') : null;
    var doneBox = section ? section.querySelector('.form-done') : null;
    var submit = form.querySelector('button[type="submit"]');
    var submitLabel = submit ? submit.textContent : 'Send';

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (errorBox) errorBox.hidden = true;
      if (submit) { submit.disabled = true; submit.textContent = 'Sending…'; }

      var payload = { _t: renderedAt, meta: attribution() };
      new FormData(form).forEach(function (value, key) {
        payload[key === '_hp' ? '_hp' : key] = value.toString();
      });

      try {
        var res = await fetch(
          '/api/public/' + encodeURIComponent(tenant) + '/forms/' + encodeURIComponent(slug),
          {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(payload),
          }
        );
        var body = await res.json().catch(function () { return {}; });
        if (!res.ok) throw new Error(body.error || 'That did not go through. Please try again.');

        form.hidden = true;
        if (doneBox) doneBox.hidden = false;
      } catch (err) {
        if (errorBox) {
          errorBox.textContent = err.message;
          errorBox.hidden = false;
        }
        if (submit) { submit.disabled = false; submit.textContent = submitLabel; }
      }
    });
  }

  document.querySelectorAll('form[data-crm-form]').forEach(wire);
}());
