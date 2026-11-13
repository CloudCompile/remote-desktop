/* Login page logic (v2.6.0): password + TOTP second factor (#109),
 * optional passkey login (#110), i18n (#54), CSRF-safe by design (#116:
 * /api/login carries no ambient credential; cross-origin logins are
 * refused server-side via Origin/Sec-Fetch-Site checks). */

const $ = id => document.getElementById(id);
const msg = $('msg');
let twoFA = false;

const LOCALES = ['en', 'de', 'es', 'fr'];
let dict = {};
function t(key, fallback){ return typeof dict[key] === 'string' && dict[key] ? dict[key] : (fallback || key); }
function apply(){
  document.querySelectorAll('[data-i18n]').forEach(el => (el.textContent = t(el.dataset.i18n, el.textContent)));
  document.querySelectorAll('[data-i18n-ph]').forEach(el => (el.placeholder = t(el.dataset.i18nPh, el.placeholder)));
  document.title = t('login.title', document.title);
}
async function loadLang(lang){
  if (!LOCALES.includes(lang)) lang = 'en';
  try {
    const r = await fetch('/locales/' + lang + '.json', { cache: 'no-store' });
    if (r.ok) dict = await r.json();
  } catch (_) { dict = {}; }
  apply();
}
(function initLang(){
  try {
    const pref = localStorage.getItem('rd_lang');
    const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
    const lang = LOCALES.includes(pref) ? pref : nav;
    $('lang').value = LOCALES.includes(lang) ? lang : 'en';
  } catch (_) {}
  loadLang($('lang').value);
  $('lang').addEventListener('change', e => {
    try { localStorage.setItem('rd_lang', e.target.value); } catch (_) {}
    loadLang(e.target.value);
  });
})();

/* passkey button only when the server enables the feature (#110) */
(async () => {
  try {
    const r = await fetch('/api/health');
    if (!r.ok) return;
    // health is public but does not advertise flags; probe the ceremony
    // endpoint once — 403 with the disabled message hides the button
    const o = await fetch('/api/webauthn/auth/options', { method: 'POST' });
    if (o.ok) $('passkey').hidden = false;
  } catch (_) {}
})();

$('passkey').addEventListener('click', async () => {
  msg.textContent = '';
  try {
    const o = await (await fetch('/api/webauthn/auth/options', { method: 'POST' })).json();
    if (!o.ok) { msg.textContent = o.error || 'passkeys unavailable'; return; }
    const cred = await navigator.credentials.get({ publicKey: o });
    const j = await (await fetch('/api/webauthn/auth/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ credential: cred }),
    })).json();
    if (j.ok) location.href = '/';
    else msg.textContent = j.error || 'Passkey sign-in failed';
  } catch (e) {
    msg.textContent = String(e.message || e).slice(0, 140);
  }
});

function enter2fa(){
  twoFA = true;
  $('step1').style.display = 'none';
  $('step2fa').style.display = 'block';
  $('code').focus();
}
$('back').addEventListener('click', () => {
  twoFA = false;
  $('step2fa').style.display = 'none';
  $('step1').style.display = 'block';
  msg.textContent = '';
  $('p').value = '';
  $('p').focus();
});

document.getElementById('f').addEventListener('submit', async e => {
  e.preventDefault();
  msg.textContent = '';
  try {
    const body = twoFA
      ? { username: $('u').value, password: $('p').value, code: $('code').value.trim() }
      : { username: $('u').value, password: $('p').value };
    const r = await fetch('/api/login', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (j.ok) {
      location.href = '/';
      return;
    }
    if (j.needs_2fa && !twoFA) {
      enter2fa();
      msg.textContent = j.error || t('login.totpPrompt', 'Enter the code from your authenticator app');
      return;
    }
    msg.textContent = j.error || t('login.failed', 'Login failed');
  } catch (_) {
    msg.textContent = t('login.unreachable', 'Could not reach the server.');
  }
});
