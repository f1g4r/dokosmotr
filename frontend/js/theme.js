'use strict';
(() => {
  let preference;
  try {
    preference = localStorage.getItem('dokosmotr-theme');
  } catch (_) {
    /* Storage may be blocked. */
  }
  document.documentElement.dataset.theme =
    preference || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('theme');
    button?.addEventListener('click', () => {
      const value = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = value;
      try {
        localStorage.setItem('dokosmotr-theme', value);
      } catch (_) {
        /* Optional preference. */
      }
    });
  });
})();
