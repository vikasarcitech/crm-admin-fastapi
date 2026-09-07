/* global window, document, api */
(function () {
  'use strict';

  const form = document.getElementById('login-form');
  const errorBox = document.getElementById('error');
  const submit = document.getElementById('submit');

  function showError(message) {
    errorBox.textContent = message;
    errorBox.classList.remove('hidden');
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    errorBox.classList.add('hidden');
    submit.disabled = true;
    submit.textContent = 'Signing in…';

    try {
      await api.post('/api/auth/login', {
        tenant: document.getElementById('tenant').value.trim(),
        email: document.getElementById('email').value.trim(),
        password: document.getElementById('password').value,
      }, { allowUnauthenticated: true });
      window.location.href = '/';
    } catch (err) {
      showError(err.message);
      submit.disabled = false;
      submit.textContent = 'Sign in';
      document.getElementById('password').value = '';
    }
  });
}());
