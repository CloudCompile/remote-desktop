/* Pre-paint theme bootstrap (#51, #117): applies the stored theme before
 * first paint so the wrong theme never flashes. External file so the
 * CSP can forbid inline scripts (#117). */
(function () {
  try {
    var p = localStorage.getItem('rd_theme') || 'dark';
    if (p === 'auto') p = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', p);
  } catch (_) {}
})();
