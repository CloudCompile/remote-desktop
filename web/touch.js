// Mobile touch layer for the remote desktop — TRACKPAD ONLY.
//
// The whole screen is one big trackpad. The pointer (cursor ring) moves
// relatively; taps click at the pointer, never where your finger is.
//
// Gestures:
//   1-finger drag        move pointer (with acceleration)
//   tap                  left click at pointer
//   double-tap + drag    left-click-drag (grab windows, select text)
//   long press           right click at pointer
//   2-finger tap         right click at pointer
//   2-finger drag        scroll
//   3-finger tap         middle click at pointer
import DOMKeyTable from './core/input/domkeytable.js';

export function initTouch(app) {
  let rfb = app.rfb;                 // mutable: replaced on auto-reconnect
  const screenEl = app.screenEl;
  if (!rfb || !screenEl) return;

  // detach noVNC's own gesture handler; we own all touch input
  try { if (rfb._gestures) rfb._gestures.detach(); } catch (_) {}

  const defaults = { scrollInvert: false, pointerSpeed: 1.3, accel: true,
                     longPressMs: 450, haptics: true, extraKeys: false,
                     tapClick: true, tapDrag: true };
  let settings = defaults;
  try { settings = { ...defaults, ...(JSON.parse(localStorage.getItem('touchSettings') || '{}')) }; } catch (_) {}
  const saveSettings = () => { try { localStorage.setItem('touchSettings', JSON.stringify(settings)); } catch (_) {} };

  /* ---------------- visuals ---------------- */
  const style = document.createElement('style');
  style.textContent = `
    html,body,#app,#screen{touch-action:none!important;overscroll-behavior:none;
      user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}
    body{overflow:hidden;height:100dvh}
    #trackpadCursor{position:fixed;pointer-events:none;z-index:99999;
      width:26px;height:26px;margin:-13px 0 0 -13px;will-change:transform}
    #trackpadCursor .ring{position:absolute;inset:0;border:2px solid rgba(0,255,136,.9);
      border-radius:50%;transition:transform .1s,border-color .1s}
    #trackpadCursor .dot{position:absolute;left:50%;top:50%;width:4px;height:4px;margin:-2px 0 0 -2px;
      border-radius:50%;background:rgba(0,255,136,.95)}
    #trackpadCursor.pressed .ring{transform:scale(.7);border-color:rgba(255,255,255,.95)}
    #trackpadCursor.pressed .dot{background:#fff}
    .touchRipple{position:fixed;pointer-events:none;border:2px solid rgba(255,255,255,.8);
      border-radius:50%;transform:translate(-50%,-50%);animation:tRipple .3s ease-out forwards;z-index:100000}
    @keyframes tRipple{0%{width:6px;height:6px;opacity:.9}100%{width:40px;height:40px;opacity:0}}
    #extraKeys{position:fixed;left:0;right:0;bottom:0;z-index:110;padding:8px 8px calc(10px + env(safe-area-inset-bottom,0px));
      background:rgba(14,14,14,.97);box-shadow:0 -6px 24px rgba(0,0,0,.5);display:none}
    #extraKeys .ekrow{display:flex;gap:6px;justify-content:center;margin-bottom:6px;flex-wrap:wrap}
    #extraKeys button{min-width:44px;min-height:44px;border:0;border-radius:8px;background:#333;color:#fff;
      font-size:14px;padding:0 8px;-webkit-tap-highlight-color:transparent}
    #extraKeys button.mod-on{background:#2a6}`;
  document.head.appendChild(style);

  const cursor = document.createElement('div');
  cursor.id = 'trackpadCursor';
  cursor.innerHTML = '<div class="ring"></div><div class="dot"></div>';
  document.body.appendChild(cursor);
  const showCursor = () => { cursor.style.display = 'block'; paintCursor(); };
  const hideCursor = () => { cursor.style.display = 'none'; };
  const paintCursor = () => { cursor.style.transform = `translate(${cursorPos.sx}px,${cursorPos.sy}px)`; };
  const setPressed = (on) => cursor.classList.toggle('pressed', on);

  const getRect = () => screenEl.getBoundingClientRect();
  const ripple = (x, y) => {
    const d = document.createElement('div');
    d.className = 'touchRipple';
    d.style.left = x + 'px'; d.style.top = y + 'px';
    document.body.appendChild(d);
    setTimeout(() => d.remove(), 320);
  };
  const haptic = (ms) => { if (settings.haptics && navigator.vibrate) try { navigator.vibrate(ms); } catch (_) {} };

  /* ---------------- pointer plumbing ---------------- */
  // rAF coalescing: at most one pointer event per frame
  let pending = null, rafId = null;
  function rafSend(x, y, mask) {
    pending = { x, y, mask };
    if (!rafId) rafId = requestAnimationFrame(() => {
      if (pending) { const p = pending; pending = null; try { rfb._sendMouse(p.x, p.y, p.mask); } catch (_) {} }
      rafId = null;
    });
  }
  const sendPointer = (x, y, mask) => { try { rfb._sendMouse(x, y, mask); } catch (_) {} };

  /* ---------------- trackpad state ---------------- */
  // The cursor lives in REMOTE framebuffer coordinates (stable across zoom /
  // resizes). noVNC's _sendMouse() takes SCREEN coordinates (it applies
  // display.absX internally), so convert on the way out:
  //   screen = (remote - viewportLoc) * scale
  let cursorPos = { rx: 0, ry: 0 };            // remote fb coords
  let fbSize = { w: 0, h: 0 };

  function displayInfo() {
    try {
      const d = rfb._display;
      return { d, scale: d._scale || 1, vx: d._viewportLoc.x || 0, vy: d._viewportLoc.y || 0,
               vw: d._viewportLoc.w || 0, vh: d._viewportLoc.h || 0 };
    } catch (_) { return { scale: 1, vx: 0, vy: 0, vw: 0, vh: 0 }; }
  }
  function remoteToScreen(rx, ry) {
    const i = displayInfo();
    return { x: (rx - i.vx) * i.scale, y: (ry - i.vy) * i.scale };
  }
  function screenToRemote(sx, sy) {
    const i = displayInfo();
    return { x: sx / i.scale + i.vx, y: sy / i.scale + i.vy };
  }
  function sendCursor(mask) {
    const s = remoteToScreen(cursorPos.rx, cursorPos.ry);
    sendPointer(s.x, s.y, mask);
  }
  function rafSendCursor(mask) {
    const s = remoteToScreen(cursorPos.rx, cursorPos.ry);
    rafSend(s.x, s.y, mask);
  }
  function clampCursor() {
    // read the LIVE framebuffer size: the remote desktop can resize at any
    // moment (matchDesktop on connect/rotate) and a cached size would fence
    // the cursor into a stale region
    refreshFbSize();
    cursorPos.rx = Math.max(0, Math.min(fbSize.w, cursorPos.rx));
    cursorPos.ry = Math.max(0, Math.min(fbSize.h, cursorPos.ry));
    const r = getRect();
    const s = remoteToScreen(cursorPos.rx, cursorPos.ry);
    cursorPos.sx = r.left + s.x;
    cursorPos.sy = r.top + s.y;
    paintCursor();
  }
  function centerCursor() {
    try {
      const d = rfb._display;
      // center of the visible viewport, in remote coords
      const c = screenToRemote((d._viewportLoc.w || getRect().width) / 2,
                               (d._viewportLoc.h || getRect().height) / 2);
      cursorPos.rx = Math.round(c.x);
      cursorPos.ry = Math.round(c.y);
    } catch (_) {
      const r = getRect();
      cursorPos.rx = r.width / 2; cursorPos.ry = r.height / 2;
    }
    clampCursor();
  }
  function refreshFbSize() {
    try { fbSize.w = rfb._display._fbWidth; fbSize.h = rfb._display._fbHeight; }
    catch (_) { fbSize = { w: getRect().width, h: getRect().height }; }
  }

  let touches = new Map();
  let dragging = false;          // button 1 held (from double-tap-drag)
  let dragArmed = 0;             // timestamp of last tap (for double-tap-drag)
  let scrollAccum = 0;
  const SCROLL_PX = 34;          // px of finger travel per wheel notch
  const TAP_MS = 260, TAP_MOVE_PX = 14;

  function clickAt(buttonMask) {
    sendCursor(buttonMask);
    setPressed(true);
    setTimeout(() => { sendCursor(0); setPressed(dragging); }, 45);
    ripple(cursorPos.sx, cursorPos.sy);
    haptic(6);
  }

  // ignore palm touches at the screen edge or with huge contact radii
  function isEdgePalm(t) {
    const w = innerWidth, h = innerHeight, landscape = w > h;
    if ((landscape ? w - t.clientX : h - t.clientY) < 16) return true;
    if (typeof t.radiusX === 'number' && (t.radiusX > 30 || t.radiusY > 30)) return true;
    return false;
  }

  screenEl.addEventListener('touchstart', (e) => {
    e.preventDefault();
    showCursor();
    for (const t of e.changedTouches) {
      if (isEdgePalm(t)) continue;
      const r = getRect();
      const st = { id: t.identifier, x: t.clientX, y: t.clientY,
                   sx: t.clientX, sy: t.clientY, lastX: t.clientX, lastY: t.clientY,
                   lastT: performance.now(), t0: Date.now(), vel: 0,
                   moved: false, longTimer: null };
      touches.set(t.identifier, st);

      if (touches.size === 1) {
        // double-tap-drag: a new finger right after a tap starts a left-drag
        if (settings.tapDrag && Date.now() - dragArmed < 300) {
          if (!dragging) {
            dragging = true;
            setPressed(true);
            sendCursor(0x1);
            haptic(10);
          }
        }
        if (!dragging) {
          st.longTimer = setTimeout(() => {
            if (st.moved || touches.size > 1) return;
            clickAt(0x4);                                   // long press = right click
            st.longFired = true;
          }, settings.longPressMs);
        }
      } else {
        touches.forEach(s => { if (s.longTimer) { clearTimeout(s.longTimer); s.longTimer = null; } });
        scrollAccum = 0;
      }
    }
  }, { passive: false });

  screenEl.addEventListener('touchmove', (e) => {
    e.preventDefault();
    let needClamp = false;
    for (const t of e.changedTouches) {
      const st = touches.get(t.identifier);
      if (!st) continue;
      const now = performance.now();
      const dx = t.clientX - st.lastX, dy = t.clientY - st.lastY;
      const dt = Math.max(1, now - st.lastT);
      const inst = Math.hypot(dx, dy) / dt;                 // px/ms
      st.vel = st.vel * 0.65 + inst * 0.35;                 // smoothed speed
      st.lastX = t.clientX; st.lastY = t.clientY; st.lastT = now;
      if (!st.moved && Math.hypot(t.clientX - st.sx, t.clientY - st.sy) > TAP_MOVE_PX) st.moved = true;
      if (st.longTimer && st.moved) { clearTimeout(st.longTimer); st.longTimer = null; }

      if (touches.size === 1 && st.moved) {
        // ---- relative pointer with acceleration ----
        let mult = settings.pointerSpeed || 1.3;
        if (settings.accel) {
          // slow finger = precise (x0.55), fast flick = up to ~x3
          mult *= 0.55 + Math.min(st.vel * 1.1, 2.45);
        }
        // finger moves in screen px; cursor lives in remote px
        const i = displayInfo();
        const toRemote = 1 / (i.scale || 1);
        cursorPos.rx += dx * mult * toRemote;
        cursorPos.ry += dy * mult * toRemote;
        needClamp = true;
        rafSendCursor(dragging ? 0x1 : 0);
      }
    }
    if (needClamp) clampCursor();

    if (touches.size === 2) {
      const arr = [...touches.values()];
      const midY = (arr[0].y + arr[1].y) / 2;
      // ---- two-finger scroll ----
      const prevY = touches._scrollY ?? midY;
      scrollAccum += midY - prevY;
      touches._scrollY = midY;
      while (Math.abs(scrollAccum) >= SCROLL_PX) {
        const down = scrollAccum > 0;
        scrollAccum -= down ? SCROLL_PX : -SCROLL_PX;
        const inv = settings.scrollInvert;
        const mask = (down !== inv) ? 0x10 : 0x08;       // fingers down -> wheel down (invert flips)
        sendCursor(mask);
        setTimeout(() => sendCursor(0), 25);
        haptic(4);
      }
    }
  }, { passive: false });

  screenEl.addEventListener('touchend', (e) => {
    e.preventDefault();
    const lifted = [];
    for (const t of e.changedTouches) {
      const st = touches.get(t.identifier);
      if (!st) continue;
      lifted.push(st);
      if (st.longTimer) { clearTimeout(st.longTimer); st.longTimer = null; }
      touches.delete(t.identifier);
    }
    const dur = Date.now() - (lifted[0]?.t0 || Date.now());
    const moved = lifted.some(s => s.moved);

    if (touches.size === 0 && !dragging) {
      if (!moved && dur < TAP_MS && !lifted.some(s => s.longFired)) {
        if (lifted.length === 1 && settings.tapClick) {
          clickAt(0x1);                                      // tap = left click
          dragArmed = Date.now();                            // arm double-tap-drag
        } else if (lifted.length === 2) {
          clickAt(0x4);                                     // 2-finger tap = right click
        } else if (lifted.length >= 3) {
          clickAt(0x2);                                      // 3-finger tap = middle click
        }
      } else if (!moved) {
        dragArmed = 0;
      }
      touches._scrollY = undefined;
    }

    if (dragging && touches.size === 0) {
      // end of double-tap-drag
      dragging = false;
      dragArmed = 0;
      setPressed(false);
      sendCursor(0);
      haptic(8);
    }
    if (touches.size === 0 && rafId) { cancelAnimationFrame(rafId); rafId = null; pending = null; }
  }, { passive: false });

  screenEl.addEventListener('touchcancel', (e) => {
    e.preventDefault();
    touches.forEach(st => { if (st.longTimer) clearTimeout(st.longTimer); });
    touches.clear();
    if (dragging) { dragging = false; setPressed(false); }
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; pending = null; }
    sendCursor(0);
  }, { passive: false });

  /* ---------------- keyboard bridge (mobile IME) ---------------- */
  let kb = document.getElementById('kb');
  if (!kb) {
    kb = document.createElement('textarea');
    kb.id = 'kb';
    kb.autocomplete = 'off'; kb.autocapitalize = 'none'; kb.spellcheck = false;
    kb.setAttribute('inputmode', 'text');
    kb.setAttribute('autocorrect', 'off');
    kb.rows = 1;
    kb.style.cssText = 'position:fixed;bottom:-1000px;left:0;width:1px;height:1px;opacity:0;' +
                       'font-size:16px;color:transparent;background:transparent;border:0;';
    document.body.appendChild(kb);
  }
  function openKeyboard() {
    kb.style.opacity = '0.01';
    kb.style.pointerEvents = 'auto';
    kb.focus({ preventScroll: true });
    setTimeout(() => { kb.style.opacity = '0'; kb.style.pointerEvents = 'none'; }, 400);
  }
  if (app.keyboardButton) {
    app.keyboardButton.addEventListener('click', () => { openKeyboard(); showCursor(); });
  } else {
    const b = document.createElement('button');
    b.textContent = '⌨';
    b.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:99999;min-width:48px;min-height:48px;' +
                      'border:0;border-radius:10px;background:rgba(28,28,28,.92);color:#fff;font-size:20px';
    b.addEventListener('click', openKeyboard);
    document.body.appendChild(b);
  }

  const keysymFor = (key) => {
    if (DOMKeyTable[key] && DOMKeyTable[key][0] !== undefined) return DOMKeyTable[key][0];
    if (key.length === 1) return key.charCodeAt(0);
    const specials = { Backspace: 0xff08, Enter: 0xff0d, Tab: 0xff09, Escape: 0xff1b,
                       ArrowLeft: 0xff51, ArrowUp: 0xff52, ArrowRight: 0xff53, ArrowDown: 0xff54 };
    return specials[key] ?? null;
  };
  kb.addEventListener('keydown', (e) => {
    if (!rfb) return;
    const ks = keysymFor(e.key);
    if (ks !== null) { rfb.sendKey(ks, e.code || e.key, true); e.preventDefault(); }
  });
  kb.addEventListener('keyup', (e) => {
    if (!rfb) return;
    const ks = keysymFor(e.key);
    if (ks !== null) rfb.sendKey(ks, e.code || e.key, false);
  });
  kb.addEventListener('input', (e) => {
    if (!rfb) return;
    if (e.inputType === 'deleteContentBackward' && !e.data) {
      rfb.sendKey(0xff08, 'Backspace', true);
      rfb.sendKey(0xff08, 'Backspace', false);
      kb.value = '';
      return;
    }
    if (!e.data) return;
    if (e.data.length > 1) rfb.clipboardPasteFrom(e.data);
    else for (const ch of e.data) {
      const ks = ch.charCodeAt(0);
      rfb.sendKey(ks, ch, true);
      rfb.sendKey(ks, ch, false);
    }
    kb.value = '';
  });
  kb.addEventListener('compositionend', (e) => {
    if (!rfb || !e.data) return;
    rfb.clipboardPasteFrom(e.data);
    kb.value = '';
  });

  /* ---------------- extra keys panel ---------------- */
  const mods = { Ctrl: false, Alt: false, Shift: false, Win: false };
  const locks = { Ctrl: false, Alt: false, Shift: false, Win: false };

  function buildExtraKeys() {
    let c = document.getElementById('extraKeys');
    if (!c) { c = document.createElement('div'); c.id = 'extraKeys'; document.body.appendChild(c); }
    c.textContent = '';
    const rows = [
      ['Esc', 'Tab', 'Ctrl', 'Alt', 'Shift', 'Win'],
      ['Home', 'Up', 'PgUp', 'End', 'Down', 'PgDn', 'Left', 'Right'],
      ['F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12'],
      ['Ctrl+C', 'Ctrl+V', 'Ctrl+A', 'Ctrl+Z', 'Ctrl+W', 'Ctrl+T', 'Alt+Tab'],
    ];
    const KEYMAP = { Esc: 'Escape', Tab: 'Tab', Up: 'ArrowUp', Down: 'ArrowDown', Left: 'ArrowLeft',
                     Right: 'ArrowRight', PgUp: 'PageUp', PgDn: 'PageDn' };
    for (const row of rows) {
      const r = document.createElement('div');
      r.className = 'ekrow';
      for (const key of row) {
        const b = document.createElement('button');
        b.textContent = key;
        const isMod = ['Ctrl', 'Alt', 'Shift', 'Win'].includes(key);
        if (isMod && (mods[key] || locks[key])) b.classList.add('mod-on');
        b.addEventListener('click', () => {
          haptic(4);
          if (isMod) {
            if (locks[key]) { locks[key] = false; mods[key] = false; }
            else if (mods[key]) locks[key] = true;
            else mods[key] = true;
            buildExtraKeys();
            return;
          }
          if (key.includes('+')) {
            const parts = key.split('+');
            const tbl = { Ctrl: 'Control', Alt: 'Alt', Shift: 'Shift', Win: 'Meta' };
            const down = [];
            for (const m of parts.slice(0, -1)) {
              const nm = tbl[m];
              const ksm = (DOMKeyTable[nm] || [])[0];
              if (ksm !== undefined) { rfb.sendKey(ksm, nm, true); down.push(nm); }
            }
            const fk = KEYMAP[parts[parts.length - 1]] || parts[parts.length - 1];
            const fks = (DOMKeyTable[fk] || [])[0] ?? (fk.length === 1 ? fk.charCodeAt(0) : null);
            if (fks !== null) { rfb.sendKey(fks, fk, true); rfb.sendKey(fks, fk, false); }
            for (const m of down.reverse()) {
              const ks = (DOMKeyTable[m] || [])[0];
              if (ks !== undefined) rfb.sendKey(ks, m, false);
            }
          } else {
            const nk = KEYMAP[key] || key;
            const ks = (DOMKeyTable[nk] || [])[0] ?? (nk.length === 1 ? nk.charCodeAt(0) : null);
            if (ks !== null) { rfb.sendKey(ks, nk, true); rfb.sendKey(ks, nk, false); }
          }
          for (const mm of ['Ctrl', 'Alt', 'Shift', 'Win'])
            if (mods[mm] && !locks[mm]) mods[mm] = false;
          buildExtraKeys();
        });
        r.appendChild(b);
      }
      c.appendChild(r);
    }
    const cr = document.createElement('div');
    cr.className = 'ekrow';
    const mk = (label, fn, primary) => {
      const b = document.createElement('button');
      b.textContent = label;
      if (primary) b.classList.add('mod-on');
      b.addEventListener('click', fn);
      cr.appendChild(b);
    };
    mk('Copy', async () => {
      // Ctrl+C in the focused remote app first, so it puts text into the
      // X clipboard, then bring both selections to the phone
      sendShortcut('c');
      setTimeout(async () => {
        try {
          const r = await fetch('/api/clipboard', { cache: 'no-store' });
          const j = await r.json();
          if (j.text) {
            try { await navigator.clipboard.writeText(j.text); if (app.status) app.status('Copied to phone'); }
            catch (_) { if (app.status) app.status('Copied to desktop clipboard (phone write blocked)'); }
          } else if (app.status) app.status('Desktop clipboard empty');
        } catch (_) { if (app.status) app.status('Copy failed', true); }
      }, 350);
    });
    mk('Paste', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (!text) { if (app.status) app.status('Phone clipboard empty'); return; }
        await fetch('/api/clipboard/set', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });
        try { rfb.clipboardPasteFrom(text); } catch (_) {}
        if (app.status) app.status('Pasted to desktop');
      } catch (_) { if (app.status) app.status('Phone clipboard blocked', true); }
    });
    mk('Close', () => { c.style.display = 'none'; });
    c.appendChild(cr);
    c.style.display = settings.extraKeys ? 'block' : 'none';
  }
  function sendShortcut(ch) {
    const ctrl = (DOMKeyTable['Control'] || [])[0] ?? 0xffe3;
    const ks = ch.charCodeAt(0);
    rfb.sendKey(ctrl, 'ControlLeft', true);
    setTimeout(() => {
      rfb.sendKey(ks, 'Key' + ch.toUpperCase(), true);
      setTimeout(() => {
        rfb.sendKey(ks, 'Key' + ch.toUpperCase(), false);
        setTimeout(() => rfb.sendKey(ctrl, 'ControlLeft', false), 30);
      }, 30);
    }, 30);
  }

  /* ---------------- settings section (appended to the app's menu) ---------------- */
  (function buildTouchMenu() {
    const mnu = app.menu;
    if (!mnu) return;
    const sec = document.createElement('div');
    const hr = document.createElement('hr');
    sec.appendChild(hr);
    const h = document.createElement('h3');
    h.textContent = 'Trackpad';
    sec.appendChild(h);
    const row = (label, el) => {
      const r = document.createElement('div');
      r.className = 'row';
      const l = document.createElement('label');
      l.textContent = label;
      r.appendChild(l);
      r.appendChild(el);
      sec.appendChild(r);
      return r;
    };
    const mkCheck = (val, fn) => {
      const wrap = document.createElement('div');
      wrap.className = 'check';
      const c = document.createElement('input');
      c.type = 'checkbox'; c.checked = val;
      c.addEventListener('change', e => fn(e.target.checked));
      wrap.appendChild(c);
      return wrap;
    };
    const mkRange = (min, max, step, val, fn) => {
      const i = document.createElement('input');
      i.type = 'range'; i.min = min; i.max = max; i.step = step; i.value = val;
      i.addEventListener('input', e => fn(e.target.value));
      return i;
    };
    row('Pointer speed', mkRange('0.5', '3', '0.1', settings.pointerSpeed,
      v => { settings.pointerSpeed = parseFloat(v); saveSettings(); }));
    row('Acceleration', mkCheck(settings.accel, v => { settings.accel = v; saveSettings(); }));
    row('Tap to click', mkCheck(settings.tapClick, v => { settings.tapClick = v; saveSettings(); }));
    row('Double-tap + drag', mkCheck(settings.tapDrag, v => { settings.tapDrag = v; saveSettings(); }));
    row('Invert scroll', mkCheck(settings.scrollInvert, v => { settings.scrollInvert = v; saveSettings(); }));
    row('Long-press delay (ms)', mkRange('250', '800', '25', settings.longPressMs,
      v => { settings.longPressMs = parseInt(v, 10); saveSettings(); }));
    row('Haptics', mkCheck(settings.haptics, v => { settings.haptics = v; saveSettings(); }));
    row('Extra keys bar', mkCheck(settings.extraKeys, v => {
      settings.extraKeys = v; saveSettings();
      const c = document.getElementById('extraKeys');
      if (c) c.style.display = v ? 'block' : 'none';
      if (v) buildExtraKeys();
    }));
    const r = document.createElement('div');
    r.className = 'row';
    const reset = document.createElement('button');
    reset.textContent = 'Reset trackpad settings';
    reset.addEventListener('click', () => {
      localStorage.removeItem('touchSettings');
      settings = { ...defaults };
      saveSettings();
      if (app.status) app.status('Trackpad settings reset – reloading…');
      setTimeout(() => location.reload(), 600);
    });
    r.appendChild(reset);
    sec.appendChild(r);
    mnu.appendChild(sec);
    buildExtraKeys();
  })();

  // keep the cursor ring meaningful across remote resizes / zoom
  const clampOnResize = () => { refreshFbSize(); clampCursor(); };
  addEventListener('resize', () => setTimeout(clampOnResize, 100));
  if (window.visualViewport) visualViewport.addEventListener('resize', () => setTimeout(clampOnResize, 100));
  try { rfb.addEventListener('desktopname', () => clampOnResize()); } catch (_) {}

  refreshFbSize();
  centerCursor();
  showCursor();
  if (app.status) app.status('Trackpad ready');

  /* ---------------- lifecycle safety ---------------- */
  const panic = () => {
    touches.forEach(st => { if (st.longTimer) clearTimeout(st.longTimer); });
    touches.clear();
    if (dragging) { dragging = false; setPressed(false); }
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; pending = null; }
    sendCursor(0);
  };
  addEventListener('blur', panic);
  document.addEventListener('visibilitychange', () => { if (document.hidden) panic(); });

  window.__touch = { settings, cursor: cursorPos, recenter: () => { centerCursor(); showCursor(); },
    setRfb(next) { rfb = next; refreshFbSize(); clampCursor(); } };
  window.__touchActions = {
    copy: () => sendShortcut('c'),
    paste: async () => {
      try { const t = await navigator.clipboard.readText(); if (t) rfb.clipboardPasteFrom(t); } catch (_) {}
    },
    startKeyboard: openKeyboard,
  };
}
