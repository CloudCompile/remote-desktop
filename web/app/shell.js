/* Remote Desktop shell (#54, v2.6.0): extracted from web/index.html so the
 * CSP can forbid inline scripts (#117). Imports are relative to web/app/.
 *
 * CSRF (#116): the fetch wrapper below attaches the per-session token to
 * every state-changing request; the token mirrors the rd_csrf cookie set
 * at login and is refreshed from /api/csrf when missing.
 */

/* ---- CSRF fetch wrapper (#116) ------------------------------------ */
const _origFetch = window.fetch.bind(window);
function _csrfFromCookie() {
  const m = document.cookie.match(/(?:^|;\s*)rd_csrf=([^;]+)/);
  return m ? m[1] : '';
}
window.fetch = async function (input, init) {
  try {
    init = init || {};
    const method = (init.method || (init.body ? 'POST' : 'GET')).toUpperCase();
    const url = typeof input === 'string' ? input : (input && input.url) || String(input || '');
    const sameOrigin = url.startsWith('/') || url.startsWith(window.location.origin + '/');
    const mutating = method === 'POST' || method === 'DELETE' || method === 'PUT' || method === 'PATCH';
    const skip = url.includes('/api/login') || url.includes('/api/logout') || url.includes('/api/webauthn/auth');
    const h = init.headers instanceof Headers ? Object.fromEntries(init.headers.entries()) : Object.assign({}, init.headers);
    if (sameOrigin && mutating && !skip && !h['X-CSRF-Token']) {
      let tok = _csrfFromCookie();
      if (!tok) {
        const r = await _origFetch('/api/csrf', { credentials: 'same-origin' });
        if (r.ok) tok = (await r.json()).token || '';
      }
      if (tok) h['X-CSRF-Token'] = tok;
      init.headers = h;
    }
  } catch (_) { /* never break the caller because of token plumbing */ }
  return _origFetch(input, init);
};

import RFB from '../core/rfb.js';
import { initTouch } from '../touch.js';

const $ = id => document.getElementById(id);
const root = document.documentElement;
const screenEl = $('screen'), appEl = $('app');
const probe = $('probe'), statusEl = $('status');
let rfb = null, connected = false;

const NAV_DEFAULT = 48;
const store = {
  get(k, d){ try { const v = localStorage.getItem('rd_' + k); return v === null ? d : v; } catch(_) { return d; } },
  set(k, v){ try { localStorage.setItem('rd_' + k, String(v)); } catch(_) {} },
  del(k){ try { localStorage.removeItem('rd_' + k); } catch(_) {} },
};

let statusTimer = null;
function status(t, err){
  statusEl.textContent = t;
  statusEl.classList.toggle('err', !!err);
  statusEl.classList.add('show');
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => statusEl.classList.remove('show'), 4000);
  $('ovText').textContent = t;
}

/* ------------------------------------------------------------------ *
 *  Localization (#54): shell strings live in web/locales/*.json.      *
 *  Missing keys fall back to English; switching applies instantly.    *
 * ------------------------------------------------------------------ */
const LOCALES = ['en', 'de', 'es', 'fr'];
let i18nDict = {}, i18nLang = 'en';
function i18nT(key, fallback){
  const v = i18nDict[key];
  if (typeof v === 'string' && v) return v;
  return fallback ?? key;      // English fallback everywhere
}
function i18nApply(){
  document.querySelectorAll('[data-i18n]').forEach(el =>
    el.textContent = i18nT(el.dataset.i18n, el.textContent));
  document.querySelectorAll('[data-i18n-ph]').forEach(el =>
    el.placeholder = i18nT(el.dataset.i18nPh, el.placeholder));
  document.querySelectorAll('[data-i18n-aria]').forEach(el =>
    el.setAttribute('aria-label', i18nT(el.dataset.i18nAria, el.getAttribute('aria-label'))));
}
async function i18nLoad(lang){
  if (!LOCALES.includes(lang)) lang = 'en';
  try {
    const r = await fetch('/locales/' + lang + '.json', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    i18nDict = await r.json();
    i18nLang = lang;
  } catch (_) {
    i18nDict = {};
    i18nLang = 'en';
  }
  i18nApply();
  // touch.js builds labelled controls; rebuild them in the new language
  if (window.__touch && window.__touch.rebuild) try { window.__touch.rebuild(); } catch (_) {}
}
function detectLang(){
  const pref = store.get('lang', null);
  if (pref && LOCALES.includes(pref)) return pref;
  const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
  return LOCALES.includes(nav) ? nav : 'en';
}
$('langSel').value = detectLang();
$('langSel').addEventListener('change', e => {
  store.set('lang', e.target.value);
  i18nLoad(e.target.value);
});
i18nLoad(detectLang());

/* Transient action overlay: names the gesture that fired (#44) and carries
   an optional undo button for destructive taps (#68). */
let toastTimer = null, toastUndo = null;
function toast(msg, action){
  const el = $('toast');
  if (!el) return;
  $('toastMsg').textContent = msg;
  const undo = $('toastUndo');
  toastUndo = action && action.fn ? action : null;
  undo.hidden = !toastUndo;
  el.hidden = false;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.classList.remove('show'); setTimeout(() => { el.hidden = true; }, 250); }, 3500);
}
$('toastUndo').addEventListener('click', () => {
  const u = toastUndo;
  toastUndo = null;
  $('toast').classList.remove('show');
  $('toast').hidden = true;
  if (u && u.fn) try { u.fn(); } catch (_) {}
});

/* #75: launch failures surface remediation + the failing command (with a
   copy action) + the request id for the server-log correlation. */
function launchFail(j, fallback){
  j = j || {};
  const parts = [j.hint || j.error || fallback];
  if (j.cmd) parts.push('$ ' + j.cmd);
  if (j.rid) parts.push('ref ' + j.rid);
  const msg = parts.join('  ·  ');
  toast(msg, j.cmd ? {
    label: 'Copy command',
    fn: () => { navigator.clipboard.writeText(j.cmd).then(() => status('Command copied')).catch(() => {}); },
  } : null);
  status(msg, true);
}

/* iOS Safari (#57): Safari fires non-standard gesturestart/gesturechange
   for two-finger pinch and zooms the PAGE unless prevented. The app owns
   pinch (touch.js), so page-level gestures are always rejected. Rubber-
   banding and double-tap zoom are covered by touch-action /
   overscroll-behavior in the stylesheet and by the canvas touch-action:
   none set in touch.js. */
['gesturestart', 'gesturechange', 'gestureend'].forEach(ev =>
  document.addEventListener(ev, e => e.preventDefault(), { passive: false }));

/* ------------------------------------------------------------------ *
 *  Insets: keep the desktop out from under the Android nav bar        *
 * ------------------------------------------------------------------ */
const isTouch = () => matchMedia('(pointer: coarse)').matches || navigator.maxTouchPoints > 1;
const isLandscape = () => innerWidth > innerHeight;

function envInsets(){
  const cs = getComputedStyle(probe);
  return { r: parseFloat(cs.paddingRight) || 0, b: parseFloat(cs.paddingBottom) || 0,
           l: parseFloat(cs.paddingLeft) || 0 };
}
function landscapeSide(){
  const pref = store.get('side', 'auto');
  if (pref !== 'auto') return pref;
  const e = envInsets();
  if (e.r > e.l && e.r >= 8) return 'right';
  if (e.l > e.r && e.l >= 8) return 'left';
  const a = (screen.orientation && screen.orientation.angle) || 0;
  return (a === 270 || a === -90) ? 'left' : 'right';
}
function viewportCoversScreen(){
  const long = Math.max(screen.width, screen.height);
  const gap = isLandscape() ? long - innerWidth : long - innerHeight;
  return gap < 40;
}
// Only reserve an edge when the OS actually paints over the page: env()
// reports a real safe-area inset, or the app runs in true fullscreen.
// In a normal tab / standalone PWA the viewport already ends above the
// system nav bar, so reserving space just leaves a dead strip at the bottom
// that cannot be clicked in the remote UI.
function osOverlaysEdge(){
  try { return matchMedia('(display-mode: fullscreen)').matches; } catch (_) { return false; }
}
function computeInsets(){
  if (!isTouch()) return { ins: {t:0,b:0,l:0,r:0}, how: 'desktop browser' };
  const e = envInsets();
  const manual = store.get('nav', null);
  const nav = manual !== null ? parseInt(manual, 10) : NAV_DEFAULT;
  const auto = (envInset) => envInset >= 8 ? envInset : (osOverlaysEdge() && viewportCoversScreen() ? nav : 0);
  const ins = { t: 0, b: 0, l: 0, r: 0 };
  if (isLandscape()) {
    const side = landscapeSide();
    const envV = side === 'right' ? e.r : e.l;
    ins[side] = manual !== null ? nav : auto(envV);
    return { ins, how: `landscape, nav on ${side}: ${ins[side]}px` };
  }
  ins.b = manual !== null ? nav : auto(e.b);
  return { ins, how: `portrait, nav at bottom: ${ins.b}px` };
}
function applyInsets(){
  const { ins, how } = computeInsets();
  root.style.setProperty('--it', ins.t + 'px');
  root.style.setProperty('--ib', ins.b + 'px');
  root.style.setProperty('--il', ins.l + 'px');
  root.style.setProperty('--ir', ins.r + 'px');
  updateKbInset();
  $('navVal').textContent = store.get('nav', null) !== null ? store.get('nav') : 'auto';
  const r = appEl.getBoundingClientRect();
  $('info').textContent = `${how} · view ${Math.round(r.width)}×${Math.round(r.height)} · window ${innerWidth}×${innerHeight}`;
}

/* Keyboard inset (#58): visualViewport.height shrinks when the on-screen
   keyboard opens. The canvas keeps the layout viewport on purpose —
   resizing it would re-scale the remote desktop and offset the pointer
   mapping — while the shell chrome (toolbar, handle, status, toast and
   the extra-keys bar) lifts above the keyboard via --ikb. */
function updateKbInset(){
  let kb = 0;
  if (window.visualViewport) {
    kb = Math.max(0, Math.round(innerHeight - visualViewport.height));
    if (kb < 120) kb = 0;   // ignore URL-bar collapse and rubber-banding
  }
  root.style.setProperty('--ikb', kb + 'px');
}
if (window.visualViewport) {
  visualViewport.addEventListener('resize', updateKbInset);
  visualViewport.addEventListener('scroll', updateKbInset);
}
['focusin', 'focusout'].forEach(ev =>
  document.addEventListener(ev, () => { setTimeout(updateKbInset, 350); setTimeout(updateKbInset, 900); }));
addEventListener('orientationchange', () => setTimeout(updateKbInset, 300));

/* ------------------------------------------------------------------ *
 *  Remote desktop size: same proportions as the visible area          *
 * ------------------------------------------------------------------ */
function targetSize(){
  const r = appEl.getBoundingClientRect();
  const minW = parseInt(store.get('dw', '0'), 10);
  let w = minW > 0 ? minW : Math.max(r.width, 1000);
  let h = w * (r.height / Math.max(1, r.width));
  w = Math.max(320, Math.round(w / 8) * 8);
  h = Math.max(240, Math.round(h / 8) * 8);
  return { w, h };
}

let lastApiSize = '';
async function matchDesktop(force){
  if (!rfb) return;
  // Only remote-resize mode may change the desktop size (#43); local fit /
  // fill / 1:1 modes are purely client-side scaling.
  if (store.get('vpMode', 'fit') !== 'remote') return;
  const t = targetSize();
  if (rfb._supportsSetDesktopSize) {
    rfb._requestRemoteResize();
    status(`Desktop → ${t.w}×${t.h}`);
    setTimeout(() => fetch('/api/fit', { method: 'POST' }).catch(() => {}), 1000);
    return;
  }
  const key = t.w + 'x' + t.h;
  if (!force && key === lastApiSize) return;
  lastApiSize = key;
  try {
    const res = await fetch(`/api/resize?w=${t.w}&h=${t.h}`, { method: 'POST' });
    const j = await res.json();
    status(j.ok ? `Desktop → ${t.w}×${t.h}` : 'Resize failed: ' + (j.error || '?'));
  } catch (_) { status('Resize API unreachable', true); }
}

function relayout(){
  applyInsets();
  if (rfb) { clearTimeout(relayout.t); relayout.t = setTimeout(() => matchDesktop(false), 400); }
  updateVpInfo();
}

/* ------------------------------------------------------------------ *
 *  Viewport modes (#43): fit / fill / 1:1 / remote-resize             *
 * ------------------------------------------------------------------ */
function vpMode(){ return store.get('vpMode', 'fit'); }
function paintVpSeg(){
  document.querySelectorAll('#vpSeg button').forEach(b => {
    const on = b.dataset.mode === vpMode();
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}
function updateVpInfo(){
  const z = (window.__touch && window.__touch.getZoom) ? window.__touch.getZoom() : 1;
  if ($('zoomVal')) $('zoomVal').textContent = Math.round(z * 100) + '%';
  try {
    const d = rfb._display, fbw = d._fbWidth, fbh = d._fbHeight;
    const r = rfb._canvas.getBoundingClientRect();
    const eff = r.width / Math.max(1, fbw);
    const stretched = Math.abs((r.width / Math.max(1, r.height)) - (fbw / Math.max(1, fbh))) > 0.02;
    $('vpScale').textContent = `fb ${fbw}×${fbh} · on-screen ${Math.round(eff * 100)}% · ` +
      (stretched ? 'stretched' : 'aspect preserved') + ` · ${vpMode()}`;
  } catch (_) { if ($('vpScale')) $('vpScale').textContent = ''; }
}
function applyVpMode(){
  paintVpSeg();
  if (!rfb) return;
  const m = vpMode();
  rfb.scaleViewport = m !== 'one';       // 1:1 shows native pixels unscaled
  rfb.clipViewport = m === 'one';
  rfb.resizeSession = m === 'remote';    // only remote-resize may change the desktop
  if (window.__touch && window.__touch.setVpMode) window.__touch.setVpMode(m);
  updateVpInfo();
  if (m === 'remote') matchDesktop(true);
}
document.querySelectorAll('#vpSeg button').forEach(b =>
  b.addEventListener('click', () => { store.set('vpMode', b.dataset.mode); applyVpMode(); }));
$('vpReset').addEventListener('click', () => {
  if (window.__touch && window.__touch.resetZoom) window.__touch.resetZoom();
  updateVpInfo();
});

/* ------------------------------------------------------------------ *
 *  Connect with automatic reconnect (no page reload)                  *
 * ------------------------------------------------------------------ */
function readVar(name, def){
  const m = (location.href + location.hash).match(new RegExp('.*[?&]' + name + '=([^&#]*)'));
  return m ? decodeURIComponent(m[1]) : def;
}
const wsUrl = (location.protocol === 'https:' ? 'wss' : 'ws') + '://' +
  location.hostname + (location.port ? ':' + location.port : '') + '/' + readVar('path', 'websockify');
const wsPassword = readVar('password', '');

let reconnectDelay = 1000, reconnectTimer = null, userDisconnected = false;

function connect(){
  clearTimeout(reconnectTimer);
  userDisconnected = false;
  if (rfb) { try { rfb.disconnect(); } catch(_) {} rfb = null; }
  screenEl.textContent = '';
  $('overlay').classList.remove('hide');
  $('ovSpin').style.display = '';
  $('ovBtn').style.display = 'none';
  applyInsets();  // must run before RFB measures its container

  rfb = new RFB(screenEl, wsUrl, { credentials: { password: wsPassword } });
  window.rfb = rfb;
  applyVpMode();                    // scaleViewport / clipViewport / resizeSession per mode (#43)
  rfb.qualityLevel = parseInt(store.get('quality', '6'), 10);

  // custom resize: keep the remote desktop the same proportions as the visible area
  rfb._requestRemoteResize = function(){
    clearTimeout(this._resizeTimeout); this._resizeTimeout = null;
    if (!this._resizeSession || this._viewOnly || !this._supportsSetDesktopSize) return;
    const t = targetSize();
    RFB.messages.setDesktopSize(this._sock, t.w, t.h, this._screenID, this._screenFlags);
  };

  rfb.addEventListener('connect', () => {
    connected = true;
    reconnectDelay = 1000;
    $('overlay').classList.add('hide');
    status('Connected');
    setQuality('');                      // unknown until the first ping
    startPing();                         // quality monitor every 5 s (#50)
    if (audioWant && !audioRunning) setTimeout(() => { if (audioWant && !audioRunning) startAudio(); }, 800);  // #124: audio returns with the session
    if (touchReady && window.__touch?.setRfb) window.__touch.setRfb(rfb);  // re-bind touch layer
    else initTouchOnce();
    applyVpMode();
    paintTouchBtn();
    // re-apply once the first framebuffer has laid out (fill-mode base zoom,
    // remote-resize request) and again for slow links
    setTimeout(applyVpMode, 1200);
    setTimeout(applyVpMode, 2600);
    requestWakeLock();
  });
  rfb.addEventListener('disconnect', e => {
    connected = false;
    releaseWakeLock();
    stopPing();
    const clean = e.detail.clean || userDisconnected;
    if (!clean) reconnectCount++;          // count only auto-reconnects (#50)
    lastDropReason = (e.detail && e.detail.reason) ? String(e.detail.reason)
      : (clean ? 'clean' : 'unknown');
    if (clean) {
      status('Disconnected');
      if (!audioOnlyOn()) {              // audio-only keeps the shell clear (#98)
        $('overlay').classList.remove('hide');
        $('ovSpin').style.display = 'none';
        $('ovBtn').style.display = '';
      }
    } else {
      status('Connection lost – reconnecting…', true);
      if (!audioOnlyOn()) reconnectTimer = setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    }
  });
  rfb.addEventListener('credentialsrequired', () =>
    rfb.sendCredentials({ password: prompt('Password required:') }));
  rfb.addEventListener('desktopname', e => { document.title = 'Remote – ' + e.detail.name; updateVpInfo(); });
  rfb.addEventListener('clipboard', e => {
    if (e.detail.text) { try { navigator.clipboard.writeText(e.detail.text).catch(() => {}); } catch(_) {} }
  });
}

$('ovBtn').addEventListener('click', () => { reconnectDelay = 1000; connect(); });
$('reconnectBtn').addEventListener('click', () => { userDisconnected = false; closeSheets(); connect(); });

/* ------------------------------------------------------------------ *
 *  Touch layer (init once, survives reconnects)                       *
 * ------------------------------------------------------------------ */
let touchReady = false;
function initTouchOnce(){
  if (touchReady) return;
  try {
    initTouch({
      rfb, screenEl, appEl, status, $,
      menu: $('shMore'),           // touch settings get appended to the More sheet
      keyboardButton: $('bKeys'),  // toolbar button toggles the mobile keyboard
      t: i18nT,                    // localized labels for touch settings (#54)
      vpInfo: updateVpInfo,        // keep the zoom indicator live (#42)
      onModeChange: paintTouchBtn, // sync the toolbar toggle (#41)
      actionToast: toast,          // transient gesture overlay (#44)
    });
    touchReady = true;
  } catch (e) { console.warn('touch init failed', e); }
}

/* ------------------------------------------------------------------ *
 *  Direct-touch toolbar toggle (#41)                                  *
 * ------------------------------------------------------------------ */
function paintTouchBtn(){
  const direct = !!(window.__touch && window.__touch.settings && window.__touch.settings.mode === 'direct');
  $('bTouch').classList.toggle('on', direct);
  $('bTouch').setAttribute('aria-pressed', direct ? 'true' : 'false');
}
$('bTouch').addEventListener('click', () => {
  if (window.__touch && window.__touch.toggleMode) window.__touch.toggleMode();
});

/* ------------------------------------------------------------------ *
 *  Wake lock: keep the phone screen on while connected                *
 * ------------------------------------------------------------------ */
let wakeLock = null;
async function requestWakeLock(){
  if (!store.get('wake', '1') || !('wakeLock' in navigator) || wakeLock) return;
  try { wakeLock = await navigator.wakeLock.request('screen'); } catch(_) {}
  wakeLock && wakeLock.addEventListener('release', () => { wakeLock = null; });
}
function releaseWakeLock(){ try { wakeLock && wakeLock.release(); } catch(_) {} wakeLock = null; }
$('wake').checked = store.get('wake', '1') === '1';
$('wake').addEventListener('change', e => {
  store.set('wake', e.target.checked ? '1' : '0');
  e.target.checked ? requestWakeLock() : releaseWakeLock();
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && connected) requestWakeLock();
});

/* ------------------------------------------------------------------ *
 *  Connection quality + reconnect diagnostics (#50)                   *
 * ------------------------------------------------------------------ */
let rttMs = null, reconnectCount = 0, lastDropReason = '—', serverVersion = '—', pingTimer = null;
function setQuality(cls){
  const dot = $('netDot');
  if (!dot) return;
  dot.className = cls;   // good | fair | poor | down | '' (unknown)
}
async function pingHealth(){
  if (!connected || document.hidden) return;
  const t0 = performance.now();
  try {
    const r = await fetch('/api/health?ping=' + Date.now(), { cache: 'no-store' });
    if (r.status === 401) { location.href = '/login'; return; }
    rttMs = Math.round(performance.now() - t0);
    let srvV = '';
    try { srvV = (await r.json()).version || ''; serverVersion = srvV || serverVersion; } catch (_) {}
    // stale-client auto-update (#128 follow-up): compare the served app
    // version against the CONTROLLING service worker's registered version;
    // one automatic reload per deployment, guarded against loops
    try {
      const metaV = (document.querySelector('meta[name="app-version"]') || {}).content || '';
      const ctrl = navigator.serviceWorker.controller;
      const ctrlV = (ctrl?.scriptURL.match(/v=([^&]+)/) || [])[1] || '';
      // first visit: no controller yet = fresh storage, nothing stale
      if (ctrl && metaV && metaV !== ctrlV && !sessionStorage.getItem('rdReloaded')) {
        sessionStorage.setItem('rdReloaded', '1');
        status(`Updating client to v${srvV || metaV}…`);
        setTimeout(() => location.reload(), 600);
        return;
      }
    } catch (_) {}
    setQuality(rttMs < 300 ? 'good' : rttMs < 1000 ? 'fair' : 'poor');
  } catch (_) {
    rttMs = null;
    setQuality('down');
  }
  updateConnBox();
}
function updateConnBox(){
  if (!$('connRtt')) return;
  $('connRtt').textContent = rttMs === null ? (connected ? 'measuring…' : 'disconnected') : rttMs + ' ms';
  $('connReconnects').textContent = String(reconnectCount);
  const r = appEl.getBoundingClientRect();
  $('connViewport').textContent = `${Math.round(r.width)}×${Math.round(r.height)}` +
    (innerWidth > innerHeight ? ' (landscape)' : ' (portrait)');
  $('connVersion').textContent = serverVersion;
  $('connReason').textContent = lastDropReason;
  $('connUA').textContent = navigator.userAgent;
}
function startPing(){ clearInterval(pingTimer); pingHealth(); pingTimer = setInterval(pingHealth, 5000); }
function stopPing(){ clearInterval(pingTimer); pingTimer = null; setQuality('down'); }

/* ------------------------------------------------------------------ *
 *  Themes (#51): dark (default), light, high-contrast, auto           *
 * ------------------------------------------------------------------ */
const themeMQ = matchMedia('(prefers-color-scheme: light)');
function resolveTheme(){
  const pref = store.get('theme', 'dark');
  if (pref === 'auto') return themeMQ.matches ? 'light' : 'dark';
  return ['dark', 'light', 'high'].includes(pref) ? pref : 'dark';
}
function applyTheme(){
  const t = resolveTheme();
  root.setAttribute('data-theme', t);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = t === 'light' ? '#f2f2f4' : '#161616';
  $('themeSel').value = store.get('theme', 'dark');
}
themeMQ.addEventListener?.('change', () => {
  if (store.get('theme', 'dark') === 'auto') applyTheme();
});
$('themeSel').addEventListener('change', e => {
  store.set('theme', e.target.value);
  applyTheme();
  status(e.target.value === 'auto' ? 'Theme: auto' :
    'Theme: ' + e.target.value);
});
applyTheme();

/* ------------------------------------------------------------------ *
 *  Orientation lock (#52): best effort — the API needs fullscreen on  *
 *  most browsers, so a rejected lock degrades to a status message.    *
 * ------------------------------------------------------------------ */
let orientLocked = false;
function paintOrient(){
  $('orientBtn').setAttribute('aria-pressed', orientLocked ? 'true' : 'false');
  $('orientBtn').textContent = orientLocked ? 'Unlock orientation' : 'Lock landscape';
}
$('orientBtn').addEventListener('click', async () => {
  if (!screen.orientation || !screen.orientation.lock) {
    $('orientStatus').textContent = 'Orientation lock is not supported on this browser.';
    return;
  }
  try {
    if (orientLocked) {
      await screen.orientation.unlock();
      orientLocked = false;
      $('orientStatus').textContent = 'Orientation unlocked.';
    } else {
      await screen.orientation.lock('landscape');
      orientLocked = true;
      $('orientStatus').textContent = 'Locked to landscape.';
    }
  } catch (e) {
    orientLocked = false;
    $('orientStatus').textContent =
      'Lock rejected — most browsers only allow it in fullscreen or an installed app.';
  }
  paintOrient();
});
paintOrient();

/* ------------------------------------------------------------------ *
 *  Sheets + toolbar                                                   *
 * ------------------------------------------------------------------ */
const sheets = [$('shApps'), $('shWins'), $('shAudio'), $('shMore'), $('shStore'), $('shFiles'), $('shPreview')];
let statsTimer = null;
let lastFocused = null;
// #53: keyboard/switch users get focus moved into an opened sheet and
// restored to the trigger when it closes
function focusables(el){
  return [...el.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter(n => !n.hidden && n.style.display !== 'none' && !n.disabled);
}
function openSheet(el){
  const was = el.classList.contains('show');
  closeSheets();
  if (!was) {
    el.classList.add('show');
    $('backdrop').classList.add('show');
    lastFocused = document.activeElement;
    const f = focusables(el)[0];
    if (f) setTimeout(() => f.focus({ preventScroll: true }), 120);
  }
}
function closeSheets(){
  let wasOpen = false;
  sheets.forEach(s => { if (s.classList.contains('show')) wasOpen = true; s.classList.remove('show'); });
  $('backdrop').classList.remove('show');
  if (wasOpen && lastFocused) {
    try { lastFocused.focus({ preventScroll: true }); } catch (_) {}
    lastFocused = null;
  }
  if (typeof clipPollStop === 'function') clipPollStop();
  if (typeof stopSyncPoll === 'function') stopSyncPoll();
  winSeq++;   // cancel any in-flight thumbnail fills (#59)
  if (typeof audioSheetStop === 'function') audioSheetStop();  // meter + rec poll stop with the sheet (#103)
  if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
}
$('backdrop').addEventListener('click', closeSheets);
// #53: keep Tab cycling inside the open sheet (focus trap) and Escape closes
addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeSheets(); return; }
  if (e.key !== 'Tab') return;
  const open = sheets.find(s => s.classList.contains('show'));
  if (!open) return;
  const f = focusables(open);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (!open.contains(e.target)) {
    e.preventDefault();
    first.focus({ preventScroll: true });
  } else if (e.shiftKey && e.target === first) {
    e.preventDefault();
    last.focus({ preventScroll: true });
  } else if (!e.shiftKey && e.target === last) {
    e.preventDefault();
    first.focus({ preventScroll: true });
  }
});
$('bApps').addEventListener('click', () => { openSheet($('shApps')); loadApps(); });
$('bStore').addEventListener('click', () => { openSheet($('shStore')); loadStoreBackend(); if (!$('shStore').dataset.loaded) storeDefault(); });
$('bAudio').addEventListener('click', () => {
  openSheet($('shAudio'));
  updateAudioBtn();
  loadSinks();        // #94
  loadAudioApps();    // #95
  recPoll();          // #102
  loadRecordings();
  loadMediaStatus();  // #101
  loadDiag();         // #106
  meterStart();       // #103
});
$('bFiles').addEventListener('click', () => { openSheet($('shFiles')); loadFiles(); });
/* ------------------------------------------------------------------ *
 *  WebRTC transport (#137, opt-in): framebuffer video + desktop audio
 *  as real media tracks — the browser's GPU decodes them. The noVNC
 *  websocket stays connected and keeps carrying input; its canvas is
 *  hidden while the video track is live.                               *
 * ------------------------------------------------------------------ */
