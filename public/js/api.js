/* global window */
/**
 * Thin fetch wrapper. Attaches the CSRF header to writes, sends cookies,
 * and turns non-2xx responses into thrown Errors with a usable message.
 */
(function () {
  'use strict';

  let csrfToken = null;

  async function request(method, path, body, opts = {}) {
    const headers = {};
    if (body !== undefined && !(body instanceof FormData)) {
      headers['content-type'] = 'application/json';
    }
    if (csrfToken && !['GET', 'HEAD'].includes(method)) {
      headers['x-csrf-token'] = csrfToken;
    }

    let res;
    try {
      res = await fetch(path, {
        method,
        headers,
        credentials: 'same-origin',
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (err) {
      throw new Error('Cannot reach the server. Check your connection.');
    }

    if (res.status === 401 && !opts.allowUnauthenticated) {
      // Session gone — bounce to sign-in rather than rendering an empty shell.
      window.location.href = '/login';
      throw new Error('Session expired');
    }

    const isJson = (res.headers.get('content-type') || '').includes('application/json');
    const payload = isJson ? await res.json().catch(() => ({})) : null;

    if (!res.ok) {
      const err = new Error(payload?.error || `Request failed (${res.status})`);
      err.status = res.status;
      err.details = payload?.details;
      throw err;
    }
    return payload;
  }

  window.api = {
    get: (p) => request('GET', p),
    post: (p, b, o) => request('POST', p, b ?? {}, o),
    patch: (p, b) => request('PATCH', p, b ?? {}),
    put: (p, b) => request('PUT', p, b ?? {}),
    del: (p) => request('DELETE', p, {}),
    setCsrf: (t) => { csrfToken = t; },
    /** Read the token back, for multipart uploads that bypass request(). */
    csrf: () => csrfToken,
    /** Build a query string, dropping empty values. */
    qs: (params) => {
      const s = new URLSearchParams();
      Object.entries(params || {}).forEach(([k, v]) => {
        if (v !== '' && v !== null && v !== undefined) s.set(k, v);
      });
      const str = s.toString();
      return str ? `?${str}` : '';
    },
  };
}());
