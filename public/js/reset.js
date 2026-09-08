/* global window, document, api */
(function () {
  'use strict';

  const form = document.getElementById('reset-form');
  const errorBox = document.getElementById('error');
  const submit = document.getElementById('submit');
  const token = new URLSearchParams(window.location.search).get('token') || '';

  function showError(message) {
    errorBox.textContent = message;
    errorBox.classList.remove('hidden');
  }

  if (!token) showError('This reset link is missing its token. Use the link from the email.');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    errorBox.classList.add('hidden');

    const password = document.getElementById('password').value;
    if (password.length < 12) return showError('Passwords need at least 12 characters.');
    if (password !== document.getElementById('confirm').value) {
      return showError('Those passwords do not match.');
    }

    submit.disabled = true;
    submit.textContent = 'Saving…';

    try {
      await api.post('/api/auth/reset', { token, password }, { allowUnauthenticated: true });
      window.location.href = '/login';
    } catch (err) {
      showError(err.message);
      submit.disabled = false;
      submit.textContent = 'Set new password';
    }
  });
}());
