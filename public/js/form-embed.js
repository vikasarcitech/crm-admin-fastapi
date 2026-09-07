/* global window, document, sessionStorage */
/**
 * Drop-in intake script for a static or WordPress front end.
 *
 * Captures first-touch UTMs once per session, adds a honeypot timing
 * check, and POSTs to the CRM. Rename TENANT/FORM and point ENDPOINT at
 * your admin host. No dependencies, no jQuery.
 */
(function () {
  'use strict';

  var ENDPOINT = window.CRM_ENDPOINT || window.location.origin;
  var TENANT = window.CRM_TENANT || 'demo';
  var FORM = window.CRM_FORM || 'contact';
  var UTM_KEYS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'];
  var STORAGE_KEY = 'crm_attribution';

  /**
   * First touch wins: if the visitor arrived from a campaign, browsed
   * three pages, then converted, we still credit the campaign.
   */
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

  var form = document.getElementById('lead-form');
  if (!form) return;

  var renderedAt = Date.now();
  var errorBox = document.getElementById('error');
  var doneBox = document.getElementById('done');
  var submit = document.getElementById('submit');

  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (errorBox) errorBox.classList.add('hidden');
    submit.disabled = true;
    submit.textContent = 'Sending…';

    var data = new FormData(form);
    var payload = {
      _hp: data.get('_hp') || '',
      _t: renderedAt,
      meta: attribution(),
    };
    ['full_name', 'email', 'phone', 'company', 'message'].forEach(function (key) {
      payload[key] = (data.get(key) || '').toString().trim();
    });

    // If you enable Turnstile, add the widget and pass its token here:
    // payload._captcha = window.turnstile.getResponse();

    try {
      var res = await fetch(
        ENDPOINT + '/api/public/' + TENANT + '/forms/' + FORM,
        {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(payload),
        }
      );
      var body = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(body.error || 'That did not go through. Please try again.');

      form.classList.add('hidden');
      if (doneBox) doneBox.classList.remove('hidden');
    } catch (err) {
      if (errorBox) {
        errorBox.textContent = err.message;
        errorBox.classList.remove('hidden');
      }
      submit.disabled = false;
      submit.textContent = 'Send enquiry';
    }
  });
}());