let rtcPc = null;
const rtcVideo = $('rtcVideo');

async function rtcStart(){
  if (rtcPc) return;
  $('rtcNote').textContent = 'starting…';
  try {
    rtcPc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
    rtcPc.addTransceiver('audio', { direction: 'recvonly' });
    rtcPc.addTransceiver('video', { direction: 'recvonly' });
    rtcPc.ontrack = e => {
      if (e.track.kind === 'video') {
        rtcVideo.srcObject = e.streams[0];
        rtcVideo.hidden = false;
      } else if (e.track.kind === 'audio') {
        const a = new Audio();
        a.srcObject = e.streams[0];
        a.autoplay = true;
        a.dataset.rtc = '1';
        document.body.append(a);
      }
    };
    rtcPc.onconnectionstatechange = () => {
      const state = rtcPc?.connectionState || 'closed';
      $('rtcNote').textContent = `WebRTC: ${state}`;
      fetch('/api/webrtc/ice-state', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ice:rtcPc?.iceConnectionState||'closed',peer:state})}).catch(()=>{});
      if (state === 'connected') status('WebRTC connected — hardware video decode active');
      if (['failed', 'closed', 'disconnected'].includes(rtcPc.connectionState)) {
        status('WebRTC connection failed — falling back to the canvas', true);
        rtcStop({keepNote: true});
        $('rtcNote').textContent = `WebRTC ${state}; canvas fallback active`;
      }
    };
    rtcPc.oniceconnectionstatechange = () => {
      if (rtcPc) {
        const ice=rtcPc.iceConnectionState;
        $('rtcNote').textContent = `ICE: ${ice}`;
        fetch('/api/webrtc/ice-state', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ice,peer:rtcPc.connectionState})}).catch(()=>{});
      }
    };
    const offer = await rtcPc.createOffer();
    await rtcPc.setLocalDescription(offer);
    // The offer is unusable for a browser behind NAT until ICE candidates
    // have been gathered. Do not signal an empty-candidate SDP; trickle ICE
    // is not implemented by this simple HTTP offer/answer endpoint.
    if (rtcPc.iceGatheringState !== 'complete') {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('ICE candidate gathering timed out; check STUN/UDP access')), 12000);
        const done = () => {
          if (rtcPc && rtcPc.iceGatheringState === 'complete') {
            clearTimeout(timer);
            rtcPc.removeEventListener('icegatheringstatechange', done);
            resolve();
          }
        };
        rtcPc.addEventListener('icegatheringstatechange', done);
        done();
      });
    }
    const r = await fetch('/api/webrtc/offer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sdp: rtcPc.localDescription.sdp, type: rtcPc.localDescription.type }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'signalling failed');
    await rtcPc.setRemoteDescription({ type: j.type, sdp: j.sdp });
    $('rtcChk').checked = true;
    $('rtcNote').textContent = 'WebRTC active — hardware decode';
  } catch (e) {
    status('WebRTC: ' + (e.message || e), true);
    $('rtcNote').textContent = 'unavailable: ' + (e.message || e);
    rtcStop({keepNote: true});
  }
}

function rtcStop(opts){
  opts = opts || {};
  if (rtcPc) { try { rtcPc.close(); } catch(_) {} rtcPc = null; }
  rtcVideo.srcObject = null;
  rtcVideo.hidden = true;
  document.querySelectorAll('audio[data-rtc]').forEach(a => a.remove());
  $('rtcChk').checked = false;
  const note = $('rtcNote');
  if (note && !opts.keepNote) note.textContent = '';
}

function rtcRefreshNote(){
  $('rtcChk').checked = store.get('rtc', '') === '1';
}

$('rtcChk').addEventListener('change', e => {
  if (e.target.checked) { store.set('rtc', '1'); rtcStart(); }
  else { store.del('rtc'); rtcStop(); }
});

$('bMore').addEventListener('click', () => {
  openSheet($('shMore'));
  loadClipHistory();
  loadStats();
  updateConnBox();
  paintBarCust();
  rtcRefreshNote();

  // geometry state refresh while open (#76)
  fetch('/api/geometry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    .then(r => r.json())
    .then(j => {
      $('geoChk').checked = j.enabled !== false;
      $('geoNote').textContent = (j.apps || 0) + ' app(s) have remembered geometry.';
    })
    .catch(() => {});
  if (typeof clipPollStart === 'function') clipPollStart();
  if (statsTimer) clearInterval(statsTimer);
  statsTimer = setInterval(() => { loadStats(); updateConnBox(); }, 3000);
});
$('bFull').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen({ navigationUI: 'hide' });
  } catch(_) { status('Fullscreen blocked', true); }
});

// The toolbar overlays the bottom of the remote screen, so it stays out of the
// way: it auto-hides after a few seconds, and touching the remote screen hides
// it immediately so nothing underneath is blocked. A small handle at the bottom
// edge (and the buttons themselves) keep it reachable. (The bar is also
// click-through between its buttons - see the CSS.)
let barHideTimer = null;
function showBar(){
  $('bar').classList.remove('hidden');
  $('barHandle').classList.add('hidden');
  clearTimeout(barHideTimer);
  barHideTimer = setTimeout(hideBar, 6000);
}
function hideBar(){
  clearTimeout(barHideTimer);
  $('bar').classList.add('hidden');
  $('barHandle').classList.remove('hidden');
}
$('barHandle').addEventListener('click', showBar);
['pointerdown'].forEach(ev => {
  document.addEventListener(ev, e => {
    // interacting with the toolbar (re)arms its auto-hide; tapping the handle
    // is handled by its click below
    if ($('bar').contains(e.target)) { showBar(); return; }
    if ($('barHandle').contains(e.target) || e.target.closest('.sheet')) return;
    // any touch on the remote desktop gets the toolbar out of the way at once
    hideBar();
  }, true);
});
hideBar();

/* ------------------------------------------------------------------ *
 *  Apps: dynamic menu of everything installed on the desktop,         *
 *  organized by category chips                                        *
 * ------------------------------------------------------------------ */
const CATS = [
  ['Network', 'Internet'], ['InstantMessaging', 'Chat'],
  ['AudioVideo', 'Music & Video'], ['Office', 'Work'], ['Graphics', 'Graphics'],
  ['Utility', 'Utilities'], ['System', 'System'], ['Development', 'Dev'],
  ['Game', 'Games'], ['Education', 'Learning'], ['Science', 'Learning'],
];
const isWebApp = a => (a.cmd || '').startsWith('google-chrome --app');
function bucketOf(a){
  const cats = (a.categories || '').split(';');
  for (const [k, label] of CATS) if (cats.includes(k)) return label;
  return 'Other';
}
let appEntries = [];   // cached list from /api/apps
let appBucket = 'All'; // active category chip
let pinnedIds = [];    // app ids pinned to the top (server-persisted)
let pinFolders = [];   // #64: folder list from /api/pins
let pinFolderSel = null; // #64: selected folder chip (null = all)

async function loadApps(){
  try {
    const [res, pr] = await Promise.all([
      fetch('/api/apps', { cache: 'no-store' }),
      fetch('/api/pins', { cache: 'no-store' }).catch(() => null),
    ]);
    const j = await res.json();
    appEntries = j.apps || [];
    if (pr && pr.ok) {
      const pj = await pr.json();
      pinnedIds = pj.pins || [];
      pinFolders = (pj.folders || []).filter(f => !f.virtual);   // "Unfiled" is computed client-side
    }
    renderFolderChips();
    renderChips();
    renderApps();
  } catch (_) {
    $('appBody').textContent = 'Could not reach the control API.';
  }
}

/* #64: folder chips over the pin board; "＋" creates a folder. */
function renderFolderChips(){
  const bar = $('pinFolders');
  bar.textContent = '';
  if (!pinFolders.length) { bar.hidden = true; return; }
  bar.hidden = false;
  const unfiled = pinnedIds.filter(id => !pinFolders.some(f => (f.apps || []).includes(id)));
  const mk = (label, sel, fn, count) => {
    const chip = document.createElement('button');
    chip.textContent = label + (count !== undefined ? ` · ${count}` : '');
    chip.classList.toggle('on', sel);
    chip.addEventListener('click', () => { if (chip._longFired) { chip._longFired = false; return; } fn(); });
    // long-press a real folder → delete it; its pins return to Unfiled (#64)
    let lp = null;
    chip.addEventListener('pointerdown', e => {
      if (!label.startsWith('🧷') && label !== 'Unfiled' && label !== '＋ Folder') {
        lp = setTimeout(() => {
          chip._longFired = true;
          const name = label.replace(/ · \d+$/, '');
          if (confirm(`Delete folder “${name}”? Its pins stay pinned under Unfiled.`)) {
            fetch('/api/pins/folder?name=' + encodeURIComponent(name), { method: 'DELETE' })
              .then(r => r.json())
              .then(j => { if (j.ok) { if (pinFolderSel === name) pinFolderSel = null; loadApps(); status('Folder deleted'); } })
              .catch(() => {});
          }
        }, 550);
      }
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(ev => chip.addEventListener(ev, () => clearTimeout(lp)));
    bar.appendChild(chip);
  };
  mk('🧷 All pins', pinFolderSel === null, () => { pinFolderSel = null; renderFolderChips(); renderApps(); }, pinnedIds.length);
  mk('Unfiled', pinFolderSel === '', () => { pinFolderSel = ''; renderFolderChips(); renderApps(); }, unfiled.length);
  pinFolders.forEach(f => mk(f.name, pinFolderSel === f.name,
    () => { pinFolderSel = f.name; renderFolderChips(); renderApps(); }, (f.apps || []).length));
  mk('＋ Folder', false, async () => {
    const name = prompt('New folder name:');
    if (!name || !name.trim()) return;
    try {
      const r = await fetch('/api/pins/folder', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim() }),
      });
      const j = await r.json();
      if (j.ok) { pinFolderSel = j.name || name.trim(); loadApps(); status('Folder created'); }
      else status('Folder rejected: ' + (j.error || '?'), true);
    } catch (_) { status('Folder failed', true); }
  });
}

/* #64: move one pinned app between folders (picker overlay). */
function pickFolder(appId){
  const box = $('folderPick'), listEl = $('folderPickList');
  $('folderPickTitle').textContent = 'Move pin to folder';
  listEl.textContent = '';
  const choose = async folder => {
    box.hidden = true;
    try {
      const r = await fetch('/api/pins/assign', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: appId, folder }),
      });
      const j = await r.json();
      if (j.ok) { pinnedIds = j.pins || pinnedIds; pinFolders = (j.folders || []).filter(f => !f.virtual); renderFolderChips(); renderApps(); status('Pin moved'); }
      else status('Move failed: ' + (j.error || '?'), true);
    } catch (_) { status('Move failed', true); }
  };
  const unpin = document.createElement('button');
  unpin.textContent = '📌 Unpin';
  unpin.addEventListener('click', () => { box.hidden = true; togglePin({ id: appId, name: appId }); });
  listEl.appendChild(unpin);
  const unfiled = document.createElement('button');
  unfiled.textContent = 'No folder (Unfiled)';
  unfiled.addEventListener('click', () => choose(''));
  listEl.appendChild(unfiled);
  for (const f of pinFolders) {
    const btn = document.createElement('button');
    btn.textContent = f.name;
    btn.addEventListener('click', () => choose(f.name));
    listEl.appendChild(btn);
  }
  const mk = document.createElement('button');
  mk.textContent = '＋ New folder…';
  mk.addEventListener('click', () => {
    const name = prompt('New folder name:');
    if (name && name.trim()) choose(name.trim());
  });
  listEl.appendChild(mk);
  box.hidden = false;
}
$('folderPickCancel').addEventListener('click', () => { $('folderPick').hidden = true; });
$('folderPick').addEventListener('click', e => { if (e.target.id === 'folderPick') $('folderPick').hidden = true; });

async function togglePin(a){
  try {
    const r = await fetch('/api/pins/toggle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: a.id }),
    });
    const j = await r.json();
    if (j.ok) {
      pinnedIds = j.pins || [];
      navigator.vibrate && navigator.vibrate(30);
      status((pinnedIds.includes(a.id) ? '📌 Pinned ' : 'Unpinned ') + a.name);
      renderApps();
    }
  } catch (_) { status('Pin failed', true); }
}

function renderChips(){
  const bar = $('appChips');
  bar.textContent = '';
  const counts = {};
  appEntries.forEach(a => { const b = bucketOf(a); counts[b] = (counts[b] || 0) + 1; });
  const order = ['All', ...CATS.map(c => c[1]).filter(l => counts[l]), 'Other'].filter((v, i, a) => a.indexOf(v) === i);
  for (const label of order) {
    const b = document.createElement('button');
    b.textContent = label === 'All' ? `All · ${appEntries.length}` : `${label} · ${counts[label]}`;
    b.classList.toggle('on', label === appBucket);
    b.addEventListener('click', () => { appBucket = label; renderChips(); renderApps(); });
    bar.appendChild(b);
  }
}

const TILecolors = ['#e91e63', '#3f51b5', '#009688', '#673ab7', '#ff5722', '#4caf50', '#607d8b', '#f44336', '#ff9800'];
function appTile(a){
  // icon img with graceful fallback to a colored letter tile
  const wrap = document.createElement('div');
  wrap.className = 'tilewrap';
  const letter = document.createElement('div');
  letter.className = 'tile';
  const h = [...a.name].reduce((s, c) => (s * 31 + c.charCodeAt(0)) >>> 0, 7);
  letter.style.background = TILecolors[h % TILecolors.length];
  letter.textContent = (a.name || '?').trim().charAt(0).toUpperCase();
  wrap.appendChild(letter);
  const img = document.createElement('img');
  img.className = 'appicon';
  img.alt = '';
  img.loading = 'lazy';
  img.src = `/icon/${encodeURIComponent(a.id)}`;
  img.addEventListener('error', () => img.remove());   // keep letter tile
  img.addEventListener('load', () => letter.remove());
  wrap.appendChild(img);
  return wrap;
}

