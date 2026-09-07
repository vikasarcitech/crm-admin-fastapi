/* global window, document, api */
(function () {
  'use strict';

  const form = document.getElementById('register-form');
  const errorBox = document.getElementById('error');
  const submit = document.getElementById('submit');

  function showError(message) {
    errorBox.textContent = message;
    errorBox.classList.remove('hidden');
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    errorBox.classList.add('hidden');

    const password = document.getElementById('password').value;
    if (password.length < 12) {
      return showError('Passwords need at least 12 characters.');
    }

    submit.disabled = true;
    submit.textContent = 'Creating…';

    try {
      await api.post('/api/auth/register', {
        workspace: document.getElementById('workspace').value.trim(),
        display_name: document.getElementById('display_name').value.trim(),
        email: document.getElementById('email').value.trim(),
        password,
      }, { allowUnauthenticated: true });
      window.location.href = '/';
    } catch (err) {
      showError(err.message);
      submit.disabled = false;
      submit.textContent = 'Create workspace';
    }
  });
}());
