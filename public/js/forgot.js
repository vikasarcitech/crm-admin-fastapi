/* global window, document, api */
(function () {
  'use strict';

  const form = document.getElementById('forgot-form');
  const errorBox = document.getElementById('error');
  const submit = document.getElementById('submit');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    errorBox.classList.add('hidden');
    submit.disabled = true;
    submit.textContent = 'Sending…';

    try {
      const res = await api.post('/api/auth/forgot', {
        tenant: document.getElementById('tenant').value.trim(),
        email: document.getElementById('email').value.trim(),
      }, { allowUnauthenticated: true });
      // Same message whether or not the account exists.
      form.replaceWith(Object.assign(document.createElement('p'), {
        textContent: res.message || 'If that account exists, a reset link is on its way.',
      }));
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.classList.remove('hidden');
      submit.disabled = false;
      submit.textContent = 'Email me a reset link';
    }
  });
}());