function appButton(a, opts){
  opts = opts || {};
  const b = document.createElement('button');
  b.appendChild(appTile(a));
  const nm = document.createElement('span');
  nm.textContent = (isWebApp(a) ? '🌐 ' : '') + (a.source === 'custom' ? '🛠️ ' : '') + a.name;
  const sm = document.createElement('small');
  sm.textContent = (isWebApp(a) && !a.desc) ? 'web app' : (a.desc || a.id).slice(0, 42);
  b.appendChild(nm); b.appendChild(sm);
  if (pinnedIds.includes(a.id)) {
    const pin = document.createElement('span');
    pin.textContent = '📌';
    pin.style.cssText = 'position:absolute;top:2px;right:4px;font-size:10px';
    b.style.position = 'relative';
    b.appendChild(pin);
  }
  // long-press (550ms): unpinned → pin; already pinned → folder picker (#64).
  // Suppress the click that follows.
  let lpTimer = null, longFired = false;
  b.addEventListener('pointerdown', () => {
    longFired = false;
    lpTimer = setTimeout(() => {
      longFired = true;
      if (pinnedIds.includes(a.id)) pickFolder(a.id);
      else togglePin(a);
    }, 550);
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(ev =>
    b.addEventListener(ev, () => clearTimeout(lpTimer)));
  b.addEventListener('contextmenu', e => e.preventDefault());
  b.addEventListener('click', async () => {
    if (longFired) { longFired = false; return; }
    try {
      const r = await fetch('/api/launch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: a.id }),
      });
      const j = await r.json();
      if (!j.ok) { launchFail(j, 'Failed: ' + (j.error || '?')); return; }  // #75 toast w/ remediation
      status(j.focused ? 'Focused ' + a.name : 'Opening ' + a.name);
      closeSheets();
    } catch (_) { status('Open failed', true); }
  });
  if (opts.arrows) {   // #64: reorder inside the selected folder
    const up = document.createElement('span');
    up.textContent = '↑'; up.title = 'Move up in folder';
    up.setAttribute('role', 'button'); up.tabIndex = 0;
    up.setAttribute('aria-label', 'Move ' + a.name + ' up in folder');
    up.style.cssText = 'position:absolute;bottom:4px;right:4px;font-size:12px;color:var(--muted);padding:2px 6px';
    up.addEventListener('click', e => { e.stopPropagation(); reorderFolderPin(a, -1); });
    const down = up.cloneNode(true);
    down.textContent = '↓'; down.title = 'Move down in folder';
    down.setAttribute('aria-label', 'Move ' + a.name + ' down in folder');
    down.addEventListener('click', e => { e.stopPropagation(); reorderFolderPin(a, 1); });
    b.appendChild(up); b.appendChild(down);
  }
  return b;
}

function renderApps(){
  const body = $('appBody');
  body.textContent = '';
  const filter = $('appSearch').value.trim().toLowerCase();
  const matches = a => (a.name + ' ' + (a.desc || '') + ' ' + a.id).toLowerCase().includes(filter);
  const groups = {};
  for (const a of appEntries) {
    if (filter && !matches(a)) continue;
    const bucket = filter ? bucketOf(a) : (appBucket === 'All' || bucketOf(a) === appBucket ? bucketOf(a) : null);
    if (!bucket && !filter) continue;
    (groups[bucket] = groups[bucket] || []).push(a);
  }
  // pinned apps surface in their own group on top (excluded from their category)
  const showPinned = !filter;
  const pinSet = new Set(pinnedIds);
  if (showPinned) {
    for (const key of Object.keys(groups)) {
      groups[key] = groups[key].filter(a => !pinSet.has(a.id));
    }
  }
  // #64: the selected folder narrows the pinned group
  let pinnedList = pinnedIds;
  if (pinFolderSel === '') pinnedList = pinnedIds.filter(id => !pinFolders.some(f => (f.apps || []).includes(id)));
  else if (pinFolderSel !== null) {
    const f = pinFolders.find(x => x.name === pinFolderSel);
    pinnedList = f ? pinnedIds.filter(id => (f.apps || []).includes(id)) : [];
  }
  const pinnedApps = showPinned
    ? pinnedList.map(id => appEntries.find(a => a.id === id))
        .filter(a => a && (appBucket === 'All' || bucketOf(a) === appBucket))
    : [];
  // #62: the 6 most recently launched apps, straight from the server's
  // recency flag — shown above everything, never hiding search matches
  const recentApps = showPinned && appBucket === 'All' && pinFolderSel === null
    ? appEntries.filter(a => a.recent && !pinSet.has(a.id)).slice(0, 6)
    : [];
  const keys = Object.keys(groups).sort((a, b) => {
    const order = ['Internet', 'Chat', 'Music & Video', 'Work', 'Graphics', 'Utilities', 'System', 'Dev', 'Games', 'Learning', 'Other'];
    return order.indexOf(a) - order.indexOf(b);
  });
  if (!keys.length && !pinnedApps.length && !recentApps.length) {
    body.innerHTML = '<small style="color:#777">No apps match.</small>';
    return;
  }
  const emitGroup = (title, list, opts) => {
    const head = document.createElement('div');
    head.className = 'grphead';
    head.textContent = (filter ? `Matches in ${title}` : title);
    body.appendChild(head);
    const grid = document.createElement('div');
    grid.className = 'appgrid';
    for (const a of list) grid.appendChild(appButton(a, opts));
    body.appendChild(grid);
  };
  if (pinnedApps.length) emitGroup('📌 Pinned' + (pinFolderSel === '' ? ' — Unfiled' : pinFolderSel ? ' — ' + pinFolderSel : ''), pinnedApps, { arrows: showPinned && pinFolderSel !== null });
  if (recentApps.length) emitGroup('🕐 Recent', recentApps);
  for (const key of keys) emitGroup(key, groups[key]);
}

/* #64: reorder inside the selected folder; the global pin order keeps
   every other pin at its position. */
async function reorderFolderPin(a, dir){
  const folder = pinFolderSel === '' ? null : pinFolders.find(f => f.name === pinFolderSel);
  const memberIds = folder
    ? pinnedIds.filter(id => (folder.apps || []).includes(id))
    : pinnedIds.filter(id => !pinFolders.some(f => (f.apps || []).includes(id)));
  const i = memberIds.indexOf(a.id), j = i + dir;
  if (i < 0 || j < 0 || j >= memberIds.length) return;
  [memberIds[i], memberIds[j]] = [memberIds[j], memberIds[i]];
  const sub = memberIds.slice();
  const newOrder = pinnedIds.map(id => (sub.includes(id) ? sub.shift() : id));
  sub.filter(x => !newOrder.includes(x)).forEach(x => newOrder.push(x));
  try {
    const r = await fetch('/api/pins/reorder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: newOrder }),
    });
    const jr = await r.json();
    if (jr.ok) { pinnedIds = jr.pins || newOrder; renderApps(); }
    else status('Reorder failed', true);
  } catch (_) { status('Reorder failed', true); }
}

$('appSearch').addEventListener('input', () => renderApps());
$('fitBtn').addEventListener('click', async () => {
  try {
    const r = await fetch('/api/fit', { method: 'POST' });
    const j = await r.json();
    status(j.ok ? `Fitted ${j.windows} window(s)` : 'Fit failed', !j.ok);
  } catch (_) { status('Fit failed', true); }
});

/* #62: clear the launch history — pins and apps stay untouched. */
$('clearHistBtn').addEventListener('click', async () => {
  try {
    await fetch('/api/launches/clear', { method: 'POST' });
    loadApps();
    status('Launch recents cleared (pins and apps untouched)');
  } catch (_) { status('Clear failed', true); }
});

/* ------------------------------------------------------------------ *
 *  Custom app editor (#63): user-defined entries with argv-vector     *
 *  commands — the server validates and never touches a shell. Also    *
 *  lists existing custom entries with delete.                         *
 * ------------------------------------------------------------------ */
async function renderCustomList(){
  const box = $('eaList');
  box.textContent = '';
  try {
    const j = await (await fetch('/api/custom-apps', { cache: 'no-store' })).json();
    for (const app of (j.apps || [])) {
      const row = document.createElement('div');
      row.className = 'strow';
      const nm = document.createElement('div');
      nm.className = 'nm';
      const b = document.createElement('b'); b.textContent = '🛠️ ' + (app.label || app.name || app.id);
      const sm = document.createElement('small');
      sm.textContent = Array.isArray(app.cmd) ? app.cmd.join(' ') : String(app.cmd || '');
      sm.style.wordBreak = 'break-all';
      nm.appendChild(b); nm.appendChild(sm);
      const del = document.createElement('button');
      del.textContent = '✕ Delete';
      del.setAttribute('aria-label', 'Delete custom app ' + (app.label || app.id));
      del.addEventListener('click', async () => {
        if (!confirm('Delete custom app “' + (app.label || app.id) + '”?')) return;
        try {
          const r2 = await fetch('/api/custom-apps?id=' + encodeURIComponent(app.id), { method: 'DELETE' });
          const j2 = await r2.json();
          status(j2.ok ? 'Custom app deleted' : 'Delete failed', !j2.ok);
          renderCustomList();
          loadApps();
        } catch (_) { status('Delete failed', true); }
      });
      row.appendChild(nm); row.appendChild(del);
      box.appendChild(row);
    }
  } catch (_) { /* editor still usable for creating */ }
}
$('addAppBtn').addEventListener('click', () => {
  const ed = $('appEditor');
  ed.hidden = !ed.hidden;
  if (!ed.hidden) { renderCustomList(); $('eaName').focus(); }
});
$('eaCancel').addEventListener('click', () => { $('appEditor').hidden = true; });
$('eaSave').addEventListener('click', async () => {
  const label = $('eaName').value.trim();
  const cmd = $('eaCmd').value.trim().split(/\s+/).filter(Boolean);
  if (!label || !cmd.length) { status('Name and command are required', true); return; }
  try {
    const r = await fetch('/api/custom-apps', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, cmd, wmclass: $('eaWm').value.trim() }),
    });
    const j = await r.json();
    if (!j.ok) { status('Rejected: ' + (j.error || '?'), true); return; }
    $('eaName').value = ''; $('eaCmd').value = ''; $('eaWm').value = '';
    $('appEditor').hidden = true;
    loadApps();
    status('Custom app saved — it launches as a plain argv vector (no shell)');
  } catch (_) { status('Save failed', true); }
});

/* ------------------------------------------------------------------ *
 *  Window switcher (#59): live thumbnails + WM actions (#60) and      *
 *  layout slots (#61). Thumbnails are single-frame x11grab JPEGs      *
 *  fetched one at a time (throttled) and cached per geometry, so a    *
 *  slow host never stalls the desktop — capture failure degrades to  *
 *  a letter tile.                                                     *
 * ------------------------------------------------------------------ */
const WIN_SLOTS = ['left', 'right', 'top', 'bottom', 'tl', 'tr', 'bl', 'br',
                   'left-third', 'center-third', 'right-third', 'full'];
const WIN_SLOT_LABEL = { left: 'L½', right: 'R½', top: 'T½', bottom: 'B½', tl: '⌜', tr: '⌝', bl: '⌞', br: '⌟',
                         'left-third': '⅓L', 'center-third': '⅓C', 'right-third': '⅓R', full: 'Full' };
let winThumbs = new Map();  // hid -> {key, url} (bounded, revoked on evict)
let winSeq = 0;             // bump to cancel in-flight thumbnail fills
let winDisp = null;         // display geometry from /api/windows

async function winAction(hid, action, extra, okMsg){
  try {
    const r = await fetch('/api/window/action', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ hid, action }, extra || {})),
    });
    const j = await r.json();
    if (!j.ok) { toast(j.error || 'Window action failed'); status(j.error || 'Window action failed', true); return false; }
    if (okMsg) status(okMsg);
    return true;
  } catch (_) { status('Window action failed', true); return false; }
}

function winThumbTile(w){
  const wrap = document.createElement('div');
  wrap.style.cssText = 'position:relative;width:100%;aspect-ratio:16/9;background:var(--card2);border-radius:6px;overflow:hidden';
  const cached = winThumbs.get(w.id);
  if (cached) {
    const img = document.createElement('img');
    img.src = cached.url; img.alt = ''; img.decoding = 'async';
    img.dataset.hid = w.id;
    wrap.appendChild(img);
  } else {
    const letter = document.createElement('div');
    letter.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:22px;color:var(--faint)';
    letter.textContent = (w.name || '?').trim().charAt(0).toUpperCase() || '□';
    wrap.appendChild(letter);
  }
  return wrap;
}

function winCard(w){
  const b = document.createElement('button');
  b.dataset.wid = w.id;
  if (!w.visible) b.classList.add('hid');
  b.appendChild(winThumbTile(w));
  const nm = document.createElement('span');
  nm.className = 'wname'; nm.textContent = w.name || ('window ' + w.id);
  const geo = document.createElement('span');
  geo.className = 'wgeo';
  geo.textContent = `${w.w}×${w.h} @ ${w.x},${w.y}` + (w.visible ? '' : ' · hidden');
  b.appendChild(nm); b.appendChild(geo);
  b.title = 'Tap to focus · long-press or ⋯ for actions';
  b.addEventListener('click', async () => {
    if (b.dataset.actions === '1') { b.dataset.actions = ''; b.querySelector('.winacts')?.remove(); return; }
    if (await winAction(w.id, 'focus', null, 'Focused ' + (w.name || 'window'))) closeSheets();
  });
  // long-press or the ⋯ button opens the inline actions row (#60/#61);
  // both paths work for touch and keyboard users
  const open = () => { b.dataset.actions = '1'; b.querySelector('.winacts')?.remove(); b.appendChild(winActionsRow(w, b)); };
  let lp = null, fired = false;
  b.addEventListener('pointerdown', () => { fired = false; lp = setTimeout(() => { fired = true; open(); }, 550); });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(ev => b.addEventListener(ev, () => clearTimeout(lp)));
  b.addEventListener('contextmenu', e => e.preventDefault());
  const dots = document.createElement('span');
  dots.textContent = '⋯'; dots.setAttribute('role', 'button');
  dots.setAttribute('aria-label', 'Window actions for ' + (w.name || w.id));
  dots.tabIndex = 0;
  dots.style.cssText = 'position:absolute;top:2px;right:6px;font-size:14px;color:var(--muted)';
  dots.addEventListener('click', e => { e.stopPropagation(); open(); });
  dots.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); e.preventDefault(); open(); } });
  b.style.position = 'relative';
  b.appendChild(dots);
  return b;
}

function winActionsRow(w, card){
  const row = document.createElement('div');
  row.className = 'winacts';
  const mk = (txt, fn, cls) => {
    const btn = document.createElement('button');
    btn.textContent = txt;
    if (cls) btn.className = cls;
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      if (await fn()) { row.remove(); card.dataset.actions = ''; loadWindows(); }
    });
    return btn;
  };
  row.appendChild(mk('Focus', () => winAction(w.id, 'focus', null, 'Focused').then(ok => { if (ok) closeSheets(); return false; })));
  row.appendChild(mk('Min', () => winAction(w.id, 'minimize', null, 'Minimized')));
  row.appendChild(mk('Max', () => winAction(w.id, 'maximize', null, 'Maximized')));
  row.appendChild(mk('Unmax', () => winAction(w.id, 'restore', null, 'Restored')));
  row.appendChild(mk('Center', () => {
    const dw = (winDisp && winDisp.width) || 1280, dh = (winDisp && winDisp.height) || 800;
    const ww = Math.min(parseInt(w.w || dw, 10) || dw, dw), hh = Math.min(parseInt(w.h || dh, 10) || dh, dh);
    return winAction(w.id, 'move', { x: Math.max(0, Math.round((dw - ww) / 2)), y: Math.max(0, Math.round((dh - hh) / 2)) }, 'Centered');
  }));
  row.appendChild(mk('Close ✕', async () => {
    // destructive: confirm first (#60) — unsaved work in remote apps is
    // invisible from here, so we never close without asking
    if (!confirm('Close “' + (w.name || 'window') + '”?\nUnsaved work in the app is lost.')) return false;
    return winAction(w.id, 'close', null, 'Closed');
  }, 'warn'));
  for (const slot of WIN_SLOTS) {
    row.appendChild(mk(WIN_SLOT_LABEL[slot], () => winAction(w.id, 'layout', { slot }, 'Placed ' + (WIN_SLOT_LABEL[slot] || slot))));
  }
  return row;
}

async function loadWindows(){
  const list = $('winList');
  const seq = ++winSeq;
  try {
    const r = await fetch('/api/windows', { cache: 'no-store' });
    const j = await r.json();
    if (seq !== winSeq) return;
    if (!j.ok) { list.innerHTML = '<small style="color:#777"></small>'; list.firstChild.textContent = j.error || 'No window manager reported.'; return; }
    winDisp = j.display || null;
    $('winsWm').textContent = j.wm ? ('WM: ' + j.wm) : 'no EWMH window manager detected';
    const wins = j.windows || [];
    if (!wins.length) { list.innerHTML = '<small style="color:#777">No windows open. Launch an app to get started.</small>'; return; }
    list.textContent = '';
    const grid = document.createElement('div');
    grid.className = 'wingrid';
    wins.forEach(w => grid.appendChild(winCard(w)));
    list.appendChild(grid);
    fillThumbs(wins, seq);
  } catch (_) { list.innerHTML = '<small style="color:#777">Could not reach the control API.</small>'; }
}

async function fillThumbs(wins, seq){
  // one grab at a time with a gap: x11grab on a busy X server must never
  // queue up (#59) — the sheet stays interactive while this trickles in
  if (audioOnlyOn()) return;   // #98: no thumbnails in audio-only mode
  for (const w of wins) {
    if (seq !== winSeq) return;
    const key = [w.x, w.y, w.w, w.h].join(',');
    const have = winThumbs.get(w.id);
    if (!have || have.key !== key) {
      try {
        const r = await fetch('/api/window/thumbnail?hid=' + encodeURIComponent(w.id), { cache: 'no-store' });
        if (r.ok && seq === winSeq) {
          const blob = await r.blob();
          if (seq !== winSeq) return;
          const url = URL.createObjectURL(blob);
          if (have) URL.revokeObjectURL(have.url);
          winThumbs.set(w.id, { key, url });
          if (winThumbs.size > 24) {  // bounded cache
            const oldest = winThumbs.keys().next().value;
            const old = winThumbs.get(oldest);
            if (old) URL.revokeObjectURL(old.url);
            winThumbs.delete(oldest);
          }
          const card = document.querySelector('.wingrid button[data-wid="' + CSS.escape(w.id) + '"]');
          const wrap = card && card.firstElementChild;
          if (wrap) {
            const img = document.createElement('img');
            img.src = url; img.alt = ''; img.decoding = 'async';
            img.addEventListener('load', () => { const old = wrap.querySelector('div'); if (old) old.remove(); });
            img.addEventListener('error', () => img.remove());  // degrade to the letter tile
            wrap.appendChild(img);
          }
        }
      } catch (_) { /* degrade to the letter tile */ }
      await new Promise(res => setTimeout(res, 350));
    }
  }
}

$('bWins').addEventListener('click', () => { openSheet($('shWins')); loadWindows(); });
$('winsRefresh').addEventListener('click', loadWindows);

/* ------------------------------------------------------------------ *
 *  App store                                                          *
 * ------------------------------------------------------------------ */
const storePolls = {};   // pkg -> interval id
let storeCaps = { search: true, install: true, remove: true, update: true, status: true };
let storeCols = [];      // curated collections (#69)
let storeColActive = null;

/* #67: the detected backend's capabilities decide which buttons exist —
   unsupported operations are hidden, never dead. */
async function loadStoreBackend(){
  try {
    const j = await (await fetch('/api/store/backend', { cache: 'no-store' })).json();
    storeCaps = j.capabilities || storeCaps;
    const row = $('storeBackendRow'), msg = $('storeBackendMsg');
    if (!j.supported) {
      row.hidden = false;
      msg.textContent = j.backend === 'none'
        ? 'No supported package manager found on the desktop — the store is read-only.'
        : (j.error || 'Package operations are not supported here.') + ' Install, update and remove are hidden.';
    } else {
      row.hidden = true;
    }
  } catch (_) { /* keep permissive defaults */ }
}

function storeRow(app){
  const row = document.createElement('div');
  row.className = 'strow';
  const nm = document.createElement('div');
  nm.className = 'nm';
  nm.innerHTML = `<b></b><small></small>`;
  nm.querySelector('b').textContent = app.label || app.name;
  nm.querySelector('small').textContent = app.desc || app.pkg || '';
  const box = document.createElement('div');
  box.style.cssText = 'display:flex;gap:6px;flex:0 0 auto';
  row.appendChild(nm); row.appendChild(box);
  paintStoreBtns(box, app);
  return row;
}

/* One row paints ALL of its operations (#66): Open / Update / Remove for
   installed packages, Install otherwise — plus a live Cancel while a job
   is queued or running. Buttons only exist when the backend supports them
   (#67). Every box for the same package repaints together. */
function paintStoreBtns(box, app){
  const pkg = app.pkg || app.name;
  box.textContent = '';
  box.dataset.pkg = pkg;
  box._app = app;
  const mk = (txt, primary, fn, title) => {
    const btn = document.createElement('button');
    if (primary) btn.className = 'primary';
    btn.textContent = txt;
    if (title) btn.title = title;
    btn.addEventListener('click', fn);
    box.appendChild(btn);
    return btn;
  };
  const poll = storePolls[pkg];
  if (poll && poll.iv) {   // one job per package: show progress + cancel
    mk('✕ Cancel', false, () => cancelPkg(pkg), 'Cancel this ' + (poll.op || 'operation'));
    return;
  }
  if (app.installed) {
    mk('Open', true, () => launchPkg(pkg, app.label || app.name));
    if (storeCaps.update) mk('Update', false, () => storeOp('update', pkg), 'Upgrade this package');
    if (storeCaps.remove) mk('Remove', false, () => storeOp('remove', pkg), 'Uninstall');  // #121: confirm dialog in storeOp
  } else if (storeCaps.install) {
    mk('Install', true, () => storeOp('install', pkg));
  } else {
    mk('n/a', false, e => e.preventDefault(), 'Not supported by the detected backend');
    box.querySelector('button').disabled = true;
  }
}

function repaintStoreRows(pkg, installed){
  document.querySelectorAll('#storeList .strow').forEach(row => {
    const box = row.querySelector('[data-pkg="' + CSS.escape(pkg) + '"]');
    if (box && box._app) {
      if (installed !== undefined) box._app.installed = installed;
      paintStoreBtns(box, box._app);
    }
  });
}

async function storeOp(op, pkg){
  const verb = { install: 'Install', remove: 'Remove', update: 'Update' }[op] || op;
  try {
    const r = await fetch('/api/store/' + op, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pkg }),
    });
    const j = await r.json();
    if (!j.ok) { status(verb + ' failed: ' + (j.error || '?'), true); return; }
    storePolls[pkg] = { op };                    // row switches to Cancel
    repaintStoreRows(pkg);
    status(verb + ' queued for ' + pkg);
    pollStoreStatus(pkg, op);
  } catch (_) { status(verb + ' request failed', true); }
}

async function cancelPkg(pkg){
  try {
    const r = await fetch('/api/store/cancel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pkg }),
    });
    const j = await r.json();
    status(j.ok ? 'Cancelling ' + pkg + '…' : 'Cancel failed: ' + (j.error || '?'), !j.ok);
  } catch (_) { status('Cancel failed', true); }
}

