// Style (modern / classic), theme (and shade) before first paint: the stored choice, else prefers-color-scheme (dark-first).
// A file, not an inline script, so a Content-Security-Policy can stay script-src 'self'.
try {
  var t = localStorage.getItem('alertbot-theme')
  if (!t) t = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
  document.documentElement.dataset.theme = t
  document.documentElement.dataset.style = localStorage.getItem('alertbot-style') === 'classic' ? 'classic' : 'modern'
  var sh = localStorage.getItem('alertbot-shade')
  if (sh && sh !== 'ocean' && sh !== 'daylight') document.documentElement.dataset.shade = sh
} catch (e) {
  document.documentElement.dataset.theme = 'dark'
  document.documentElement.dataset.style = 'modern'
}