function pollStoreStatus(pkg, op){
  const poll = storePolls[pkg] || (storePolls[pkg] = { op });
  clearInterval(poll.iv);
  let tries = 0;
  poll.iv = setInterval(async () => {
    tries++;
    try {
      const r = await fetch('/api/store/status?pkg=' + encodeURIComponent(pkg), { cache: 'no-store' });
      const j = await r.json();
      const st = j.state || '';
      if (st === 'queued') {   // queue display (#66)
        status(pkg + ': waiting in queue' + (j.queue_pos ? ' (position ' + j.queue_pos + ')' : '') + ' — one operation runs at a time');
        return;
      }
      if (st === 'running') {
        const last = (j.log || []).filter(Boolean).slice(-1)[0];
        status(pkg + ': ' + op + '…' + (last ? ' — ' + last.slice(0, 80) : ''));
        return;
      }
      clearInterval(poll.iv); delete storePolls[pkg];
      if (st === 'cancelled') status(pkg + ': cancelled');
      else if (st === 'done' || j.installed) status(pkg + ': ' + op + ' finished ✓');
      else if (st === 'interrupted') status(pkg + ': interrupted by a server restart — retry when ready', true);
      else if (st === 'failed') status(pkg + ': ' + op + ' failed — see the log on the desktop (/tmp/remote-install.log)', true);
      repaintStoreRows(pkg, j.installed);
    } catch (_) {
      if (tries > 40) { clearInterval(poll.iv); delete storePolls[pkg]; repaintStoreRows(pkg); }
    }
  }, 3000);
}

function launchPkg(pkg, label){
  fetch('/api/store/launch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: pkg }),
  }).then(r => r.json()).then(j => {
    if (j.ok) { status('Opening ' + (label || pkg)); closeSheets(); }
    else launchFail(j, 'Could not open ' + pkg);   // #75
  }).catch(() => status('Launch failed', true));
}

async function storeSearch(q){
  const list = $('storeList');
  list.innerHTML = '<div class="strow"><div class="nm"><small>Searching…</small></div></div>';
  try {
    const r = await fetch('/api/store/search?q=' + encodeURIComponent(q), { cache: 'no-store' });
    const j = await r.json();
    renderStore((j.apps || []), q);
  } catch (_) {
    list.innerHTML = '<div class="strow"><div class="nm"><small>Search failed.</small></div></div>';
  }
}

function renderStore(apps, q){
  const list = $('storeList');
  list.textContent = '';
  $('shStore').dataset.loaded = '1';
  if (!apps.length) {
    list.innerHTML = '<div class="strow"><div class="nm"><small>No results' + (q ? ' for “' + q + '”' : '') + '.</small></div></div>';
    return;
  }
  apps.forEach(a => list.appendChild(storeRow(a)));
}

/* #69: curated collections as chips — the catalog ships with the server,
   so it renders with no network access and never auto-installs. */
function renderCollections(){
  const list = $('storeList');
  const chips = $('storeChips');
  list.textContent = '';
  $('shStore').dataset.loaded = '1';
  if (!storeCols.length) { chips.hidden = true; return false; }
  chips.hidden = false;
  chips.textContent = '';
  const mk = (label, id) => {
    const chip = document.createElement('button');
    chip.textContent = label;
    chip.classList.toggle('on', storeColActive === id);
    chip.addEventListener('click', () => { storeColActive = storeColActive === id ? null : id; renderCollections(); });
    chips.appendChild(chip);
  };
  mk('All', null);
  storeCols.forEach(c => mk(c.label || c.id, c.id));
  for (const c of storeCols) {
    if (storeColActive && c.id !== storeColActive) continue;
    const head = document.createElement('div');
    head.className = 'grphead';
    head.textContent = c.label || c.id;
    list.appendChild(head);
    (c.apps || []).forEach(a => list.appendChild(storeRow(Object.assign({ desc: a.desc || '' }, a, { name: a.label || a.pkg, pkg: a.pkg }))));
  }
  return true;
}

async function storeDefault(){
  // curated collections first (#69), legacy catalog as the fallback
  try {
    const j = await (await fetch('/api/store/collections', { cache: 'no-store' })).json();
    storeCols = j.collections || [];
    if (renderCollections()) return;
  } catch (_) { /* fall through to the legacy catalog */ }
  try {
    const r = await fetch('/api/store', { cache: 'no-store' });
    const j = await r.json();
    renderStore(j.apps || []);
  } catch (_) { renderStore([]); }
}

$('storeGo').addEventListener('click', () => { const q = $('storeQ').value.trim(); if (q) storeSearch(q); else storeDefault(); });
$('storeQ').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); const q = $('storeQ').value.trim(); if (q) storeSearch(q); else storeDefault(); }
});

/* ------------------------------------------------------------------ *
 *  Display settings                                                   *
 * ------------------------------------------------------------------ */
$('matchBtn').addEventListener('click', () => { applyInsets(); matchDesktop(true); closeSheets(); });

/* Per-app geometry remembering (#76): opt-out toggle + reset-to-fit. */
$('geoChk').addEventListener('change', async e => {
  try {
    await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: 'geo_remember', value: e.target.checked }),
    });
    status(e.target.checked ? 'Windows will reopen at their remembered size' : 'Geometry remembering off');
  } catch (_) { status('Could not save the setting', true); }
});
$('geoReset').addEventListener('click', async () => {
  try {
    const r = await fetch('/api/geometry', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reset: true }),
    });
    const j = await r.json();
    $('geoNote').textContent = 'Remembered geometries cleared (' + (j.apps || 0) + ' remaining). Windows fall back to fit.';
    status('Remembered window geometry cleared');
  } catch (_) { status('Reset failed', true); }
});
$('autoCrop').addEventListener('click', () => {
  store.del('nav'); store.del('side'); store.del('dw');
  $('side').value = 'auto'; $('dw').value = '0';
  applyInsets(); matchDesktop(true); status('Edges re-detected');
});
function stepNav(d){
  const cur = store.get('nav', null);
  const curIns = computeInsets().ins;
  const base = cur !== null ? parseInt(cur, 10)
    : (curIns[isLandscape() ? (landscapeSide() === 'right' ? 'r' : 'l') : 'b'] || 0);
  store.set('nav', Math.max(0, Math.min(120, base + d)));
  relayout();
}
$('navMinus').addEventListener('click', () => stepNav(-4));
$('navPlus').addEventListener('click', () => stepNav(4));
$('side').value = store.get('side', 'auto');
$('side').addEventListener('change', e => { store.set('side', e.target.value); relayout(); });
$('dw').value = store.get('dw', '0');
$('dw').addEventListener('change', e => { store.set('dw', e.target.value); matchDesktop(true); });
$('quality').value = store.get('quality', '6');
$('qualityVal').textContent = $('quality').value;
$('quality').addEventListener('input', e => {
  $('qualityVal').textContent = e.target.value;
  if (rfb) rfb.qualityLevel = parseInt(e.target.value, 10);
  store.set('quality', e.target.value);
});
$('clipboardBtn').addEventListener('click', async () => {
  try { rfb.clipboardPasteFrom(await navigator.clipboard.readText()); status('Pasted to remote'); }
  catch (_) { status('Clipboard blocked', true); }
});
/* Clipboard buttons: go through the X selections server-side so BOTH the
   CLIPBOARD (Ctrl+V apps) and PRIMARY (terminals, middle-click) are set. */
$('copyFromBtn').addEventListener('click', async () => {
  try {
    const r = await fetch('/api/clipboard', { cache: 'no-store' });
    const j = await r.json();
    const text = j.text || '';
    if (!text) { status('Desktop clipboard is empty', true); return; }
    try { await navigator.clipboard.writeText(text); status('Copied ' + text.length + ' chars to phone'); }
    catch (_) {
      // browser blocked the write: fall back to a prompt the user can copy from
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.cssText = 'position:fixed;inset:10%;z-index:99;width:80%;height:40%;font-size:13px';
      document.body.appendChild(ta); ta.select();
      status('Select all + copy, then close');
      ta.addEventListener('blur', () => ta.remove());
    }
  } catch (_) { status('Clipboard read failed', true); }
});
$('pasteToBtn').addEventListener('click', async () => {
  let text = '';
  try { text = await navigator.clipboard.readText(); }
  catch (_) { status('Phone clipboard blocked – use Share → Copy instead', true); return; }
  if (!text) { status('Phone clipboard is empty', true); return; }
  try {
    await fetch('/api/clipboard/set', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (rfb) rfb.clipboardPasteFrom(text);   // also notify VNC viewers
    status('Pasted ' + text.length + ' chars to desktop');
  } catch (_) { status('Paste failed', true); }
});
$('typeBtn').addEventListener('click', async () => {
  let text = '';
  try { text = await navigator.clipboard.readText(); }
  catch (_) { status('Phone clipboard blocked', true); return; }
  if (!text) { status('Phone clipboard is empty', true); return; }
  try {
    const r = await fetch('/api/paste', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const j = await r.json();
    status(j.ok ? 'Typed ' + text.length + ' chars into the focused window' : 'Typing failed', !j.ok);
    closeSheets();
  } catch (_) { status('Typing failed', true); }
});

/* Clipboard history: unified list of everything copied on the remote desktop
   or pushed from the phone. Tap an entry to paste it to the desktop. */
function timeAgo(t){
  const s = Math.max(1, Math.floor(Date.now() / 1000 - t));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

let clipTagFilter = null;   // active tag chip (#70)
let clipTagsCache = [];

async function loadClipHistory(){
  const list = $('clipList');
  if (document.activeElement === $('clipQ')) return;  // never clobber typing
  try {
    const q = $('clipQ').value.trim();
    const p = new URLSearchParams();
    if (q) p.set('q', q);
    if (clipTagFilter) p.set('tag', clipTagFilter);
    const r = await fetch('/api/clipboard/history' + (p.toString() ? '?' + p : ''), { cache: 'no-store' });
    const j = await r.json();
    clipTagsCache = j.tags || [];
    paintClipTagChips();
    list.textContent = '';
    if (!j.items || !j.items.length) {
      list.innerHTML = '<div class="cliprow"><div class="cv"><small></small></div></div>';
      list.querySelector('small').textContent = (q || clipTagFilter)
        ? 'Nothing matches this search/filter.'
        : 'Nothing copied yet. Copy something on the desktop or phone.';
      return;
    }
    for (const it of j.items) {
      const row = document.createElement('div');
      row.className = 'cliprow';
      const cv = document.createElement('div');
      cv.className = 'cv';
      const b = document.createElement('b');
      if (it.kind === 'image') {
        if (it.preview_image) {   // inline thumbnail (#71)
          const img = document.createElement('img');
          img.className = 'clipimg'; img.alt = 'clipboard image'; img.decoding = 'async';
          img.src = it.preview_image;
          cv.appendChild(img);
        } else if (it.preview && it.preview !== '(image skipped — too large)') {
          // older item without an inline preview: lazy-load the full image
          const img = document.createElement('img');
          img.className = 'clipimg'; img.alt = 'clipboard image'; img.loading = 'lazy';
          img.src = '/api/clipboard/image?id=' + encodeURIComponent(it.id);
          img.addEventListener('error', () => img.remove());
          cv.appendChild(img);
        } else {
          b.textContent = it.preview || '(image)';
        }
      } else {
        b.textContent = (it.kind === 'uri' ? '📁 ' : '') + (it.preview || '(empty)');
      }
      const small = document.createElement('small');
      const kindTxt = it.kind === 'image' ? 'image' : it.kind === 'uri' ? 'file(s)' : (it.len + ' chars');
      small.textContent = `${kindTxt} · ${timeAgo(it.time)} · ${it.source === 'phone' ? 'phone' : 'desktop'}` + (it.pinned ? ' · 📌' : '');
      cv.appendChild(b);
      for (const t of (it.tags || [])) {   // tag chips ride along (#70)
        const chip = document.createElement('span');
        chip.className = 'tagchip'; chip.textContent = '#' + t;
        cv.appendChild(chip);
      }
      cv.appendChild(small);
      cv.addEventListener('click', async () => {         // tap = paste to desktop (images too, #71)
        try {
          const r2 = await fetch('/api/clipboard/paste', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: it.id }),
          });
          const j2 = await r2.json();
          status(j2.ok ? 'Pasted to desktop' : 'Paste failed: ' + (j2.error || '?'), !j2.ok);
          if (j2.ok) closeSheets();
        } catch (_) { status('Paste failed', true); }
      });
      const pin = document.createElement('button');      // pin/unpin (#70)
      pin.textContent = it.pinned ? '📌' : '📍';
      pin.title = it.pinned ? 'Unpin (returns to normal retention)' : 'Pin (exempt from history trimming)';
      pin.setAttribute('aria-label', pin.title);
      pin.addEventListener('click', async e => {
        e.stopPropagation();
        try {
          const r2 = await fetch('/api/clipboard/pin', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: it.id, pinned: !it.pinned }),
          });
          if (!r2.ok) { status('Pin failed', true); return; }
          navigator.vibrate && navigator.vibrate(20);
          loadClipHistory();
        } catch (_) { status('Pin failed', true); }
      });
      const tag = document.createElement('button');      // edit tags (#70)
      tag.textContent = '🏷';
      tag.title = 'Tags'; tag.setAttribute('aria-label', 'Edit tags for this clipboard item');
      tag.addEventListener('click', async e => {
        e.stopPropagation();
        const val = prompt('Tags for this item (comma separated):', (it.tags || []).join(', '));
        if (val === null) return;
        try {
          const r2 = await fetch('/api/clipboard/tag', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: it.id, tags: val }),
          });
          if (!r2.ok) { status('Tag failed', true); return; }
          loadClipHistory();
        } catch (_) { status('Tag failed', true); }
      });
      const toPhone = document.createElement('button');
      if (it.kind === 'image') {
        toPhone.textContent = '⬇';
        toPhone.title = 'Save image to phone';
        toPhone.setAttribute('aria-label', 'Save clipboard image to phone');
        toPhone.addEventListener('click', e => {
          e.stopPropagation();
          location.href = '/api/clipboard/image?id=' + encodeURIComponent(it.id);  // attachment download (#71)
        });
      } else {
        toPhone.textContent = '→📱';
        toPhone.title = 'Copy to phone';
        toPhone.setAttribute('aria-label', 'Copy history item to phone');
        toPhone.addEventListener('click', async e => {
          e.stopPropagation();
          try {
            const r2 = await fetch('/api/clipboard/item?id=' + it.id, { cache: 'no-store' });
            const j2 = await r2.json();
            await navigator.clipboard.writeText(j2.text);
            status('Copied to phone');
          } catch (_) { status('Phone clipboard write blocked', true); }
        });
      }
      const del = document.createElement('button');
      del.className = 'del';
      del.textContent = '×';
      del.setAttribute('aria-label', 'Delete history item');
      del.addEventListener('click', async e => {
        e.stopPropagation();
        try {
          await fetch('/api/clipboard/remove', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: it.id }),
          });
          loadClipHistory();
        } catch (_) {}
      });
      row.appendChild(cv);
      if (it.kind !== 'image') row.appendChild(typeBtnFor(it));   // paste-as-keystrokes (#74)
      row.appendChild(pin); row.appendChild(tag); row.appendChild(toPhone); row.appendChild(del);
      list.appendChild(row);
    }
  } catch (_) {
    list.innerHTML = '<div class="cliprow"><div class="cv"><small>History unavailable.</small></div></div>';
  }
}

function paintClipTagChips(){
  const bar = $('clipTags');
  bar.textContent = '';
  if (!clipTagsCache.length) { bar.hidden = true; return; }
  bar.hidden = false;
  const mk = (label, t) => {
    const chip = document.createElement('button');
    chip.textContent = label;
    chip.classList.toggle('on', (t || null) === clipTagFilter);
    chip.addEventListener('click', () => { clipTagFilter = (clipTagFilter === (t || null)) ? null : (t || null); loadClipHistory(); });
    bar.appendChild(chip);
  };
  mk('All', null);
  clipTagsCache.slice(0, 12).forEach(t => mk('#' + t, t));
}

/* #74: per-item "type instead of paste" — reuses the server's xdotool
   path, warns before large/multiline content, rate-limited client-side. */
let lastTypeAt = 0;
function typeBtnFor(it){
  const btn = document.createElement('button');
  btn.textContent = '⌨';
  btn.title = 'Type as keystrokes (fallback for apps that ignore the clipboard)';
  btn.setAttribute('aria-label', 'Type this item as keystrokes');
  btn.addEventListener('click', async e => {
    e.stopPropagation();
    if (Date.now() - lastTypeAt < 5000) { status('Typing fallback: wait a moment between attempts', true); return; }
    let text = it.preview || '';
    try {
      const r2 = await fetch('/api/clipboard/item?id=' + it.id, { cache: 'no-store' });
      const j2 = await r2.json();
      text = j2.text || text;
    } catch (_) {}
    if (!text) { status('Nothing to type', true); return; }
    if (text.length > 2000 || text.includes('\n')) {
      if (!confirm(`Type ${text.length} characters as keystrokes into the focused window?\nMultiline/large text is slow — use paste instead.`)) return;
    }
    lastTypeAt = Date.now();
    try {
      const r2 = await fetch('/api/paste', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, mode: 'keys' }),
      });
      const j2 = await r2.json();
      if (j2.ok) { status('Typed ' + text.length + ' chars'); closeSheets(); }
      else status('Typing refused: ' + (j2.error || 'failed'), true);
    } catch (_) { status('Typing failed', true); }
  });
  return btn;
}

$('clipRefresh').addEventListener('click', loadClipHistory);
let clipQDebounce = null;
$('clipQ').addEventListener('input', () => {
  clearTimeout(clipQDebounce);
  clipQDebounce = setTimeout(loadClipHistory, 300);
});
$('clipClear').addEventListener('click', async () => {
  try { await fetch('/api/clipboard/clear', { method: 'POST' }); loadClipHistory(); status('History cleared (pinned items kept)'); }
  catch (_) { status('Clear failed', true); }
});

// live refresh while the More sheet is open
let clipPoll = null;
const clipPollStart = () => { clipPollStop(); clipPoll = setInterval(loadClipHistory, 3000); };
const clipPollStop = () => { if (clipPoll) { clearInterval(clipPoll); clipPoll = null; } };

/* ------------------------------------------------------------------ *
 *  Phone sync (#72): opt-in desktop → phone clipboard push. Off by    *
 *  default, enabled from this toggle only, served ONLY while this     *
 *  page is visible (a poll tick is skipped when hidden — sync happens *
 *  on user action/focus, never silently in the background). Denied    *
 *  clipboard permission degrades to the manual Desktop → Phone flow.  *
 * ------------------------------------------------------------------ */
let syncCursor = null, syncTimer = null, syncKnownOn = false;
async function syncTick(){
  if (document.hidden) return;
  try {
    const r = await fetch('/api/clipboard/sync' + (syncCursor ? '?since=' + encodeURIComponent(syncCursor) : ''), { cache: 'no-store' });
    const j = await r.json();
    if (!j.enabled) { if (syncKnownOn) { $('clipSyncChk').checked = false; syncKnownOn = false; paintSyncNote(); } stopSyncPoll(); return; }
    for (const it of (j.items || [])) {
      syncCursor = it.id;
      try {
        await navigator.clipboard.writeText(it.text);
        toast('📋 ' + (it.text || '').slice(0, 60));
      } catch (_) {
        status('Clipboard permission denied — use Desktop → Phone instead', true);
        break;  // degrade to the manual flow, keep the cursor for later
      }
    }
  } catch (_) {}
}
function startSyncPoll(){ stopSyncPoll(); syncTick(); syncTimer = setInterval(syncTick, 5000); }
function stopSyncPoll(){ if (syncTimer) { clearInterval(syncTimer); syncTimer = null; } }
function paintSyncNote(){
  $('clipSyncNote').textContent = $('clipSyncChk').checked
    ? 'ON — new desktop copies reach this phone automatically while this page is visible. Nothing leaves the host; use the toggle to stop and clear the sync cursor.'
    : 'Off — nothing is synced. When on, new desktop copies appear on this phone automatically; the browser may ask for clipboard permission, and a denial falls back to the Desktop → Phone button.';
}
$('clipSyncChk').addEventListener('change', async e => {
  const on = e.target.checked;
  try {
    await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: 'clip_sync', value: on }),
    });
  } catch (_) { status('Could not save the sync setting', true); }
  if (on) { syncCursor = null; syncKnownOn = true; startSyncPoll(); }
  else { stopSyncPoll(); syncCursor = null; }   // one tap off + cursor cleared (#72)
  paintSyncNote();
  status(on ? 'Clipboard sync ON (while visible)' : 'Clipboard sync OFF — sync cache cleared');
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && syncKnownOn) syncTick();  // focus-triggered catch-up
});
// restore the opt-in toggles (#72/#76) on load; the sync poll restarts only
// because the user explicitly enabled it before — consent is persistent
(async () => {
  try {
    const j = await (await fetch('/api/settings', { cache: 'no-store' })).json();
    const s = j.settings || {};
    syncKnownOn = !!s.clip_sync;
    $('clipSyncChk').checked = syncKnownOn;
    if (syncKnownOn) startSyncPoll();
    paintSyncNote();
    $('geoChk').checked = s.geo_remember !== false;
  } catch (_) { paintSyncNote(); }
})();
$('logoutBtn').addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' }).catch(() => {});
  location.href = '/login';
});

// paste events: forward straight into the remote session
document.addEventListener('paste', async e => {
  try {
    let text = '';
    if (e.clipboardData && typeof e.clipboardData.getData === 'function')
      text = e.clipboardData.getData('text/plain') || e.clipboardData.getData('text');
    if (!text) { try { text = await navigator.clipboard.readText(); } catch(_) {} }
    if (text) {
      e.preventDefault();
      if (rfb) rfb.clipboardPasteFrom(text);
      // record phone-side copies in the shared clipboard history + X selections
      fetch('/api/clipboard/set', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      }).catch(() => {});
      status(`Pasted ${text.length} chars`);
    }
  } catch(_) {}
}, true);

// relayout on rotate / resize / fullscreen
['resize', 'orientationchange'].forEach(ev =>
  addEventListener(ev, () => { setTimeout(relayout, 50); setTimeout(relayout, 500); }));
if (screen.orientation) screen.orientation.addEventListener('change', () => { setTimeout(relayout, 60); setTimeout(relayout, 500); });
document.addEventListener('fullscreenchange', () => setTimeout(relayout, 200));
if (window.visualViewport) visualViewport.addEventListener('resize', relayout);

/* ------------------------------------------------------------------ *
 *  Files: shared roots, manager, resumable transfers (#78-#93)        *
 * ------------------------------------------------------------------ */
const humanSize = n => n >= (1 << 30) ? (n / (1 << 30)).toFixed(1) + ' GB'
  : n >= (1 << 20) ? (n / (1 << 20)).toFixed(1) + ' MB'
  : n >= 1024 ? (n / 1024).toFixed(0) + ' KB' : n + ' B';
const fmtWhen = ts => { const d = new Date(ts * 1000); return d.toLocaleDateString() + ' ' + d.toLocaleTimeString(); };

let fmRoot = store.get('fmRoot', '') || '';
let fmPath = '';
let fmEntries = [];
let fmSel = new Set();
let fmSelAnchor = null;
let fmSort = store.get('fmSort', 'name');
let fmDesc = store.get('fmDesc', '0') === '1';
let fmReadOnly = false;
let fmSearchAbort = null;

async function fmApi(path, opts) {
  const r = await fetch(path, Object.assign({ cache: 'no-store' }, opts || {}));
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || r.status), { status: r.status, payload: j });
  return j;
}

function fmSortLabel() {
  const keys = { name: 'files.sortName', size: 'files.sortSize', mtime: 'files.sortDate', type: 'files.sortType' };
  return i18nT(keys[fmSort] || 'files.sortName', fmSort) + (fmDesc ? ' ↓' : ' ↑');
}

async function loadFiles(){
  const box = $('fileList');
  try {
    const q = new URLSearchParams({ root: fmRoot, path: fmPath, sort: fmSort, dir: fmDesc ? 'desc' : 'asc' });
    const j = await fmApi('/api/files?' + q);
    fmRoot = j.root;
    fmReadOnly = !!j.readonly;
    store.set('fmRoot', fmRoot);
    $('fRoot').innerHTML = '';
    for (const r of j.roots || []) {
      const o = document.createElement('option');
      o.value = r.id; o.textContent = r.label + (r.readonly ? ' 🔒' : '');
      if (r.id === j.root) o.selected = true;
      $('fRoot').appendChild(o);
    }
    paintCrumbs(j.root_label || 'Files');
    paintDisk(j.disk);
    fmEntries = j.entries || [];
    if (fmSearchAbort) { fmSearchAbort = null; }
    fmPaint(box, fmEntries, true);
  } catch (e) {
    box.textContent = '';
    const err = document.createElement('small');
    err.style.color = '#777'; err.textContent = e.message || 'Could not list files.';
    box.appendChild(err);
  }
}

function paintDisk(disk){
  const row = $('fWarn');
  if (!disk) { row.hidden = true; return; }
  const total = disk.quota ? disk.quota : disk.total;
  const used = disk.quota ? (disk.quota_used || 0) : disk.used;
  const pct = total ? Math.round(used / total * 100) : 0;
  if (pct >= (disk.warn_pct || 90)) {
    $('fWarnTxt').textContent = disk.quota
      ? i18nT('files.warnQuota', 'Root is nearly full') + ` (${pct}% · ${humanSize(used)}/${humanSize(disk.quota)})`
      : i18nT('files.warnDisk', 'Disk is nearly full') + ` (${pct}%)`;
    row.hidden = false;
  } else row.hidden = true;
}

function paintCrumbs(rootLabel){
  const box = $('fCrumbs');
  box.textContent = '';
  const mk = (label, target) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.style.flex = '0 1 auto'; b.style.minHeight = '34px'; b.style.fontSize = '12px';
    b.setAttribute('aria-label', label);
    if (target === fmPath) b.classList.add('primary');
    b.addEventListener('click', () => { fmPath = target; fmSel.clear(); paintSelRow(); loadFiles(); });
    box.appendChild(b);
  };
  mk(rootLabel, '');
  let acc = '';
  for (const seg of fmPath.split('/').filter(Boolean)) {
    acc += (acc ? '/' : '') + seg;
    mk('› ' + seg, acc);
  }
}

const typeIcon = e => e.type === 'dir' ? '📁' : e.type === 'link' ? '🔗' : '.jpg.jpeg.png.gif.webp.bmp.svg'.includes(ext(e.name)) ? '🖼️'
  : '.mp4.webm.mkv.mov'.includes(ext(e.name)) ? '🎞️' : '.mp3.wav.ogg.flac.m4a'.includes(ext(e.name)) ? '🎵'
  : '.pdf'.includes(ext(e.name)) ? '📕' : '📄';
const ext = n => '.' + (n.split('.').pop() || '').toLowerCase();

function fmPaint(box, entries, mainView){
  box.textContent = '';
  $('upQueue').textContent = '';
  paintQueue();
  if (!entries.length) {
    const s = document.createElement('small');
    s.style.color = '#777';
    s.textContent = i18nT('files.empty', 'This folder is empty.');
    box.appendChild(s);
    return;
  }
  for (const e of entries) {
    const row = document.createElement('div');
    row.className = 'frow';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = fmSel.has(e.name);
    cb.setAttribute('aria-label', (e.type === 'dir' ? 'Folder ' : 'File ') + e.name);
    cb.addEventListener('click', ev => { ev.stopPropagation(); });
    cb.addEventListener('change', () => {
      if (cb.checked) fmSel.add(e.name); else fmSel.delete(e.name);
      paintSelRow();
    });
    const nm = document.createElement('div');
    nm.className = 'nm';
    nm.textContent = typeIcon(e) + ' ' + e.name;
    nm.title = `${e.name} · ${e.mode || ''} · ${e.owner || ''} · ${fmtWhen(e.mtime)}`;
    nm.style.cursor = e.type === 'dir' ? 'pointer' : 'default';
    nm.addEventListener('click', () => {
      if (e.type === 'dir') { fmPath = (fmPath ? fmPath + '/' : '') + e.name; fmSel.clear(); paintSelRow(); loadFiles(); }
    });
    const meta = document.createElement('div');
    meta.className = 'sz';
    meta.textContent = (e.type === 'dir' ? '—' : humanSize(e.size)) + ' · ' + fmtWhen(e.mtime);
    row.appendChild(cb); row.appendChild(nm); row.appendChild(meta);
    if (mainView && fmReadOnly) {
      const ro = document.createElement('span');
      ro.className = 'sz'; ro.textContent = '🔒'; ro.title = i18nT('files.readonly', 'Read-only');
      row.appendChild(ro);
    }
    if (e.type !== 'dir') {
      const eye = document.createElement('button');
      eye.textContent = '👁'; eye.setAttribute('aria-label', 'Preview ' + e.name);
      eye.addEventListener('click', () => previewEntry(e));
      row.appendChild(eye);
      const dl = document.createElement('button');
      dl.textContent = '⬇'; dl.setAttribute('aria-label', 'Save ' + e.name + ' to phone');
      dl.addEventListener('click', () => downloadOne(e));
      row.appendChild(dl);
    }
    if (mainView && fmSel.has(e.name)) row.style.background = 'var(--card2)';
    box.appendChild(row);
  }
}

function paintSelRow(){
  const on = fmSel.size > 0;
  $('fSelRow').hidden = !on;
  $('fSelRow2').hidden = !on;
  const onlyFiles = [...fmSel].every(n => (fmEntries.find(e => e.name === n) || {}).type === 'file');
  $('fExec').hidden = !(on && onlyFiles && fmSel.size === 1) || fmReadOnly;
  $('fRename').hidden = fmSel.size !== 1 || fmReadOnly;
}

function downloadOne(e){
  const url = '/api/files/content?root=' + encodeURIComponent(fmRoot) + '&path=' + encodeURIComponent((fmPath ? fmPath + '/' : '') + e.name);
  const a = document.createElement('a');
  a.href = url; a.download = e.name;
  document.body.appendChild(a); a.click(); a.remove();
}

function previewEntry(e){
  const rel = (fmPath ? fmPath + '/' : '') + e.name;
  const url = '/api/files/content?root=' + encodeURIComponent(fmRoot) + '&path=' + encodeURIComponent(rel) + '&inline=1';
  $('pvTitle').textContent = e.name;
  const body = $('pvBody');
  body.textContent = '';
  let node = null;
  const mime = { jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp' }[e.name.split('.').pop().toLowerCase()];
  if (mime) node = document.createElement('img');
  else if (ext(e.name).match(/^\.(mp4|webm|mkv|mov)$/)) node = document.createElement('video');
  else if (ext(e.name).match(/^\.(mp3|wav|ogg|flac|m4a)$/)) node = document.createElement('audio');
  else if (ext(e.name) === '.pdf') { node = document.createElement('iframe'); node.src = url; }
  if (node && node.tagName !== 'IFRAME') { node.src = url; node.controls = node.tagName !== 'IMG'; node.alt = e.name; node.style.maxWidth = '100%'; node.style.maxHeight = '50vh'; }
  if (!node) {
    const s = document.createElement('small');
    s.style.color = '#777';
    s.textContent = i18nT('files.noPreview', 'No preview for this type — download it instead.');
    body.appendChild(s);
  } else body.appendChild(node);
  $('pvDl').onclick = () => downloadOne(e);
  const prev = document.createElement('button');
  prev.hidden = true;
  openSheet($('shPreview'));
}

/* queue (#80/#88): per-file state, bounded concurrency, pause/retry/cancel */
const upQueue = [];
const UP_CONCURRENCY = 2;
let upActive = 0;

function paintQueue(){
  const box = $('upQueue');
  if (!box) return;
  box.textContent = '';
  for (const it of upQueue) {
    const row = document.createElement('div');
    row.className = 'frow';
    const nm = document.createElement('div');
    nm.className = 'nm';
    nm.textContent = (it.state === 'running' ? '⬆ ' : it.state === 'paused' ? '⏸ ' : it.state === 'failed' ? '⚠ ' : it.state === 'done' ? '✓ ' : '✕ ') + it.file.name;
    const bar = document.createElement('div');
    bar.style.cssText = 'flex:1;height:4px;background:var(--card2);border-radius:2px;overflow:hidden';
    const fill = document.createElement('div');
    fill.style.cssText = `height:100%;width:${it.pct || 0}%;background:var(--accent)`;
    bar.appendChild(fill);
    const sz = document.createElement('div');
    sz.className = 'sz';
    sz.textContent = `${humanSize(it.ack || 0)}${it.file.size ? ' / ' + humanSize(it.file.size) : ''}${it.retries ? ' · ↻' + it.retries : ''}`;
    row.appendChild(nm); row.appendChild(bar); row.appendChild(sz);
    if (it.state === 'running' || it.state === 'paused') {
      const pb = document.createElement('button');
      pb.textContent = it.state === 'paused' ? '▶' : '⏸';
      pb.setAttribute('aria-label', (it.state === 'paused' ? 'Resume ' : 'Pause ') + it.file.name);
      pb.addEventListener('click', () => { it.paused = it.state !== 'paused'; if (it.paused && it.state === 'running') it.state = 'paused'; else if (!it.paused && it.state === 'paused') { it.state = 'running'; it.wake && it.wake(); } paintQueue(); });
      row.appendChild(pb);
    }
    if (it.state === 'failed') {
      const rb = document.createElement('button');
      rb.textContent = '↻'; rb.setAttribute('aria-label', 'Retry ' + it.file.name);
      rb.addEventListener('click', () => { it.state = 'running'; it.session = null; pumpUploads(); paintQueue(); });
      row.appendChild(rb);
    }
    if (it.state !== 'done') {
      const cb = document.createElement('button');
      cb.textContent = '✕'; cb.setAttribute('aria-label', 'Cancel ' + it.file.name);
      cb.addEventListener('click', () => { it.cancelled = true; if (it.wake) it.wake(); upQueue.splice(upQueue.indexOf(it), 1); if (it.session) fmApi('/api/files/upload/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session: it.session }) }).catch(() => {}); paintQueue(); });
      row.appendChild(cb);
    }
    box.appendChild(row);
  }
}

async function sha256Hex(buf){
  const d = await crypto.subtle.digest('SHA-256', buf);
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('');
}

function queueUploads(files){
  for (const f of files) upQueue.push({ file: f, state: 'running', ack: 0, pct: 0, retries: 0 });
  pumpUploads();
}

async function pumpUploads(){
  while (upActive < UP_CONCURRENCY) {
    const it = upQueue.find(x => x.state === 'running' && !x.started);
    if (!it) return;
    it.started = true; upActive++;
    runUpload(it).finally(() => { upActive--; it.started = false; if (it.state === 'running') it.state = 'failed'; paintQueue(); pumpUploads(); });
  }
}

async function runUpload(it){
  try {
    const dir = fmPath;
    let init = await fmApi('/api/files/upload/init', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: fmRoot, path: dir, name: it.file.name, size: it.file.size, chunk_size: 1 << 20 }),
    });
    if (init.exists && !it.overwrite) {
      const choice = prompt(i18nT('files.exists', 'A file with this name exists — enter a new name, empty = overwrite'), it.file.name);
      if (choice === null) { it.state = 'cancelled'; return; }
      if (choice.trim() && choice !== it.file.name) it.file = new File([it.file], choice.trim(), { type: it.file.type });
      else it.overwrite = true;
      it.started = false; it.state = 'running';
      return;
    }
    it.session = init.session;
    let offset = init.offset || 0;
    const CHUNK = init.chunk_size || (1 << 20);
    while (offset < it.file.size) {
      if (it.cancelled) return;
      while (it.state === 'paused') await new Promise(w => { it.wake = w; });
      if (it.cancelled || it.state === 'failed') return;
      const chunk = await it.file.slice(offset, offset + CHUNK).arrayBuffer();
      const sum = await sha256Hex(chunk);
      let acked = false;
      for (let tries = 0; tries < 3 && !acked; tries++) {
        const r = await fmApi(`/api/files/upload/chunk?session=${it.session}&offset=${offset}`, {
          method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Chunk-Sha256': sum },
          body: chunk,
        });
        if (r.offset >= offset + chunk.byteLength) acked = true;
        else { it.retries++; await new Promise(w => setTimeout(w, 500 * (tries + 1))); }
      }
      if (!acked) throw new Error('chunk not acknowledged');
      offset += chunk.byteLength;
      it.ack = offset;   // progress = bytes the SERVER acknowledged (#80)
      it.pct = it.file.size ? Math.round(offset / it.file.size * 100) : 100;
      paintQueue();
    }
    await fmApi('/api/files/upload/complete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session: it.session }) });
    it.state = 'done'; it.pct = 100;
    status(i18nT('files.uploaded', 'Uploaded') + ' ' + it.file.name);
    loadFiles();
  } catch (e) {
    if (it.cancelled) return;
    it.state = 'failed';
    status((e.message || 'Upload failed') + ': ' + it.file.name, true);
  } finally {
    paintQueue();
  }
}

/* search (#81): bounded recursive, cancellable */
async function runSearch(){
  const q = $('fSearch').value.trim();
  if (fmSearchAbort) fmSearchAbort.abort();
  if (!q) { loadFiles(); return; }
  fmSearchAbort = new AbortController();
  const box = $('fileList');
  box.textContent = '';
  const s = document.createElement('small');
  s.style.color = '#777'; s.textContent = i18nT('files.searching', 'Searching…');
  box.appendChild(s);
  try {
    const j = await fmApi(`/api/files/search?root=${encodeURIComponent(fmRoot)}&path=${encodeURIComponent(fmPath)}&q=${encodeURIComponent(q)}`, { signal: fmSearchAbort.signal });
    box.textContent = '';
    const head = document.createElement('div');
    head.className = 'sz';
    head.textContent = `${j.matches.length}${j.truncated ? '+' : ''} · ` + i18nT('files.results', 'matches');
    box.appendChild(head);
    fmSel.clear();
    fmPaint(box, j.matches.map(m => ({ ...m, dir: m.dir })), false);
  } catch (e) {
    if (e.name === 'AbortError') return;
    box.textContent = '';
    const err = document.createElement('small');
    err.style.color = '#777'; err.textContent = e.message || 'Search failed.';
    box.appendChild(err);
  }
}

/* drag & drop (#80) */
function wireDrop(){
  const zone = $('fDrop');
  for (const [ev, on] of [['dragover', e => { e.preventDefault(); zone.style.background = 'var(--card2)'; }], ['dragleave', () => { zone.style.background = ''; }]]) {
    zone.addEventListener(ev, on);
  }
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.style.background = '';
    const files = [...(e.dataTransfer && e.dataTransfer.files || [])];
    if (files.length) queueUploads(files);
  });
}

/* optional client-side photo compression (#87) */
async function maybeCompress(file){
  if (!$('fCompress') || !$('fCompress').checked) return file;
  if (!/^image\/(jpeg|png|webp)$/.test(file.type) || file.size < 1.5 * (1 << 20)) return file;
  try {
    const bmp = await createImageBitmap(file);
    const max = 2048;
    const sc = Math.min(1, max / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas');
    c.width = Math.round(bmp.width * sc); c.height = Math.round(bmp.height * sc);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.85));
    if (blob && blob.size < file.size) {
      status(`${i18nT('files.compressed', 'Compressed')} ${humanSize(file.size)} → ${humanSize(blob.size)}`);
      return new File([blob], file.name.replace(/\.(jpe?g|png|webp)$/i, '.jpg'), { type: 'image/jpeg' });
    }
  } catch (_) {}
  return file;
}

async function addFiles(list, compress){
  const out = [];
  for (const f of list) out.push(await (compress ? maybeCompress(f) : f));
  queueUploads(out);
}

/* ---- wiring ---- */
$('upBtn').addEventListener('click', () => $('upInput').click());
$('camBtn').addEventListener('click', () => $('camInput').click());
$('upInput').addEventListener('change', async e => { const f = [...e.target.files]; e.target.value = ''; await addFiles(f, false); });
$('camInput').addEventListener('change', async e => { const f = [...e.target.files]; e.target.value = ''; await addFiles(f, true); });
$('fRoot').addEventListener('change', e => { fmRoot = e.target.value; fmPath = ''; fmSel.clear(); paintSelRow(); loadFiles(); });
$('fRefresh').addEventListener('click', () => loadFiles());
$('fSearchGo').addEventListener('click', runSearch);
$('fSearch').addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
wireDrop();

$('fMkdir').addEventListener('click', async () => {
  if (fmReadOnly) return;
  const name = prompt(i18nT('files.newFolder', 'Folder name'));
  if (!name) return;
  try {
    await fmApi('/api/files/mkdir', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ root: fmRoot, path: (fmPath ? fmPath + '/' : '') + name }) });
    loadFiles();
  } catch (e) { status(e.message, true); }
});

$('fSelAll').addEventListener('click', () => {
  if (fmSel.size === fmEntries.length) fmSel.clear();
  else fmSel = new Set(fmEntries.map(e => e.name));
  paintSelRow(); loadFiles();
});
$('fClearSel').addEventListener('click', () => { fmSel.clear(); paintSelRow(); loadFiles(); });

$('fDelete').addEventListener('click', async () => {
  if (!fmSel.size || fmReadOnly) return;
  // #121: the dialog names the exact targets; destructive button non-primary
  const names = [...fmSel].slice(0, 8).join(', ') + (fmSel.size > 8 ? ` (+${fmSel.size - 8} more)` : '');
  const ok = await confirmAction({
    title: i18nT('files.confirmDeleteTitle', 'Delete files?'),
    body: names,
    confirmLabel: i18nT('files.deleteBtn', 'Delete'),
    danger: true,
  });
  if (!ok) return;
  try {
    const j = await fmApi('/api/files/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ root: fmRoot, paths: [...fmSel] }) });
    if (j.failed && j.failed.length) status(`${j.failed.length} × ` + i18nT('files.someFailed', 'could not be deleted'), true);
    fmSel.clear();
  } catch (e) { status(e.message, true); }
  paintSelRow(); loadFiles();
});

$('fRename').addEventListener('click', async () => {
  if (fmSel.size !== 1 || fmReadOnly) return;
  const old = [...fmSel][0];
  const name = prompt(i18nT('files.newName', 'New name'), old);
  if (!name || name === old) return;
  try {
    await fmApi('/api/files/rename', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ root: fmRoot, path: (fmPath ? fmPath + '/' : '') + old, name }) });
    fmSel.delete(old);
  } catch (e) { status(e.message, true); }
  paintSelRow(); loadFiles();
});

$('fDownload').addEventListener('click', () => {
  const sel = [...fmSel].map(n => fmEntries.find(e => e.name === n)).filter(Boolean);
  if (sel.length === 1 && sel[0].type === 'file') return downloadOne(sel[0]);
  if (!sel.length) return;
  // anything multi or a folder: zip it first (#85)
  archiveSelection(sel);
});

async function archiveSelection(sel){
  try {
    const fmt = 'zip';
    const base = sel.length === 1 ? sel[0].name.replace(/\/$/, '') : 'selection';
    const j = await fmApi('/api/files/archive', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ root: fmRoot, paths: [...fmSel], format: fmt, name: base }) });
    const a = document.createElement('a');
    a.href = '/api/files/content?root=' + encodeURIComponent(fmRoot) + '&path=' + encodeURIComponent(j.path);
    a.download = j.path.split('/').pop();
    document.body.appendChild(a); a.click(); a.remove();
    fmSel.clear(); paintSelRow();
    setTimeout(loadFiles, 300);
  } catch (e) { status(e.message, true); }
}

$('fArchive').addEventListener('click', () => archiveSelection([...fmSel]));

$('fExec').addEventListener('click', async () => {
  if (fmSel.size !== 1 || fmReadOnly) return;
  const name = [...fmSel][0];
  const entry = fmEntries.find(e => e.name === name) || {};
  const hasX = entry.mode && entry.mode.match(/[xst]/g);
  const mode = hasX ? '644' : '755';
  if (!confirm((hasX ? i18nT('files.confirmUnexec', 'Remove the executable bit from ') : i18nT('files.confirmExec', 'Mark as executable: ')) + name)) return;
  try {
    await fmApi('/api/files/chmod', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ root: fmRoot, path: (fmPath ? fmPath + '/' : '') + name, mode }) });
    status(name + ' → ' + mode);
  } catch (e) { status(e.message, true); }
  loadFiles();
});

$('pvClose').addEventListener('click', () => { closeSheets(); });
$('pvNext').addEventListener('click', () => { const i = fmEntries.findIndex(e => e.name === $('pvTitle').textContent); for (let k = 1; k <= fmEntries.length; k++) { const n = fmEntries[(i + k) % fmEntries.length]; if (n && n.type === 'file') return previewEntry(n); } });
$('pvPrev').addEventListener('click', () => { const i = fmEntries.findIndex(e => e.name === $('pvTitle').textContent); for (let k = 1; k <= fmEntries.length; k++) { const n = fmEntries[(i - k + fmEntries.length) % fmEntries.length]; if (n && n.type === 'file') return previewEntry(n); } });
$('pvZoom').addEventListener('click', () => { const img = $('pvBody').querySelector('img'); if (img) img.style.width = img.style.width ? '' : '160%'; });

/* ------------------------------------------------------------------ *
 *  System stats (More sheet, polled while open)                       *
 * ------------------------------------------------------------------ */
function fmtGB(b){ return (b / (1 << 30)).toFixed(1) + ' GB'; }
function statRow(lbl, pct, txt){
  const row = document.createElement('div'); row.className = 'stat';
  const l = document.createElement('span'); l.className = 'lbl'; l.textContent = lbl;
  const bar = document.createElement('div'); bar.className = 'bar';
  const i = document.createElement('i'); i.style.width = Math.min(100, Math.max(0, pct)) + '%'; bar.appendChild(i);
  const v = document.createElement('span'); v.className = 'val'; v.textContent = txt;
  row.appendChild(l); row.appendChild(bar); row.appendChild(v);
  return row;
}
async function loadStats(){
  try {
    const r = await fetch('/api/stats', { cache: 'no-store' });
    const s = await r.json();
    const box = $('statsBox');
    box.textContent = '';
    const upH = Math.floor(s.uptime_s / 3600), upM = Math.floor((s.uptime_s % 3600) / 60);
    box.appendChild(statRow('CPU', s.cpu_pct, s.cpu_pct + '%'));
    box.appendChild(statRow('RAM', s.mem_total ? 100 * s.mem_used / s.mem_total : 0,
      fmtGB(s.mem_used) + ' / ' + fmtGB(s.mem_total)));
    box.appendChild(statRow('Disk', s.disk_total ? 100 * s.disk_used / s.disk_total : 0,
      fmtGB(s.disk_used) + ' / ' + fmtGB(s.disk_total)));
    box.appendChild(statRow('Load', s.cores ? 100 * s.load[0] / s.cores : 0,
      s.load[0] + ' · ' + s.cores + ' cores'));
    box.appendChild(statRow('Up', 100, upH + 'h ' + upM + 'm'));
  } catch (_) {}
}

/* ------------------------------------------------------------------ *
 *  Audio v2.5: Opus/WebM (MSE) or raw PCM (Web Audio) with            *
 *  quality tiers + adaptive bitrate (#96), jitter/drift handling      *
 *  (#99), a level meter (#103), codec probing with PCM fallback       *
 *  (#100), sink + per-app controls (#94/#95), recording (#102),       *
 *  media keys (#101), diagnostics (#106) and recovery across          *
 *  visibility changes and reconnects (#124).                          *
 * ------------------------------------------------------------------ */
const SR = 44100, CH = 2, FRAME_BYTES = CH * 2;
let actx = null, reader = null, audioRunning = false, nextTime = 0, carry = new Uint8Array(0), gainNode = null;
let audioEl = null, mseURL = null, pruneTimer = null, pcmAnalyser = null;
let meterCtx = null, meterSrc = null, meterAn = null, meterTimer = null;
let audioWant = false, lastOpusUrl = '', recSheetTimer = null, abrTimer = null, driftTimer = null;

const postJSON = (u, d) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(d || {}) }).then(r => r.json());

// browser codec capabilities (#100): probed once, BEFORE any fetch — an
// incompatible client never starts ffmpeg because it never asks for audio
const AUDCAP = {
  mse: typeof MediaSource !== 'undefined' && !!(MediaSource.isTypeSupported &&
    MediaSource.isTypeSupported('audio/webm; codecs="opus"')),
  webaudio: !!(window.AudioContext || window.webkitAudioContext),
};

// per-device prefs survive reconnects (#96/#98)
const audPref = { get: (k, d) => store.get('aud_' + k, d), set: (k, v) => store.set('aud_' + k, v) };

// playback health, surfaced in the diagnostics view (#99/#106)
const aStats = {
  underruns: 0, overruns: 0, resyncs: 0, undrunTick: 0, jitterMs: 0, bufMs: 0, targetMs: 300,
  chunks: 0, lastArrival: 0, level: 0, peak: 0, clipping: false, codec: '', bitrate: 0,
};

const QUALITY_BITRATES = { low: 48, medium: 96, high: 160 };
function wantBitrateK(){                    // kbps requested of the server
  const mode = audPref.get('q', 'auto');
  if (mode === 'custom') return Math.max(16, Math.min(320, parseInt(audPref.get('brc', '96'), 10) || 96));
  if (mode === 'auto') return Math.max(16, Math.min(320, parseInt(audPref.get('autoBr', '96'), 10) || 96));
  return QUALITY_BITRATES[mode] || 96;
}
function paintBitrateNow(){
  const el = $('audioBitrateNow');
  if (el) el.textContent = wantBitrateK() + ' kbps' + (audPref.get('q', 'auto') === 'auto' ? ' · auto' : '');
}
function currentStreamUrl(){ return '/api/audio.opus?bitrate=' + (wantBitrateK() * 1000); }

function updateAudioBtn(){
  $('bAudio').classList.toggle('on', audioRunning);
  // recoverable-error UI (#124): a dropped stream turns Start into Retry
  $('audioStart').textContent = (audioWant && !audioRunning)
    ? i18nT('audio.retry', 'Retry') : i18nT('audio.start', 'Start');
}

function onChunkArrival(){
  aStats.chunks++;
  const now = performance.now();
  if (aStats.lastArrival) {
    const d = now - aStats.lastArrival;
    aStats.jitterMs = aStats.jitterMs * 0.85 + Math.min(2000, Math.abs(d - 250)) * 0.15;  // rough arrival irregularity
  }
  aStats.lastArrival = now;
}

async function startAudio(){
  if (audioRunning) return;
  // #100: unsupported browsers get actionable feedback, not silence — and
  // no fetch is made, so the server never spawns ffmpeg for them
  if (!AUDCAP.webaudio && !AUDCAP.mse) {
    status(i18nT('audio.unsupported', 'This browser cannot play the audio stream (no WebAudio, no MSE/Opus).'), true);
    return;
  }
  if ($('audioCodec').value === 'opus' && !AUDCAP.mse) {
    $('audioCodec').value = 'pcm';
    status(i18nT('audio.fallbackPcm', 'Opus/WebM is not supported here — falling back to raw PCM.'), true);
  }
  audioWant = true;
  updateAudioBtn();
  return $('audioCodec').value === 'opus' ? startOpus() : startPCM();
}

function bufferedAheadMs(){
  try {
    const b = audioEl.buffered;
    return b.length ? (b.end(b.length - 1) - audioEl.currentTime) * 1000 : 0;
  } catch (_) { return 0; }
}

async function startOpus(){
  try {
    audioEl = audioEl || new Audio();
    audioEl.volume = parseFloat($('audioVol').value);
    audioEl.playbackRate = 1.0;
    ensureOpusMeter();
    const mse = new MediaSource();
    mseURL = URL.createObjectURL(mse);
    audioEl.src = mseURL;
    await new Promise((res, rej) => {
      mse.addEventListener('sourceopen', res, { once: true });
      setTimeout(() => rej(new Error('MediaSource timeout')), 5000);
    });
    const sb = mse.addSourceBuffer('audio/webm; codecs="opus"');
    sb.mode = 'sequence';
    const resp = await fetch(currentStreamUrl(), { cache: 'no-store' });
    if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
    lastOpusUrl = currentStreamUrl();
    aStats.codec = 'opus'; aStats.bitrate = wantBitrateK();
    audioRunning = true;
    updateAudioBtn();
    status('Audio streaming (Opus ' + wantBitrateK() + ' kbps)');
    audioEl.play().catch(() => {});
    let queue = [];
    sb.addEventListener('updateend', () => {
      if (!audioRunning) return;
      if (queue.length && !sb.updating) sb.appendBuffer(queue.shift());
    });
    // prune buffered data behind playback so memory doesn't grow forever
    pruneTimer = setInterval(() => {
      try {
        if (!audioRunning || !sb || !sb.buffered.length) return;
        const start = sb.buffered.start(0);
        if (start < audioEl.currentTime - 30) sb.remove(start, audioEl.currentTime - 25);
      } catch (_) {}
    }, 20000);
    // underrun: the buffer ran dry — widen the jitter target (#99)
    // (on* assignment, not addEventListener: restarts must not stack handlers)
    audioEl.onwaiting = () => {
      aStats.underruns++; aStats.undrunTick++;
      aStats.targetMs = Math.min(800, aStats.targetMs + 120);
      audioEl.playbackRate = 1.0;
    };
    reader = resp.body.getReader();
    (async () => {
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done || !audioRunning) break;
          if (!value || !value.length) continue;
          onChunkArrival();
          queue.push(value);
          if (queue.length > 300) queue.splice(0, queue.length - 300); // don't hoard if stalled
          if (!sb.updating && queue.length) sb.appendBuffer(queue.shift());
        }
      } catch (_) {}
      finally { streamDied(); }
    })();
  } catch (e) {
    audioRunning = false;
    stopAudio();
    streamDied();
    status('Audio failed: ' + e.message, true);
  }
}

function streamDied(){
  // #124: a lost stream (network switch, PulseAudio restart, server
  // 503) becomes a visible retry state instead of silence
  const was = audioRunning;
  audioRunning = false;
  updateAudioBtn();
  if (audioWant && was) status(i18nT('audio.lost', 'Audio lost — press Retry (or Start) to reconnect.'), true);
}

/* drift + jitter control (#99): nudge playbackRate and resync to the
   live edge so a 60-minute session never accumulates unbounded delay */
function driftTick(){
  if (!audioRunning || !audioEl) return;
  aStats.bufMs = bufferedAheadMs();
  const t = aStats.targetMs / 1000;
  if (aStats.bufMs > 5000) { resyncOpus(true); return; }   // backlog (tab was hidden)
  if (aStats.bufMs > t * 2.2) audioEl.playbackRate = 1.06; // gently catch up
  else if (aStats.bufMs > 0 && aStats.bufMs < t * 0.4) audioEl.playbackRate = 0.94;  // let the buffer fill
  else audioEl.playbackRate = 1.0;
}
function resyncOpus(count){
  try {
    const b = audioEl.buffered;
    if (!b.length) return;
    const end = b.end(b.length - 1);
    if (end - audioEl.currentTime > 1.2) {
      audioEl.currentTime = Math.max(0, end - Math.max(0.2, aStats.targetMs / 1000));
      audioEl.playbackRate = 1.0;
      if (count) { aStats.overruns++; aStats.resyncs++; }
    }
  } catch (_) {}
}

/* adaptive bitrate (#96): auto mode walks the bitrate tier with the
   measured RTT and buffer occupancy; manual presets stay put */
function abrTick(){
  if (!audioRunning || audPref.get('q', 'auto') !== 'auto') { aStats.undrunTick = 0; return; }
  const rtt = (typeof rttMs === 'number') ? rttMs : null;
  const br = wantBitrateK();
  const underruns = aStats.undrunTick; aStats.undrunTick = 0;
  if ((rtt !== null && rtt > 800) || aStats.bufMs > aStats.targetMs * 4 || underruns > 2) {
    if (br > 48) setAutoBr(Math.max(48, br - 32));
    return;
  }
  if (rtt !== null && rtt < 300 && aStats.bufMs < aStats.targetMs * 1.5 && underruns === 0) {
    if (br < 160) setAutoBr(Math.min(160, br + 32));
  }
}
function setAutoBr(k){
  audPref.set('autoBr', String(k));
  paintBitrateNow();
  if (audioRunning && $('audioCodec').value === 'opus') { stopAudio(); startAudio(); }  // no page reload (#96)
}

async function startPCM(){
  try {
    actx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: SR });
    await actx.resume();
    const resp = await fetch('/api/audio/raw', { cache: 'no-store' });
    if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
    reader = resp.body.getReader();
    carry = new Uint8Array(0);
    aStats.codec = 'pcm'; aStats.bitrate = 1411;
    nextTime = actx.currentTime + aStats.targetMs / 1000;   // start buffered to the target (#99)
    audioRunning = true;
    updateAudioBtn();
    status('Audio streaming (PCM)');
    (async () => {
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done || !audioRunning) break;
          if (!value || !value.length) continue;
          onChunkArrival();
          const merged = new Uint8Array(carry.length + value.length);
          merged.set(carry); merged.set(value, carry.length);
          const frames = Math.floor(merged.length / FRAME_BYTES);
          if (!frames) { carry = merged; continue; }
          carry = merged.slice(frames * FRAME_BYTES);
          const buf = actx.createBuffer(CH, frames, SR);
          const L = buf.getChannelData(0), R = buf.getChannelData(1);
          const dv = new DataView(merged.buffer, merged.byteOffset, frames * FRAME_BYTES);
          for (let i = 0; i < frames; i++) {
            L[i] = dv.getInt16(i * 4, true) / 32768;
            R[i] = dv.getInt16(i * 4 + 2, true) / 32768;
          }
          const src = actx.createBufferSource();
          src.buffer = buf;
          if (!gainNode) {
            gainNode = actx.createGain();
            gainNode.gain.value = parseFloat($('audioVol').value);
            gainNode.connect(actx.destination);
            pcmAnalyser = actx.createAnalyser();           // level meter tap (#103)
            pcmAnalyser.fftSize = 1024;
            gainNode.connect(pcmAnalyser);
          }
          src.connect(gainNode);
          const now = actx.currentTime;
          const t = aStats.targetMs / 1000;
          if (nextTime < now + 0.02) {                     // underrun (#99): re-buffer to the target
            aStats.underruns++; aStats.undrunTick++;
            aStats.targetMs = Math.min(800, aStats.targetMs + 120);
            nextTime = now + t;
          }
          if (nextTime > now + t * 2.5) {                  // backlog (tab was hidden): drop (#99)
            aStats.overruns++;
            nextTime = now + t;
          }
          src.start(nextTime);
          nextTime += buf.duration;
        }
      } catch (_) {}
      finally { streamDied(); }
    })();
  } catch (e) {
    audioRunning = false;
    stopAudio();
    streamDied();
    status('Audio failed: ' + e.message, true);
  }
}
function stopAudio(explicit){
  if (explicit) audioWant = false;
  audioRunning = false;
  gainNode = null;
  try { reader && reader.cancel(); } catch(_) {}
  reader = null; carry = new Uint8Array(0);
  if (actx) { try { actx.close(); } catch(_) {} actx = null; pcmAnalyser = null; }
  if (pruneTimer) { clearInterval(pruneTimer); pruneTimer = null; }
  if (audioEl) { try { audioEl.pause(); } catch(_) {} audioEl.removeAttribute('src'); }
  if (mseURL) { try { URL.revokeObjectURL(mseURL); } catch(_) {} mseURL = null; }
  updateAudioBtn();
}

/* ---- level meter (#103): passive tap, 5 Hz refresh, sheet-open only */
function ensureOpusMeter(){
  if (!AUDCAP.webaudio) return null;
  try {
    meterCtx = meterCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (!meterSrc) {
      meterSrc = meterCtx.createMediaElementSource(audioEl);
      meterAn = meterCtx.createAnalyser();
      meterAn.fftSize = 1024;
      meterSrc.connect(meterAn);
      meterAn.connect(meterCtx.destination);
    }
    meterCtx.resume().catch(() => {});
    return meterAn;
  } catch (_) { return null; }
}
function currentAnalyser(){
  if ($('audioCodec').value === 'opus') return meterAn;
  return (actx && pcmAnalyser) ? pcmAnalyser : null;
}
function audioSheetOpen(){ return $('shAudio').classList.contains('show'); }
function meterTick(){
  if (!audioSheetOpen()) { meterStop(); return; }
  meterTimer = setTimeout(meterTick, 200);   // throttled for battery (#103)
  if (!audioRunning) { paintMeter(0, 0, 'off'); return; }
  const an = currentAnalyser();
  const stalled = performance.now() - aStats.lastArrival > 5000;
  if (!an || stalled) { paintMeter(0, 0, 'stall'); return; }   // capture failure ≠ silence (#103)
  try {
    const buf = new Float32Array(an.fftSize);
    an.getFloatTimeDomainData(buf);
    let peak = 0, sum = 0;
    for (let i = 0; i < buf.length; i++) { const v = buf[i]; peak = Math.max(peak, Math.abs(v)); sum += v * v; }
    const rms = Math.sqrt(sum / buf.length);
    aStats.level = rms; aStats.peak = peak; aStats.clipping = peak >= 0.99;
    paintMeter(rms, peak, aStats.clipping ? 'clip' : (rms < 0.004 ? 'silent' : ''));
  } catch (_) { paintMeter(0, 0, 'stall'); }
}
function meterStart(){ if (!meterTimer) meterTick(); }
function meterStop(){
  if (meterTimer) { clearTimeout(meterTimer); meterTimer = null; }
  paintMeter(0, 0, 'off');
}
function paintMeter(rms, peak, state){
  const bar = $('audioMeterBar'), txt = $('audioMeterState');
  if (!bar) return;
  bar.style.width = Math.min(100, Math.max(rms * 300, peak * 100)).toFixed(1) + '%';
  txt.textContent = state === 'clip' ? i18nT('audio.mClip', 'clipping')
    : state === 'silent' ? i18nT('audio.mSilent', 'silent')
    : state === 'stall' ? i18nT('audio.mStall', 'capture stalled')
    : state === 'off' ? '' : i18nT('audio.mLive', 'live');
  txt.style.color = state === 'clip' ? 'var(--err)' : state === 'stall' ? 'var(--dot-fair)' : 'var(--muted)';
}
function audioSheetStop(){ meterStop(); if (recSheetTimer) { clearInterval(recSheetTimer); recSheetTimer = null; } }

/* ---- sinks: selection + device volume/mute (#94) */
let sinkDebounce = null;
async function loadSinks(){
  try {
    const j = await fetch('/api/audio/sinks').then(r => r.json());
    const sel = $('audioSink');
    sel.textContent = '';
    for (const s of j.sinks || []) {
      const o = document.createElement('option');
      o.value = s.name;
      o.textContent = (s.description || s.name) + ' (' + s.state + (s.volume != null ? ' · ' + s.volume + '%' : '') + ')';
      if (s.name === j.selected) o.selected = true;
      sel.appendChild(o);
    }
    if (!j.sinks || !j.sinks.length) {
      const o = document.createElement('option');
      o.textContent = '—';
      sel.appendChild(o);
      sel.disabled = true;
    } else sel.disabled = false;
    if (j.volume != null) $('audioSinkVol').value = j.volume;
    $('audioSinkMute').setAttribute('aria-pressed', j.mute ? 'true' : 'false');
    $('audioSinkMute').classList.toggle('on', !!j.mute);
    const n = $('audioNotice');
    n.hidden = !j.notice; n.textContent = j.notice || '';
  } catch (_) {}
}
$('audioSink').addEventListener('change', e => {
  postJSON('/api/audio/sink', { name: e.target.value }).then(j => {
    if (!j.ok) { status(j.error || 'sink switch failed', true); loadSinks(); }
    else status('Capture sink → ' + e.target.value);
  }).catch(() => {});
});
$('audioSinkRefresh').addEventListener('click', loadSinks);
$('audioSinkVol').addEventListener('input', e => {
  clearTimeout(sinkDebounce);
  const v = parseInt(e.target.value, 10);
  sinkDebounce = setTimeout(() => { postJSON('/api/audio/volume', { volume: v }).catch(() => {}); }, 400);  // debounced (#94)
});
$('audioSinkMute').addEventListener('click', e => {
  const on = e.currentTarget.getAttribute('aria-pressed') === 'true';
  postJSON('/api/audio/volume', { mute: !on }).then(() => loadSinks()).catch(() => {});
});

/* ---- per-application volume (#95): hidden entirely when unsupported */
const appDeb = {};
async function loadAudioApps(){
  try {
    const j = await fetch('/api/audio/apps').then(r => r.json());
    const box = $('audioApps'), wrap = $('audioAppsBox');
    if (!j.supported) { wrap.hidden = true; return; }   // #95: UI hides when unsupported
    wrap.hidden = false;
    box.textContent = '';
    if (!j.apps.length) {
      const s = document.createElement('small');
      s.style.color = 'var(--muted)';
      s.textContent = i18nT('audio.nothing', 'Nothing is playing on the desktop.');
      box.appendChild(s);
      return;
    }
    for (const g of j.apps) {
      const row = document.createElement('div');
      row.className = 'row';
      const lbl = document.createElement('label');
      lbl.style.flex = '0 0 auto;max-width:40%';
      lbl.textContent = g.app + (g.count > 1 ? ' ×' + g.count : '');
      lbl.title = g.app;
      const rng = document.createElement('input');
      rng.type = 'range'; rng.min = '0'; rng.max = '150'; rng.step = '5';
      rng.value = g.pref_volume != null ? g.pref_volume : (g.volume != null ? g.volume : 100);
      rng.setAttribute('aria-label', i18nT('audio.appsH', 'Applications') + ': ' + g.app);
      rng.addEventListener('input', () => {
        clearTimeout(appDeb[g.app]);
        appDeb[g.app] = setTimeout(() => {
          postJSON('/api/audio/app-volume', { app: g.app, volume: parseInt(rng.value, 10) }).catch(() => {});
        }, 400);
      });
      const mbtn = document.createElement('button');
      mbtn.type = 'button'; mbtn.style.flex = '0 0 auto';
      mbtn.textContent = g.mute || g.pref_mute ? '🔇' : '🔊';
      mbtn.setAttribute('aria-pressed', (g.mute || g.pref_mute) ? 'true' : 'false');
      mbtn.setAttribute('aria-label', i18nT('audio.mute', 'Mute') + ': ' + g.app);
      mbtn.addEventListener('click', () => {
        const mute = mbtn.getAttribute('aria-pressed') !== 'true';
        postJSON('/api/audio/app-volume', { app: g.app, mute }).then(() => loadAudioApps()).catch(() => {});
      });
      row.appendChild(lbl); row.appendChild(rng); row.appendChild(mbtn);
      box.appendChild(row);
    }
  } catch (_) {}
}

/* ---- recording (#102) */
async function recPoll(){
  try {
    const st = await fetch('/api/audio/record').then(r => r.json());
    const btn = $('audioRec'), flag = $('recFlag'), flags = $('audioFlags'), info = $('audioRecInfo');
    btn.textContent = st.active ? i18nT('audio.recStop', '⏹ Stop recording') : i18nT('audio.recStart', '⏺ Start recording');
    btn.classList.toggle('warn', !!st.active);
    flag.hidden = !st.active;
    flags.hidden = !(st.active || !$('onlyFlag').hidden);
    if (st.active) {
      const mm = String(Math.floor(st.seconds / 60)).padStart(2, '0'), ss = String(st.seconds % 60).padStart(2, '0');
      info.textContent = 'REC ' + mm + ':' + ss + ' · ' + (st.bytes / 1048576).toFixed(1) + ' MB · ' +
        st.name + ' · cap ' + st.max_minutes + ' min / ' + st.max_mb + ' MB';
      loadRecordings();
    } else if (!info.textContent) info.textContent = '';
  } catch (_) {}
}
async function loadRecordings(){
  try {
    const j = await fetch('/api/audio/recordings').then(r => r.json());
    const box = $('audioRecList');
    box.textContent = '';
    for (const r of (j.recordings || []).slice(0, 10)) {
      const row = document.createElement('div');
      row.className = 'row';
      const a = document.createElement('a');
      a.href = r.download; a.download = r.name;
      a.textContent = '⬇ ' + r.name + ' · ' + (r.size / 1048576).toFixed(1) + ' MB';
      a.style.cssText = 'color:var(--fg);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
      row.appendChild(a);
      box.appendChild(row);
    }
  } catch (_) {}
}
$('audioRec').addEventListener('click', async () => {
  try {
    const cur = await fetch('/api/audio/record').then(r => r.json());
    if (cur.active) {
      await postJSON('/api/audio/record', { action: 'stop' });
      status(i18nT('audio.recSaved', 'Recording saved to the files root'));
    } else {
      const j = await postJSON('/api/audio/record', { action: 'start', format: $('audioRecFmt').value });
      if (!j.ok) status(j.error || 'Recording failed', true);
      else status(i18nT('audio.recOn', 'Recording desktop audio…'));
    }
    recPoll();
  } catch (_) { status('Recording control failed', true); }
});

/* ---- media keys (#101): MPRIS when possible, XF86 keysyms otherwise */
async function loadMediaStatus(){
  try {
    const j = await fetch('/api/media/status').then(r => r.json());
    $('mediaPlayer').textContent = j.active ? i18nT('audio.player', 'Player') + ': ' + j.active : '';
  } catch (_) {}
}
document.querySelectorAll('#mediaKeys button').forEach(b => {
  b.addEventListener('click', async () => {
    try {
      const j = await postJSON('/api/media/keys', { key: b.dataset.mkey });
      if (!j.ok) status(j.error || 'key failed', true);
      else if (j.player) $('mediaPlayer').textContent = i18nT('audio.player', 'Player') + ': ' + j.player;
    } catch (_) { status('key failed', true); }
  });
});

/* ---- diagnostics (#106): identify capture vs encode vs transport vs playback */
function redact(s){
  let t = String(s == null ? '' : s);
  t = t.replace(/\/(home|Users)\/[^\s"']+/g, '/~');                              // home paths
  t = t.replace(/\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/g, 'x.x.x.x');           // IPs
  t = t.replace(/unix:[^\s"']+/g, 'unix:[socket]');
  return t;
}
async function loadDiag(){
  try {
    const srv = await fetch('/api/audio/diagnostics').then(r => r.json());
    const el = $('audioDiag');
    el.hidden = false;
    const line = (k, v) => '<b>' + k + '</b> ' + v;
    const rows = [
      line('sink:', (srv.selected_sink || 'default') + (srv.fallback ? ' → fell back to ' + srv.effective_sink : ' (' + srv.effective_sink + ')')),
      line('source:', srv.chosen_source),
      line('pulse:', srv.pulse_ok ? 'running' : 'unreachable') + ', pactl ' + (srv.pactl ? 'ok' : 'missing') +
        ', ffmpeg ' + (srv.ffmpeg ? 'ok' : 'missing'),
      line('codec:', aStats.codec + ' @ ' + wantBitrateK() + ' kbps (' + audPref.get('q', 'auto') + ')' +
        ' · browser MSE/Opus ' + (AUDCAP.mse ? 'yes' : 'NO') + ', WebAudio ' + (AUDCAP.webaudio ? 'yes' : 'NO')),
      line('buffer:', 'target ' + aStats.targetMs + ' ms · now ' + Math.round(aStats.bufMs) + ' ms · jitter ~' + Math.round(aStats.jitterMs) + ' ms'),
      line('health:', aStats.underruns + ' underruns · ' + aStats.overruns + ' overruns · ' + aStats.resyncs + ' resyncs'),
      line('captures:', (srv.captures || []).length + ' (' + (srv.listeners || 0) + ' listener(s)) ' +
        JSON.stringify(srv.captures || [])),
      line('record:', srv.record && srv.record.active ? srv.record.name + ' ' + srv.record.seconds + 's' : 'idle'),
      line('level:', 'rms ' + aStats.level.toFixed(4) + ' · peak ' + aStats.peak.toFixed(3) + (aStats.clipping ? ' · CLIPPING' : '')),
      srv.notice ? line('notice:', redact(srv.notice)) : '',
      srv.last_error ? line('last error:', redact(srv.last_error)) : '',
      srv.ffmpeg_log ? line('ffmpeg log:', '\n' + redact(srv.ffmpeg_log).split('\n').slice(-6).join('\n')) : '',
    ].filter(Boolean);
    el.innerHTML = rows.map(r => '<div>' + r + '</div>').join('');
  } catch (_) {}
}
$('audioDiagRefresh').addEventListener('click', () => { loadDiag(); loadSinks(); loadAudioApps(); });
$('audioDiagCopy').addEventListener('click', async () => {
  await loadDiag();
  try {
    await navigator.clipboard.writeText($('audioDiag').innerText);
    status(i18nT('audio.diagCopied', 'Diagnostics copied (host details redacted)'));
  } catch (_) { status('Copy failed', true); }
});

$('audioStart').addEventListener('click', startAudio);
$('audioStop').addEventListener('click', () => { stopAudio(true); status('Audio stopped'); });
$('audioTest').addEventListener('click', async () => {
  try { const r = await fetch('/api/audio/test'); const j = await r.json(); status(j.msg || 'test tone'); }
  catch(_) { status('Test failed', true); }
});
$('audioCodec').addEventListener('change', () => {
  if (audioRunning) { stopAudio(); startAudio(); }
});
$('audioVol').addEventListener('input', e => {
  const v = parseFloat(e.target.value);
  if (gainNode && actx) gainNode.gain.setTargetAtTime(v, actx.currentTime, 0.02);
  if (audioEl) audioEl.volume = v;
});
$('audioQuality').addEventListener('change', e => {
  audPref.set('q', e.target.value);          // remembered per device (#96)
  $('audioBitrateCustom').hidden = e.target.value !== 'custom';
  paintBitrateNow();
  if (audioRunning) { stopAudio(); startAudio(); }   // takes effect without a reload (#96)
});
$('audioBitrateCustom').addEventListener('change', e => {
  audPref.set('brc', String(Math.max(16, Math.min(320, parseInt(e.target.value, 10) || 96))));
  paintBitrateNow();
  if (audioRunning && $('audioCodec').value === 'opus') { stopAudio(); startAudio(); }
});

// restore remembered preferences (#96/#98)
$('audioQuality').value = ['auto', 'low', 'medium', 'high', 'custom'].includes(audPref.get('q', 'auto')) ? audPref.get('q', 'auto') : 'auto';
$('audioBitrateCustom').hidden = $('audioQuality').value !== 'custom';
$('audioBitrateCustom').value = audPref.get('brc', '96');
paintBitrateNow();

/* ---- audio-only mode (#98): framebuffer off, audio + clipboard on */
function audioOnlyOn(){ return audPref.get('only', '0') === '1'; }
function paintAudioOnly(){
  const on = audioOnlyOn();
  $('audioOnlyChk').checked = on;
  $('onlyFlag').hidden = !on;
  $('audioFlags').hidden = !on && $('recFlag').hidden;
}
function setAudioOnly(on){
  audPref.set('only', on ? '1' : '0');
  paintAudioOnly();
  if (on) {
    // stop framebuffer traffic but keep reconnect + clipboard reachable
    if (rfb) { userDisconnected = true; try { rfb.disconnect(); } catch(_) {} rfb = null; }
    screenEl.textContent = '';
    $('overlay').classList.add('hide');
    status(i18nT('audio.onlyOn', 'Audio-only mode: desktop video is paused'));
  } else {
    status(i18nT('audio.onlyOff', 'Restoring desktop…'));
    connect();
  }
}
$('audioOnlyChk').addEventListener('change', e => setAudioOnly(e.target.checked));
if (audioOnlyOn()) { $('overlay').classList.add('hide'); paintAudioOnly(); }

// mobile browsers auto-suspend audio contexts in background tabs (#124)
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  if (actx) actx.resume().catch(() => {});
  if (meterCtx) meterCtx.resume().catch(() => {});
  // a stream that died while backgrounded comes back without a reload
  if (audioWant && !audioRunning) setTimeout(() => { if (audioWant && !audioRunning) startAudio(); }, 600);
});

// adaptive + drift controllers (#96/#99)
setInterval(abrTick, 5000);
setInterval(() => { if (audioRunning && $('audioCodec').value === 'opus') driftTick(); }, 1000);

// persistent recording indicator, even with the sheet closed (#102)
recPoll();
setInterval(recPoll, 15000);

/* ------------------------------------------------------------------ *
 *  Devices in: share THIS computer's mic and webcam into the desktop  *
 *  so remote apps see a real capture device. Desktop-browser clients  *
 *  only — the controls stay hidden on phones and tablets.             *
 * ------------------------------------------------------------------ */

const DESKTOP_CLIENT = matchMedia('(pointer: fine)').matches &&
                      !/Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
if (DESKTOP_CLIENT) $('devRow').hidden = false;

function wsURL(path){
  return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + path;
}

// AudioWorklet source: getUserMedia -> Int16 PCM chunks (s16le/48 kHz/mono),
// ~85 ms per chunk, ~96 kB/s upstream. Posted to the main thread over a
// MessagePort; the WebSocket then carries the raw buffers.
const MIC_WORKLET = `
  class MicIn extends AudioWorkletProcessor {
    constructor(){ super(); this.buf = new Int16Array(4096); this.n = 0; }
    process(inputs){
      const ch = inputs[0] && inputs[0][0];
      if (!ch) return true;
      for (let i = 0; i < ch.length; i++){
        const s = Math.max(-1, Math.min(1, ch[i]));
        this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        if (this.n === this.buf.length){ this.port.postMessage(this.buf.slice(0)); this.n = 0; }
      }
      return true;
    }
  }
  registerProcessor('mic-in', MicIn);`;

let micIn = null;   // { ws, actx, stream }
let camIn = null;   // { ws, rec, stream }

async function startMicIn(){
  if (micIn) return;
  const ws = new WebSocket(wsURL('/api/mic'));
  ws.binaryType = 'arraybuffer';
  try {
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('server refused mic-in')); });
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const actx = new AudioContext({ sampleRate: 48000 });
    await actx.audioWorklet.addModule(URL.createObjectURL(new Blob([MIC_WORKLET], { type: 'application/javascript' })));
    const node = new AudioWorkletNode(actx, 'mic-in');
    node.port.onmessage = e => { if (ws.readyState === 1) ws.send(e.data); };
    actx.createMediaStreamSource(stream).connect(node);  // never to destination: no local echo
    ws.onclose = () => stopMicIn();
    micIn = { ws, actx, stream };
    $('micInBtn').classList.add('on');
    status('Mic in: this computer is the desktop microphone');
  } catch (e) {
    try { ws.close(); } catch(_) {}
    stopMicIn();
    status('Mic in failed: ' + e.message, true);
  }
}

function stopMicIn(){
  if (micIn){
    try { micIn.ws.onclose = null; micIn.ws.close(); } catch(_) {}
    micIn.stream.getTracks().forEach(t => t.stop());
    try { micIn.actx.close(); } catch(_) {}
    micIn = null;
  }
  $('micInBtn').classList.remove('on');
}

async function startCamIn(){
  if (camIn) return;
  let st = {};
  try { st = await fetch('/api/camera/status').then(r => r.json()); } catch(_) {}
  if (!st.device){
    status(st.loopback === false
      ? 'Cam in needs the v4l2loopback module on the desktop (sudo modprobe v4l2loopback)'
      : 'Cam in: no /dev/video device on the desktop', true);
    return;
  }
  const ws = new WebSocket(wsURL('/api/camera'));
  ws.binaryType = 'arraybuffer';
  try {
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('server refused cam-in')); });
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } } });
    const mime = ['video/webm;codecs=vp8', 'video/webm'].find(m => MediaRecorder.isTypeSupported(m)) || '';
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime, videoBitsPerSecond: 2500000 } : undefined);
    rec.ondataavailable = e => { if (ws.readyState === 1 && e.data.size) ws.send(e.data); };
    rec.onstop = () => { try { ws.close(); } catch(_) {} };
    rec.start(500);                       // flush a WebM cluster every 500 ms
    ws.onclose = () => stopCamIn();
    camIn = { ws, rec, stream };
    $('camInBtn').classList.add('on');
    status('Cam in: this computer is the desktop webcam (' + st.device + ')');
  } catch (e) {
    try { ws.close(); } catch(_) {}
    stopCamIn();
    status('Cam in failed: ' + e.message, true);
  }
}

function stopCamIn(){
  if (camIn){
    try { camIn.rec.stop(); } catch(_) {}   // onstop closes the websocket
    camIn.stream.getTracks().forEach(t => t.stop());
    camIn = null;
  }
  $('camInBtn').classList.remove('on');
}

$('micInBtn').addEventListener('click', () => {
  if (micIn) { stopMicIn(); status('Mic in stopped'); } else startMicIn();
});
$('camInBtn').addEventListener('click', () => {
  if (camIn) { stopCamIn(); status('Cam in stopped'); } else startCamIn();
});

/* ------------------------------------------------------------------ *
 *  Session keep-alive: refresh page if the API was restarted          *
 *  (version bump = new deploy)                                        *
 * ------------------------------------------------------------------ */
let version = null;
(async () => {
  try {
    const r = await fetch('/api/health', { cache: 'no-store' });
    if (r.status === 401) { location.href = '/login'; return; }
  } catch(_) {}
})();
setInterval(async () => {
  if (document.hidden) return;
  try {
    const r = await fetch('/api/health', { cache: 'no-store' });
    if (r.status === 401) location.href = '/login';
  } catch(_) {}
}, 60000);

/* ------------------------------------------------------------------ *
 *  PWA: service worker + install prompt (#49)                         *
 * ------------------------------------------------------------------ */
// Register only on secure origins (https or localhost); plain-HTTP LAN
// setups skip the worker and keep working exactly as before.
if ('serviceWorker' in navigator &&
    (window.isSecureContext || ['localhost', '127.0.0.1'].includes(location.hostname))) {
  addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js?v=2.6.2').then(reg => {
      if (reg.waiting && navigator.serviceWorker.controller)
        status('App update ready — reload to apply');
      reg.addEventListener('updatefound', () => {
        const sw = reg.installing;
        if (!sw) return;
        sw.addEventListener('statechange', () => {
          if (sw.state === 'installed' && navigator.serviceWorker.controller)
            status('App update ready — reload to apply');
        });
      });
    }).catch(() => {});
  });
}
let installEvent = null;
addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  installEvent = e;
  $('installBtn').hidden = false;
});
$('installBtn').addEventListener('click', async () => {
  if (!installEvent) {
    // iOS Safari has no install prompt: Add to Home Screen is manual
    status('Use the browser menu — Share → Add to Home Screen (iOS)');
    return;
  }
  try {
    installEvent.prompt();
    await installEvent.userChoice;
  } catch (_) {}
  installEvent = null;
  $('installBtn').hidden = true;
});

/* ------------------------------------------------------------------ *
 *  First-run tour (#56): three cards, skip at every step, shown once  *
 * ------------------------------------------------------------------ */
let tourStep = 0;
function paintTour(){
  $(`tourTitle`).textContent = i18nT('tour.title' + (tourStep + 1), '');
  $('tourBody').textContent = i18nT('tour.body' + (tourStep + 1), '');
  document.querySelectorAll('#tour .tourDots i').forEach((d, i) =>
    d.classList.toggle('on', i === tourStep));
  $('tourNext').textContent = tourStep < 2 ? i18nT('btn.next', 'Next') : i18nT('btn.done', 'Done');
}
function showTour(){ tourStep = 0; paintTour(); $('tour').hidden = false; }
function endTour(){ $('tour').hidden = true; store.set('tour', '1'); }
$('tourNext').addEventListener('click', () => {
  if (tourStep < 2) { tourStep++; paintTour(); } else endTour();
});
$('tourSkip').addEventListener('click', endTour);
$('tourReplay').addEventListener('click', () => { closeSheets(); showTour(); });
$('tour').addEventListener('click', e => { if (e.target.id === 'tour') endTour(); });
if (!store.get('tour', '')) setTimeout(showTour, 2500);

/* ------------------------------------------------------------------ *
 *  Toolbar customization (#68): reorder, hide, restore                *
 * ------------------------------------------------------------------ */
const BAR_DEFAULT = ['bApps', 'bWins', 'bStore', 'bKeys', 'bTouch', 'bAudio', 'bFiles', 'bFull', 'bMore'];
const BAR_LABELS = { bApps: 'bar.apps', bWins: 'bar.windows', bStore: 'bar.store', bKeys: 'bar.keys', bTouch: 'bar.touch',
                     bAudio: 'bar.audio', bFiles: 'bar.files', bFull: 'bar.full', bMore: 'bar.more' };
function loadBarOrder(){
  let order;
  try { order = JSON.parse(store.get('barOrder', '') || 'null'); } catch (_) { order = null; }
  if (!Array.isArray(order)) order = [];
  order = order.filter(id => BAR_DEFAULT.includes(id));
  for (const id of BAR_DEFAULT) if (!order.includes(id)) order.push(id);
  return order;
}
function loadBarHidden(){
  let hidden;
  try { hidden = JSON.parse(store.get('barHidden', '') || 'null'); } catch (_) { hidden = null; }
  if (!Array.isArray(hidden)) hidden = [];
  // bMore can never be hidden: it is the recovery path into settings
  return hidden.filter(id => BAR_DEFAULT.includes(id) && id !== 'bMore');
}
let barOrder = loadBarOrder();
let barHidden = loadBarHidden();
function saveBar(){
  store.set('barOrder', JSON.stringify(barOrder));
  store.set('barHidden', JSON.stringify(barHidden));
}
function paintBar(){
  const bar = $('bar');
  for (const id of barOrder) { const b = $(id); if (b) bar.appendChild(b); }   // move keeps listeners
  for (const id of barOrder) {
    const b = $(id); if (!b) continue;
    b.classList.toggle('bar-hidden', id !== 'bMore' && barHidden.includes(id));
  }
}
function paintBarCust(){
  const box = $('barCust');
  if (!box) return;
  box.textContent = '';
  barOrder.forEach((id, i) => {
    const row = document.createElement('div');
    row.className = 'strow';
    const nm = document.createElement('div');
    nm.className = 'nm';
    nm.textContent = i18nT(BAR_LABELS[id], id);
    row.appendChild(nm);
    const up = document.createElement('button');
    up.textContent = '↑'; up.setAttribute('aria-label', 'Move ' + nm.textContent + ' up');
    up.disabled = i === 0;
    up.addEventListener('click', () => {
      [barOrder[i - 1], barOrder[i]] = [barOrder[i], barOrder[i - 1]];
      saveBar(); paintBar(); paintBarCust();
    });
    const down = document.createElement('button');
    down.textContent = '↓'; down.setAttribute('aria-label', 'Move ' + nm.textContent + ' down');
    down.disabled = i === barOrder.length - 1;
    down.addEventListener('click', () => {
      [barOrder[i + 1], barOrder[i]] = [barOrder[i], barOrder[i + 1]];
      saveBar(); paintBar(); paintBarCust();
    });
    const cb = document.createElement('div');
    cb.className = 'check';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !(id !== 'bMore' && barHidden.includes(id));
    input.disabled = id === 'bMore';   // at least one launcher stays reachable
    input.setAttribute('aria-label', 'Show ' + nm.textContent);
    input.addEventListener('change', e => {
      barHidden = barHidden.filter(x => x !== id);
      if (!e.target.checked) barHidden.push(id);
      saveBar(); paintBar();
    });
    cb.appendChild(input);
    row.appendChild(up); row.appendChild(down); row.appendChild(cb);
    box.appendChild(row);
  });
}
$('barReset').addEventListener('click', () => {
  barOrder = BAR_DEFAULT.slice();
  barHidden = [];
  saveBar(); paintBar(); paintBarCust();
  status('Toolbar restored');
});
// hide by long-pressing a toolbar button, with an undo toast (#68)
let barLongFired = false;
$('bar').addEventListener('click', e => {
  if (barLongFired) { e.stopPropagation(); e.preventDefault(); barLongFired = false; }
}, true);
for (const id of BAR_DEFAULT) {
  const b = $(id);
  let lp = null;
  b.addEventListener('pointerdown', () => {
    lp = setTimeout(() => {
      if (id === 'bMore') return;             // always keep a way back
      barLongFired = true;
      barHidden.push(id);
      saveBar(); paintBar();
      toast(i18nT(BAR_LABELS[id], id) + ' hidden', {
        label: 'Undo',
        fn: () => { barHidden = barHidden.filter(x => x !== id); saveBar(); paintBar(); },
      });
    }, 600);
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(ev =>
    b.addEventListener(ev, () => clearTimeout(lp)));
}
paintBar();

applyInsets();
if (!audioOnlyOn()) connect();   // audio-only boot skips the framebuffer (#98); exiting the mode calls connect()

/* ------------------------------------------------------------------ *
 *  Accessible confirm dialog (#121): every sensitive action names its *
 *  exact target; destructive buttons are never primary; Escape cancels,*
 *  Enter confirms; focus is moved in and restored.                    *
 * ------------------------------------------------------------------ */
let confirmResolve = null, confirmLastFocus = null;
function confirmAction(opts){
  opts = opts || {};
  // never render a blank dialog: title/body always resolve to something
  const title = opts.title || i18nT('confirm.title', 'Are you sure?');
  const body = opts.body || '';
  $('confirmTitle').textContent = title;
  $('confirmBody').textContent = body;
  $('confirmTitle').hidden = !title;
  $('confirmBody').hidden = !body;
  const ok = $('confirmOk');
  ok.textContent = opts.confirmLabel || i18nT('btn.confirm', 'Confirm');
  // destructive default buttons are NOT primary (#121): plain styling, red hint via aria
  ok.classList.remove('primary');
  ok.setAttribute('aria-label', (opts.danger ? i18nT('confirm.dangerAria', 'Destructive action') + ': ' : '') + ok.textContent);
  $('confirmScrim').hidden = false;
  confirmLastFocus = document.activeElement;
  $('confirmCancel').focus();
  return new Promise(res => { confirmResolve = res; });
}
function confirmFinish(v){
  $('confirmScrim').hidden = true;
  const r = confirmResolve;
  confirmResolve = null;
  if (confirmLastFocus && confirmLastFocus.focus) try { confirmLastFocus.focus(); } catch (_) {}
  if (r) r(v);
}
$('confirmOk').addEventListener('click', () => confirmFinish(true));
$('confirmCancel').addEventListener('click', () => confirmFinish(false));
$('confirmScrim').addEventListener('click', e => { if (e.target === $('confirmScrim')) confirmFinish(false); });
document.addEventListener('keydown', e => {
  if ($('confirmScrim').hidden || !confirmResolve) return;
  if (e.key === 'Enter') { e.preventDefault(); confirmFinish(true); }
  if (e.key === 'Escape') { e.preventDefault(); confirmFinish(false); }
});

/* Wrap the highest-risk existing actions with the confirm dialog (#121):
 * package install/remove/update name the exact package; the destructive
 * button is non-primary. */
const _storeOp = storeOp;
storeOp = async function (op, pkg){
  if (op === 'install' || op === 'remove' || op === 'update') {
    const verb = { install: i18nT('sec.confirmInstall', 'Install'), remove: i18nT('sec.confirmRemove', 'Remove'), update: i18nT('sec.confirmUpdate', 'Update') }[op];
    const ok = await confirmAction({
      title: verb + ' ' + pkg,
      body: i18nT('sec.confirmStoreBody', op === 'install'
        ? 'The package will be installed system-wide on the desktop with root privileges.'
        : 'This changes installed software on the desktop.'),
      confirmLabel: verb,
      danger: op !== 'update',
    });
    if (!ok) return { ok: false, cancelled: true };
  }
  return _storeOp(op, pkg);
};

/* ------------------------------------------------------------------ *
 *  Security & account sheet (#109-#119, #123)                         *
 * ------------------------------------------------------------------ */
let secCaps = { admin: false, operate: true };
function secUser(){ return secCaps.user || ''; }

async function loadSec(){
  try {
    const ac = await (await fetch('/api/account')).json();
    secCaps.admin = !!(ac.caps && ac.caps.admin);
    secCaps.user = ac.user;
    $('secPwSave').disabled = false;
    // 2FA status
    const t2 = await (await fetch('/api/2fa')).json();
    $('secTotpStatus').textContent = t2.enabled
      ? i18nT('sec.totpOn', '2FA is on for this account.') + (t2.required ? ' ' + i18nT('sec.totpEnforced', '(enforced on this server)') : '')
      : i18nT('sec.totpOff', '2FA is off — add a second factor for the strongest protection.');
    $('secTotpOn').hidden = !t2.enabled;
    $('secTotpSetup').hidden = !!t2.enabled;
    $('secRecovery').textContent = t2.enabled && t2.recovery_left !== undefined
      ? i18nT('sec.recoveryLeft', 'recovery codes left') + ': ' + t2.recovery_left
      : '';
    // passkeys (only when the server enables the feature)
    $('secPasskeys').hidden = !ac.passkeys;
    if (ac.passkeys) paintPasskeys(ac.passkeys);
    // notifications (#123)
    const nf = await (await fetch('/api/notify')).json();
    paintNotify(nf);
    paintAlerts(nf);
    // sessions (#114)
    await loadSessions();
    // admin panel (#111/#112/#113)
    $('secAdmin').hidden = !secCaps.admin;
    if (secCaps.admin) { await loadUsers(); await loadAudit(); await loadKeys(); }
  } catch (e) { status(i18nT('sec.loadFail', 'Could not load security settings'), true); }
}

function paintAlerts(nf){
  const box = $('secAlerts');
  const items = nf.notifications || [];
  if (!items.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = '';
  items.slice(-3).forEach(n => {
    const d = document.createElement('div');
    d.className = 'stat';
    const why = { 'new-device': i18nT('sec.newDevice', 'New device signed in'), 'new-ip': i18nT('sec.newIp', 'Sign-in from a new network'), 'test': i18nT('sec.testAlert', 'Test alert') }[n.reason] || n.reason;
    d.textContent = `⚠ ${why} — ${n.device || ''} · ${n.ip_prefix || ''} · ${fmtWhen(n.ts)}`;
    box.appendChild(d);
  });
}

function paintNotify(nf){
  const box = $('secNotify');
  box.innerHTML = '';
  const add = (l, v) => {
    const d = document.createElement('div');
    d.className = 'stat';
    d.innerHTML = `<span class="lbl">${l}</span><span class="val" style="flex:1;text-align:left"></span>`;
    d.querySelector('.val').textContent = v;
    box.appendChild(d);
  };
  add(i18nT('sec.failed24h', 'Failed logins (24 h)'), String(nf.failed_24h || 0));
  const dev = (nf.devices || []).length;
  add(i18nT('sec.knownDevices', 'Known devices'), String(dev));
  const tb = document.createElement('button');
  tb.textContent = i18nT('sec.notifyTest', 'Send a test alert');
  tb.addEventListener('click', async () => {
    try { await fetch('/api/notify/test', { method: 'POST' }); toast(i18nT('sec.notifyTestSent', 'Test alert queued')); } catch (_) {}
  });
  box.appendChild(tb);
}

async function loadSessions(){
  try {
    const j = await (await fetch('/api/sessions')).json();
    const box = $('secSessions');
    box.innerHTML = '';
    (j.sessions || []).forEach(s => {
      const d = document.createElement('div');
      d.className = 'stat';
      const cur = s.current ? ' · ' + i18nT('sec.thisDevice', 'this device') : '';
      d.innerHTML = `<span class="lbl" style="max-width:45%"></span><span class="val" style="flex:1;text-align:left"></span>`;
      d.querySelector('.lbl').textContent = (s.label || s.id) + cur;
      d.querySelector('.val').textContent = `${s.ip || ''} · ${i18nT('sec.lastSeen', 'last seen')} ${fmtWhen(s.last_seen)}`;
      if (!s.current) {
        const b = document.createElement('button');
        b.textContent = i18nT('sec.revoke', 'Revoke');
        b.setAttribute('aria-label', i18nT('sec.revokeAria', 'Revoke device') + ' ' + (s.label || s.id));
        b.addEventListener('click', async () => {
          const ok = await confirmAction({
            title: i18nT('sec.revokeTitle', 'Sign out device?'),
            body: (s.label || s.id) + ' · ' + (s.ip || ''),
            confirmLabel: i18nT('sec.revoke', 'Revoke'),
            danger: true,
          });
          if (!ok) return;
          await fetch('/api/sessions/revoke', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: s.id }) });
          loadSessions();
        });
        d.appendChild(b);
      }
      box.appendChild(d);
    });
  } catch (_) {}
}

$('secSessionsRefresh').addEventListener('click', loadSessions);
$('secRevokeAll').addEventListener('click', async () => {
  const ok = await confirmAction({
    title: i18nT('sec.revokeAllTitle', 'Sign out all other devices?'),
    body: i18nT('sec.revokeAllBody', 'Every signed-in device except this one will need to log in again.'),
    confirmLabel: i18nT('sec.revokeAll', 'Revoke other devices'),
    danger: true,
  });
  if (!ok) return;
  await fetch('/api/sessions/revoke-all', { method: 'POST' });
  loadSessions();
  toast(i18nT('sec.revoked', 'Other devices signed out'));
});

$('secPwSave').addEventListener('click', async () => {
  const cur = $('secPwCur').value, nw = $('secPwNew').value;
  if (!cur || nw.length < 8) { status(i18nT('sec.pwShort', 'The new password needs 8+ characters'), true); return; }
  try {
    const j = await (await fetch('/api/account/password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current: cur, new: nw, keep_sessions: $('secPwKeep').checked }),
    })).json();
    if (j.ok) {
      $('secPwCur').value = $('secPwNew').value = '';
      toast(i18nT('sec.pwChanged', 'Password changed'));
      status(i18nT('sec.pwChanged', 'Password changed'));
    } else status(j.error || i18nT('sec.pwFail', 'Could not change the password'), true);
  } catch (_) { status(i18nT('sec.pwFail', 'Could not change the password'), true); }
});

$('bSec').addEventListener('click', () => { openSheet($('shSec')); loadSec(); });

/* --- two-factor flows (#109) --- */
async function totpSetup(){
  try {
    const j = await (await fetch('/api/2fa/setup', { method: 'POST' })).json();
    if (!j.ok) { status(j.error || 'setup failed', true); return; }
    $('secTotpSetup').hidden = false;
    $('secTotpSecret').textContent = j.secret;
    // QR only when the server can render it (#109); the manual secret is always shown
    const qr = await fetch('/api/2fa/qr');
    if (qr.ok) {
      const blob = await qr.blob();
      $('secTotpQr').src = URL.createObjectURL(blob);
    } else $('secTotpQr').hidden = true;
    $('secTotpCode').focus();
  } catch (_) { status(i18nT('sec.loadFail', 'Could not load security settings'), true); }
}
$('secTotpEnable').addEventListener('click', async () => {
  const code = $('secTotpCode').value.trim();
  if (!code) return;
  try {
    const j = await (await fetch('/api/2fa/enable', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
    })).json();
    if (!j.ok) { status(j.error || 'invalid code', true); return; }
    $('secTotpSetup').hidden = true;
    const codes = (j.recovery_codes || []).join('\n');
    $('secRecovery').textContent = i18nT('sec.recoveryShow', 'Save these one-time recovery codes now — they are shown only once:') + '\n' + codes;
    toast(i18nT('sec.totpEnabled', '2FA enabled'));
    loadSec();
  } catch (_) { status('2FA enable failed', true); }
});
$('secTotpOff').addEventListener('click', async () => {
  const pw = prompt(i18nT('sec.curPw', 'Current password') + ':') || '';
  if (!pw) return;
  const ok = await confirmAction({
    title: i18nT('sec.totpDisableTitle', 'Disable two-factor?'),
    body: i18nT('sec.totpDisableBody', 'Password-only sign-in is weaker; you can re-enrol any time.'),
    confirmLabel: i18nT('sec.totpDisable', 'Disable 2FA'),
    danger: true,
  });
  if (!ok) return;
  const j = await (await fetch('/api/2fa/disable', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw }),
  })).json();
  if (!j.ok) { status(j.error || 'failed', true); return; }
  toast(i18nT('sec.totpDisabledMsg', '2FA disabled'));
  loadSec();
});
$('secTotpRegen').addEventListener('click', async () => {
  const j = await (await fetch('/api/2fa/recovery/regenerate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: prompt(i18nT('sec.totpCode', '6-digit code')) || '' }),
  })).json();
  if (!j.ok) { status(j.error || 'failed', true); return; }
  $('secRecovery').textContent = i18nT('sec.recoveryShow', 'Save these one-time recovery codes now — they are shown only once:') + '\n' + (j.recovery_codes || []).join('\n');
});
// kick off enrollment straight from the status row
document.querySelector('#secTotpStatus').closest('.row').insertAdjacentHTML(
  'afterend',
  '<div class="row"><button id="secTotpStart" data-i18n="sec.totpStart">Start 2FA enrollment</button></div>'
);
$('secTotpStart').addEventListener('click', totpSetup);

/* --- passkeys (#110): only reachable when the server enables them --- */
function paintPasskeys(creds){
  const box = $('secPasskeyList');
  box.innerHTML = '';
  (creds || []).forEach(c => {
    const d = document.createElement('div');
    d.className = 'stat';
    d.innerHTML = '<span class="lbl"></span><span class="val" style="flex:1;text-align:left"></span>';
    d.querySelector('.lbl').textContent = c.label || 'passkey';
    d.querySelector('.val').textContent = i18nT('sec.lastUsed', 'last used') + ' ' + fmtWhen(c.last_used);
    const b = document.createElement('button');
    b.textContent = i18nT('sec.passkeyRemove', 'Remove');
    b.addEventListener('click', async () => {
      const ok = await confirmAction({
        title: i18nT('sec.passkeyRemoveTitle', 'Remove passkey?'),
        body: (c.label || 'passkey') + ' — ' + i18nT('sec.passkeyRemoveBody', 'password sign-in keeps working.'),
        confirmLabel: i18nT('sec.passkeyRemove', 'Remove'),
        danger: true,
      });
      if (!ok) return;
      await fetch('/api/webauthn/credentials?id=' + encodeURIComponent(c.id), { method: 'DELETE' });
      loadSec();
    });
    d.appendChild(b);
    box.appendChild(d);
  });
}
$('secPasskeyAdd').addEventListener('click', async () => {
  try {
    const opts = await (await fetch('/api/webauthn/register/options', { method: 'POST' })).json();
    if (!opts.ok) { status(opts.error, true); return; }
    const label = prompt(i18nT('sec.passkeyLabel', 'Name this device (e.g. Pixel 9)')) || 'passkey';
    // the browser ceremony (WebAuthn API) completes only when the optional
    // server backend is installed; errors surface verbatim
    const cred = await navigator.credentials.create({ publicKey: opts });
    const j = await (await fetch('/api/webauthn/register/verify', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ credential: cred, label }),
    })).json();
    if (!j.ok) { status(j.error || 'registration failed', true); return; }
    toast(i18nT('sec.passkeyAdded', 'Passkey registered'));
    loadSec();
  } catch (e) { status(String(e.message || e).slice(0, 120), true); }
});

/* --- admin panel (#111/#112/#113) --- */
async function loadUsers(){
  try {
    const j = await (await fetch('/api/admin/users')).json();
    const box = $('admUsers');
    box.innerHTML = '';
    (j.users || []).forEach(u => {
      const d = document.createElement('div');
      d.className = 'stat';
      const sel = document.createElement('select');
      sel.setAttribute('aria-label', i18nT('sec.roleAria', 'Role of') + ' ' + u.username);
      [['viewer', 'Viewer'], ['operator', 'Operator'], ['admin', 'Admin']].forEach(([v, t]) => {
        const o = document.createElement('option');
        o.value = v; o.textContent = t;
        if (u.role === v) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('change', () => admUser({ action: 'role', username: u.username, role: sel.value }));
      d.innerHTML = '<span class="lbl" style="max-width:38%"></span>';
      d.querySelector('.lbl').textContent = u.username + (u.default ? ' ★' : '') + (u.status !== 'active' ? ' (' + i18nT('sec.disabled', 'disabled') + ')' : '');
      const acts = document.createElement('span');
      acts.className = 'val';
      acts.style.cssText = 'flex:1;text-align:right;display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap';
      if (u.status === 'active') {
        const b = document.createElement('button');
        b.textContent = i18nT('sec.disable', 'Disable');
        b.addEventListener('click', () => admUser({ action: 'status', username: u.username, status: 'disabled' }, true));
        acts.appendChild(b);
      } else {
        const b = document.createElement('button');
        b.textContent = i18nT('sec.enable', 'Enable');
        b.addEventListener('click', () => admUser({ action: 'status', username: u.username, status: 'active' }));
        acts.appendChild(b);
      }
      const rp = document.createElement('button');
      rp.textContent = i18nT('sec.resetPw', 'Reset password');
      rp.addEventListener('click', () => {
        const pw = prompt(i18nT('sec.newPwFor', 'New password for') + ' ' + u.username + ' (8+ chars):');
        if (pw && pw.length >= 8) admUser({ action: 'password', username: u.username, password: pw }, true);
      });
      acts.appendChild(rp);
      if (!u.default) {
        const del = document.createElement('button');
        del.textContent = i18nT('sec.delete', 'Delete');
        del.setAttribute('aria-label', i18nT('sec.deleteAria', 'Delete account') + ' ' + u.username);
        del.addEventListener('click', async () => {
          const ok = await confirmAction({
            title: i18nT('sec.deleteTitle', 'Delete account?'),
            body: u.username + ' — ' + i18nT('sec.deleteBody', 'their sessions are signed out immediately; their files stay on disk.'),
            confirmLabel: i18nT('sec.delete', 'Delete'),
            danger: true,
          });
          if (ok) admUser({ action: 'delete', username: u.username }, true);
        });
        acts.appendChild(del);
      }
      d.appendChild(sel);
      d.appendChild(acts);
      box.appendChild(d);
    });
  } catch (_) {}
}
async function admUser(payload, confirmFirst){
  if (confirmFirst) {
    const ok = await confirmAction({
      title: i18nT('sec.adminAction', 'Apply this admin action?'),
      body: payload.action + ': ' + payload.username,
      confirmLabel: i18nT('btn.confirm', 'Confirm'),
      danger: payload.action === 'delete' || payload.action === 'status',
    });
    if (!ok) return;
  }
  try {
    const j = await (await fetch('/api/admin/user', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    })).json();
    if (!j.ok) status(j.error || 'failed', true);
    else toast(payload.username + ': ' + payload.action);
  } catch (_) {}
  loadUsers();
}
$('admRefresh').addEventListener('click', loadUsers);
$('admCreate').addEventListener('click', () => {
  const username = $('admNewName').value.trim(), pw = $('admNewPw').value;
  if (!username || pw.length < 8) { status(i18nT('sec.pwShort', 'The new password needs 8+ characters'), true); return; }
  admUser({ action: 'create', username, password: pw, role: $('admNewRole').value });
  $('admNewName').value = ''; $('admNewPw').value = '';
});

async function loadAudit(){
  try {
    const kind = $('admAuditKind').value, q = $('admAuditQ').value.trim();
    const j = await (await fetch('/api/admin/audit?limit=60' + (kind ? '&kind=' + kind : '') + (q ? '&q=' + encodeURIComponent(q) : ''))).json();
    const box = $('admAudit');
    box.innerHTML = '';
    (j.events || []).slice().reverse().forEach(ev => {
      const d = document.createElement('div');
      d.className = 'stat';
      d.innerHTML = '<span class="lbl" style="max-width:30%"></span><span class="val" style="flex:1;text-align:left;word-break:break-all"></span>';
      d.querySelector('.lbl').textContent = `${ev.kind}/${ev.action}`;
      d.querySelector('.val').textContent = `${ev.user}@${ev.ip} → ${ev.target || ev.path || ''} ${ev.result} · ${fmtWhen(ev.ts)}`;
      box.appendChild(d);
    });
    const v = await (await fetch('/api/admin/audit/verify')).json();
    $('admAuditChain').textContent = v.chain_ok
      ? i18nT('sec.chainOk', 'Audit chain verified') + ` (${v.checked} records)`
      : i18nT('sec.chainBad', 'AUDIT CHAIN BROKEN — records were edited or removed') + ` (first bad: ${v.first_bad})`;
  } catch (_) {}
}
$('admAuditRefresh').addEventListener('click', loadAudit);
$('admAuditKind').addEventListener('change', loadAudit);

async function loadKeys(){
  try {
    const j = await (await fetch('/api/admin/keys')).json();
    $('admKeyInfo').textContent = i18nT('sec.keyVersion', 'active signing key version') + ': v' + j.key_version;
  } catch (_) {}
}
$('admKeyRotate').addEventListener('click', async () => {
  const ok = await confirmAction({
    title: i18nT('sec.keyRotateTitle', 'Rotate the signing key?'),
    body: i18nT('sec.keyRotateBody', 'All signed-in devices are signed out (5-minute grace for reconnects).'),
    confirmLabel: i18nT('sec.keyRotate', 'Rotate key'),
    danger: true,
  });
  if (!ok) return;
  await fetch('/api/admin/keys/rotate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
  loadKeys();
});
