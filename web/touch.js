// Mobile touch layer for the remote desktop.
//
// Default model is a TRACKPAD: the whole screen is one big trackpad and the
// pointer (cursor ring) moves relatively; taps click at the pointer, never
// where your finger is. An optional DIRECT-TOUCH mode (#41) sends taps and
// drags at the touched pixel instead.
//
// Gestures (trackpad mode):
//   1-finger drag        move pointer (with acceleration)
//   tap                  left click at pointer
//   double-tap + drag    left-click-drag (grab windows, select text)
//   double-tap (zoomed)  reset zoom (#42)
//   long press           right click at pointer
//   2-finger tap         right click at pointer
//   2-finger drag        scroll (zoom == 1) or pan the zoomed view (#42)
//   pinch                zoom around the focal point (#42)
//   3-finger tap         middle click at pointer
//   3-finger swipe       Alt+Tab app switch (#44)
//
// Gestures (direct mode, #41):
//   tap                  left click where you touched
//   1-finger drag        press-and-drag at the touch (pan when zoomed)
//   long press           right click where you touched
//   2-finger drag        scroll / pinch zoom (same as trackpad)
import DOMKeyTable from './core/input/domkeytable.js';

export function initTouch(app) {
  let rfb = app.rfb;                 // mutable: replaced on auto-reconnect
  const screenEl = app.screenEl;
  if (!rfb || !screenEl) return;

  // detach noVNC's own gesture handler; we own all touch input
  try { if (rfb._gestures) rfb._gestures.detach(); } catch (_) {}

  const defaults = { scrollInvert: false, pointerSpeed: 1.3, accel: true,
                     longPressMs: 450, haptics: false, extraKeys: false,
                     tapClick: true, tapDrag: true,
                     // v2.2.0: input model + gesture tuning (#41 #45 #46)
                     mode: 'trackpad', scrollPx: 34, tapSlop: 14, threeFinger: true };
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
    #extraKeys{position:fixed;left:0;right:0;bottom:var(--ikb,0px);z-index:110;
      padding:8px 8px calc(10px + var(--ib,0px) + var(--ikb,0px));
      background:var(--sheet,#161616);box-shadow:0 -6px 24px rgba(0,0,0,.5);display:none}
    #extraKeys .ekrow{display:flex;gap:6px;justify-content:center;margin-bottom:6px;flex-wrap:wrap}
    #extraKeys button{min-width:44px;min-height:44px;border:0;border-radius:8px;
      background:var(--card2,#333);color:var(--fg,#eee);
      font-size:14px;padding:0 8px;-webkit-tap-highlight-color:transparent;touch-action:manipulation}
    #extraKeys button.mod-on{background:var(--accent,#2a6);color:var(--accent-fg,#fff)}
  `;
  document.head.appendChild(style);

  const cursor = document.createElement('div');
  cursor.id = 'trackpadCursor';
  cursor.innerHTML = '<div class="ring"></div><div class="dot"></div>';
  document.body.appendChild(cursor);
  const showCursor = () => { if (settings.mode === 'direct') return; cursor.style.display = 'block'; paintCursor(); };
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
  const haptic = (ms) => {
    // never fire when disabled, unsupported (iOS) or reduced-motion is set (#45)
    if (!settings.haptics || !navigator.vibrate) return;
    try { if (!matchMedia('(prefers-reduced-motion: reduce)').matches) navigator.vibrate(ms); } catch (_) {}
  };
  // transient action overlay (e.g. "Alt+Tab") — the shell renders it (#44)
  const actionToast = (text) => { if (app.actionToast) try { app.actionToast(text); } catch (_) {} };

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
  // With the pinch-zoom layer active (#42) the shell canvas is additionally
  // translated by (panX, panY) and scaled by zoom.z, so painting multiplies
  // by the zoom and adds the pan, while _sendMouse coordinates divide it out.
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

  /* ---------------- pinch-zoom layer (#42) ---------------- */
  // CSS transform on noVNC's screen element: translate(pan) scale(z) with
  // origin at 0,0. The zoom transform never resizes the remote desktop —
  // it is purely visual (#43: local zoom must not silently resize).
  let vpMode = 'fit';                    // fit | fill | one | remote (set by the shell, #43)
  const zoom = { z: 1, base: 1, panX: 0, panY: 0, max: 5 };
  const zoomTarget = () => { try { return rfb._screen || null; } catch (_) { return null; } };
  const minZoom = () => (vpMode === 'fill' ? zoom.base : 1);
  function applyTransform() {
    const el = zoomTarget(); if (!el) return;
    el.style.transformOrigin = '0 0';
    el.style.transform = (zoom.z === 1 && !zoom.panX && !zoom.panY) ? '' :
      `translate(${zoom.panX}px,${zoom.panY}px) scale(${zoom.z})`;
  }
  function canvasBox() {
    // untransformed canvas offset + layout size in base (viewport) coordinates
    const c = rfb._canvas.getBoundingClientRect();
    const b = getRect();
    return { offX: (c.left - b.left - zoom.panX) / zoom.z,
             offY: (c.top - b.top - zoom.panY) / zoom.z,
             w: c.width / zoom.z, h: c.height / zoom.z };
  }
  function clampPan() {
    // panning can never move the framebuffer completely off-screen (#42)
    let box; try { box = canvasBox(); } catch (_) { return; }
    const b = getRect();
    const visW = box.w * zoom.z, visH = box.h * zoom.z;
    const m = 48;   // margin of content kept visible on each axis
    if (visW <= b.width - 2 * m) zoom.panX = (b.width - visW) / 2 - box.offX * zoom.z;
    else zoom.panX = Math.min(Math.max(zoom.panX, m - box.offX * zoom.z - visW),
                              b.width - m - box.offX * zoom.z);
    if (visH <= b.height - 2 * m) zoom.panY = (b.height - visH) / 2 - box.offY * zoom.z;
    else zoom.panY = Math.min(Math.max(zoom.panY, m - box.offY * zoom.z - visH),
                              b.height - m - box.offY * zoom.z);
  }
  function fillBase() {
    // fill mode zooms the fitted canvas so it covers the viewport (#43)
    if (vpMode !== 'fill') { zoom.base = 1; return; }
    const el = zoomTarget();
    const saved = el ? el.style.transform : '';
    if (el) el.style.transform = '';
    let b, c;
    try { b = getRect(); c = rfb._canvas.getBoundingClientRect(); } catch (_) { c = null; }
    if (el) el.style.transform = saved;
    if (!c || !c.width || !c.height) { zoom.base = 1; return; }
    zoom.base = Math.min(4, Math.max(1, Math.max(b.width / c.width, b.height / c.height)));
  }
  function resetZoom() {
    fillBase();
    zoom.z = vpMode === 'fill' ? zoom.base : 1;
    zoom.panX = 0; zoom.panY = 0;
    clampPan();
    applyTransform();
    if (app.vpInfo) try { app.vpInfo(); } catch (_) {}
  }
  function setVpMode(mode) {
    vpMode = ['fit', 'fill', 'one', 'remote'].includes(mode) ? mode : 'fit';
    resetZoom();
  }
  function setZoom(z, fxc, fyc) {
    // zoom around a focal point in viewport coordinates (#42)
    const z0 = zoom.z;
    const nz = Math.min(zoom.max, Math.max(minZoom(), z));
    if (fxc !== undefined && nz !== z0) {
      const b = getRect();
      const cx = (fxc - b.left - zoom.panX) / z0, cy = (fyc - b.top - zoom.panY) / z0;
      zoom.panX = fxc - b.left - cx * nz;
      zoom.panY = fyc - b.top - cy * nz;
    }
    zoom.z = nz;
    clampPan();
    applyTransform();
    if (app.vpInfo) try { app.vpInfo(); } catch (_) {}
  }

  /* ---------------- coordinate mapping ---------------- */
  // viewport (client) coordinates -> noVNC canvas coordinates, compensating
  // for the zoom layer; used by direct-touch taps and direct scroll (#41 #42)
  function clientToCanvas(cx, cy) {
    const b = getRect();
    return { x: (cx - b.left - zoom.panX) / zoom.z, y: (cy - b.top - zoom.panY) / zoom.z };
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
    cursorPos.sx = r.left + zoom.panX + s.x * zoom.z;
    cursorPos.sy = r.top + zoom.panY + s.y * zoom.z;
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
  let lastTapT = 0;              // timestamp of last tap (for double-tap zoom reset)
  let scrollAccum = 0;
  let gest = null;               // active 2-finger gesture: 'pinch' | 'pan' | 'scroll' | 'off'
  let swipe = null;              // 3-finger swipe origin (#44)
  // gesture tuning is user-configurable (#46): px of finger travel per wheel
  // notch, and the movement slop a tap is allowed before it counts as a drag
  const scrollNotch = () => Math.max(8, settings.scrollPx || 34);
  const tapSlop = () => Math.max(4, settings.tapSlop || 14);
  const TAP_MS = 260;

  function clickAt(buttonMask) {
    sendCursor(buttonMask);
    setPressed(true);
    setTimeout(() => { sendCursor(0); setPressed(dragging); }, 45);
    ripple(cursorPos.sx, cursorPos.sy);
    haptic(8);                                       // short tick on tap-click (#45)
  }
  // direct mode: click at explicit canvas coordinates (#41)
  function clickAtPos(p, buttonMask) {
    sendPointer(p.x, p.y, buttonMask);
    setPressed(true);
    setTimeout(() => { sendPointer(p.x, p.y, 0); setPressed(dragging); }, 45);
    haptic(8);
  }
  function directDrag(st) {
    // immediate press-and-drag at the touch point, 1:1, no acceleration (#41)
    if (!st.pressed) {
      const p = clientToCanvas(st.x, st.y);
      sendPointer(p.x, p.y, 0x1);
      st.pressed = 0x1;
      haptic(8);
      return;
    }
    const p = clientToCanvas(st.x, st.y);
    rafSend(p.x, p.y, st.pressed);
  }
  function sendAltTab() {
    if (!rfb) return;
    const alt = (DOMKeyTable['Alt'] || [])[0] ?? 0xffe9;
    const tab = (DOMKeyTable['Tab'] || [])[0] ?? 0xff09;
    rfb.sendKey(alt, 'AltLeft', true);
    setTimeout(() => {
      rfb.sendKey(tab, 'Tab', true);
      setTimeout(() => {
        rfb.sendKey(tab, 'Tab', false);
        setTimeout(() => rfb.sendKey(alt, 'AltLeft', false), 30);
      }, 40);
    }, 30);
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
    if (settings.mode !== 'direct') showCursor();
    for (const t of e.changedTouches) {
      if (isEdgePalm(t)) continue;
      const r = getRect();
      const st = { id: t.identifier, x: t.clientX, y: t.clientY,
                   sx: t.clientX, sy: t.clientY, lastX: t.clientX, lastY: t.clientY,
                   lastT: performance.now(), t0: Date.now(), vel: 0,
                   moved: false, longTimer: null, pressed: 0, panning: false };
      touches.set(t.identifier, st);

      if (touches.size === 1) {
        if (settings.mode === 'direct') {
          // direct mode: feedback at the finger; drag arms after the tap slop
          ripple(t.clientX, t.clientY);
          st.longTimer = setTimeout(() => {
            if (st.moved || touches.size > 1) return;
            clickAtPos(clientToCanvas(st.x, st.y), 0x4);   // long press = right click here
            st.longFired = true;
          }, settings.longPressMs);
        } else {
          // double-tap-drag: a new finger right after a tap starts a left-drag
          // (while zoomed a double-tap resets the zoom instead — #42)
          if (settings.tapDrag && dragArmed && Date.now() - dragArmed < 300) {
            if (zoom.z > minZoom() + 0.001) {
              resetZoom();                                 // double-tap zoom reset (#42)
              dragArmed = 0;
              st.ateDouble = true;
            } else if (!dragging) {
              dragging = true;
              setPressed(true);
              sendCursor(0x1);
              haptic(10);
            }
          }
          if (!dragging) {
            st.longTimer = setTimeout(() => {
              if (st.moved || touches.size > 1) return;
              clickAt(0x4);                                // long press = right click
              st.longFired = true;
            }, settings.longPressMs);
          }
        }
      } else if (touches.size === 2) {
        touches.forEach(s => { if (s.longTimer) { clearTimeout(s.longTimer); s.longTimer = null; } });
        scrollAccum = 0;
        // start a fresh scroll baseline when a second finger joins
        touches._scrollY = undefined;
        // pinch baseline: how the two fingers move decides pinch vs scroll (#42)
        const arr = [...touches.values()];
        gest = { d0: Math.max(20, Math.hypot(arr[0].x - arr[1].x, arr[0].y - arr[1].y)),
                 z0: zoom.z, mx0: (arr[0].x + arr[1].x) / 2, my0: (arr[0].y + arr[1].y) / 2,
                 mode: null };
      } else if (touches.size === 3) {
        // three-finger swipe origin (#44)
        const arr = [...touches.values()];
        swipe = { x: (arr[0].x + arr[1].x + arr[2].x) / 3,
                  y: (arr[0].y + arr[1].y + arr[2].y) / 3, t: Date.now() };
        gest = null;
      } else {
        gest = null;
        swipe = null;
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
      // keep the current position as well as the previous one: two-finger
      // scroll derives its midpoint from st.x/st.y, so only updating lastX/
      // lastY leaves the midpoint frozen and scroll never fires.
      st.x = t.clientX; st.y = t.clientY;
      st.lastX = t.clientX; st.lastY = t.clientY; st.lastT = now;
      if (!st.moved && Math.hypot(t.clientX - st.sx, t.clientY - st.sy) > tapSlop()) st.moved = true;
      if (st.longTimer && st.moved) { clearTimeout(st.longTimer); st.longTimer = null; }

      if (touches.size === 1 && st.moved) {
        if (settings.mode === 'direct') {
          // ---- direct touch: pan while zoomed, otherwise drag in place (#41 #42)
          if (zoom.z > minZoom() + 0.001 || st.panning) {
            st.panning = true;
            zoom.panX += dx; zoom.panY += dy;
            clampPan();
            applyTransform();
          } else {
            directDrag(st);
          }
        } else {
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
    }
    if (needClamp) clampCursor();

    if (touches.size === 2 && gest) {
      const arr = [...touches.values()];
      const mx = (arr[0].x + arr[1].x) / 2, my = (arr[0].y + arr[1].y) / 2;
      const d = Math.hypot(arr[0].x - arr[1].x, arr[0].y - arr[1].y);
      if (!gest.mode) {
        // disambiguate: fingers spreading = pinch, moving together = scroll/pan;
        // 2-finger scroll only when zoom == 1 (#42)
        const spread = Math.abs(d - gest.d0);
        const midMove = Math.hypot(mx - gest.mx0, my - gest.my0);
        if (spread > 12 && spread > midMove * 0.8)
          gest.mode = (vpMode === 'remote' || !settings.tapClick) ? 'off' : 'pinch';
        else if (midMove > tapSlop())
          gest.mode = zoom.z > minZoom() + 0.001 ? 'pan' : 'scroll';
        else if (gest._tries === undefined) gest._tries = 0;
        else if (++gest._tries > 400) gest.mode = 'off';   // stuck gesture guard
      }
      if (gest.mode === 'pinch') {
        setZoom(gest.z0 * (d / gest.d0), mx, my);
      } else if (gest.mode === 'pan') {
        const dxm = mx - (gest._mx ?? mx), dym = my - (gest._my ?? my);
        gest._mx = mx; gest._my = my;
        zoom.panX += dxm; zoom.panY += dym;
        clampPan();
        applyTransform();
      } else if (gest.mode === 'scroll') {
        // ---- two-finger scroll ----
        const prevY = touches._scrollY ?? my;
        scrollAccum += my - prevY;
        touches._scrollY = my;
        while (Math.abs(scrollAccum) >= scrollNotch()) {
          const down = scrollAccum > 0;
          scrollAccum -= down ? scrollNotch() : -scrollNotch();
          const inv = settings.scrollInvert;
          const mask = (down !== inv) ? 0x10 : 0x08;       // fingers down -> wheel down (invert flips)
          if (settings.mode === 'direct') {
            const p = clientToCanvas(mx, my);              // scroll at the fingers (#41)
            sendPointer(p.x, p.y, mask);
            setTimeout(() => sendPointer(p.x, p.y, 0), 25);
          } else {
            sendCursor(mask);
            setTimeout(() => sendCursor(0), 25);
          }
          haptic(4);
        }
      }
    }

    // track the three-finger swipe travel (#44)
    if (touches.size === 3 && swipe) {
      const arr = [...touches.values()];
      const mx = (arr[0].x + arr[1].x + arr[2].x) / 3;
      const my = (arr[0].y + arr[1].y + arr[2].y) / 3;
      swipe.dx = mx - swipe.x;
      swipe.dy = my - swipe.y;
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
    // a lifted finger invalidates the two-finger midpoint baseline
    if (touches.size < 2) touches._scrollY = undefined;
    const dur = Date.now() - (lifted[0]?.t0 || Date.now());
    const moved = lifted.some(s => s.moved);

    // three-finger horizontal swipe = Alt+Tab (#44); a cancelled or
    // vertical-dominant gesture does nothing
    if (lifted.length === 3 && swipe) {
      const s = swipe;
      swipe = null; gest = null;
      if (settings.threeFinger && Math.abs(s.dx || 0) > 70 && Math.abs(s.dy || 0) < 50) {
        sendAltTab();
        actionToast('Alt+Tab');
        haptic(12);
      }
    }

    if (touches.size === 0) {
      gest = null;
      if (settings.mode === 'direct') {
        for (const st of lifted) {
          if (st.pressed) {                                    // end of a direct drag
            const p = clientToCanvas(st.x, st.y);
            sendPointer(p.x, p.y, 0);
            st.pressed = 0;
          } else if (!moved && dur < TAP_MS && !lifted.some(s => s.longFired)) {
            const p = clientToCanvas(st.x, st.y);
            if (lifted.length === 1 && settings.tapClick) {
              const now = Date.now();
              if (zoom.z > minZoom() + 0.001 && now - lastTapT < 300) {
                resetZoom();                                   // double-tap resets zoom (#42)
                lastTapT = 0;
              } else {
                clickAtPos(p, 0x1);                            // tap = left click here
                lastTapT = now;
              }
            } else if (lifted.length === 2) {
              clickAtPos(p, 0x4);                              // 2-finger tap = right click
            } else if (lifted.length >= 3) {
              clickAtPos(p, 0x2);                              // 3-finger tap = middle click
            }
          }
        }
      } else if (!dragging) {
        if (!moved && dur < TAP_MS && !lifted.some(s => s.longFired || s.ateDouble)) {
          if (lifted.length === 1 && settings.tapClick) {
            const now = Date.now();
            if (zoom.z > minZoom() + 0.001 && now - lastTapT < 300) {
              resetZoom();                                     // double-tap resets zoom (#42)
              lastTapT = 0;
            } else {
              clickAt(0x1);                                    // tap = left click at pointer
              dragArmed = Date.now();                          // arm double-tap-drag
              lastTapT = now;
            }
          } else if (lifted.length === 2) {
            clickAt(0x4);                                      // 2-finger tap = right click
          } else if (lifted.length >= 3) {
            clickAt(0x2);                                      // 3-finger tap = middle click
          }
        } else if (!moved) {
          dragArmed = 0;
        }
        touches._scrollY = undefined;
      }
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
    gest = null; swipe = null;
    if (dragging) { dragging = false; setPressed(false); }
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; pending = null; }
    sendCursor(0);
  }, { passive: false });

  /* ---------------- keyboard bridge (mobile IME, #47) ---------------- */
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
  // IME composition (#47): while an editor is composing (Chinese, Japanese,
  // Korean, accent pickers, autocorrect), key/input events are pre-commit
  // noise and must not be forwarded. The committed text is typed in one shot
  // through the paste API on compositionend.
  let composing = false;
  kb.addEventListener('compositionstart', () => { composing = true; });
  kb.addEventListener('compositionupdate', () => { composing = true; });
  kb.addEventListener('compositionend', async (e) => {
    composing = false;
    const text = e.data || '';
    kb.value = '';
    if (!rfb || !text) return;
    try {
      const r = await fetch('/api/paste', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      const j = await r.json();
      if (app.status) app.status(j.ok ? 'Sent ' + text.length + ' chars' : 'Typing failed', !j.ok);
    } catch (_) {
      rfb.clipboardPasteFrom(text);      // control API unreachable: VNC clipboard fallback
    }
  });
  kb.addEventListener('keydown', (e) => {
    if (!rfb || composing || e.isComposing) return;
    const ks = keysymFor(e.key);
    if (ks !== null) { rfb.sendKey(ks, e.code || e.key, true); e.preventDefault(); }
  });
  kb.addEventListener('keyup', (e) => {
    if (!rfb || composing || e.isComposing) return;
    const ks = keysymFor(e.key);
    if (ks !== null) rfb.sendKey(ks, e.code || e.key, false);
  });
  kb.addEventListener('input', (e) => {
    if (!rfb || composing || e.isComposing) return;
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

  /* ---------------- extra keys panel (user-editable, #48) ---------------- */
  const mods = { Ctrl: false, Alt: false, Shift: false, Win: false };
  const locks = { Ctrl: false, Alt: false, Shift: false, Win: false };
  const KEYMAP = { Esc: 'Escape', Tab: 'Tab', Up: 'ArrowUp', Down: 'ArrowDown', Left: 'ArrowLeft',
                   Right: 'ArrowRight', PgUp: 'PageUp', PgDn: 'PageDown' };
  const MOD_TBL = { Ctrl: 'Control', Alt: 'Alt', Shift: 'Shift', Win: 'Meta' };
  const DEFAULT_CUSTOM = ['Ctrl+C', 'Ctrl+V', 'Ctrl+A', 'Ctrl+Z', 'Ctrl+W', 'Ctrl+T', 'Alt+Tab']
    .map(s => ({ label: s, keys: s }));
  const defaultCustomKeys = () => DEFAULT_CUSTOM.map(k => ({ ...k }));
  function loadCustomKeys() {
    try {
      const v = JSON.parse(localStorage.getItem('touchCustomKeys') || 'null');
      if (Array.isArray(v)) {
        const list = v.filter(e => e && typeof e.label === 'string' && typeof e.keys === 'string')
          .slice(0, 24).map(e => ({ label: e.label.slice(0, 12), keys: e.keys.slice(0, 40) }));
        if (list.length) return list;
      }
    } catch (_) {}
    return defaultCustomKeys();
  }
  let customKeys = loadCustomKeys();
  const saveCustomKeys = () => { try { localStorage.setItem('touchCustomKeys', JSON.stringify(customKeys)); } catch (_) {} };

  // parse "Ctrl+Alt+T", "Escape" or "f" into a keysym sequence, or null (#48)
  function parseKeys(seq) {
    const parts = String(seq).split('+').map(s => s.trim()).filter(Boolean);
    if (!parts.length) return null;
    const out = [];
    for (const p of parts.slice(0, -1)) {
      const name = MOD_TBL[p] || p;
      const ks = (DOMKeyTable[name] || [])[0];
      if (ks === undefined) return null;
      out.push([ks, name]);
    }
    const lastRaw = parts[parts.length - 1];
    const name = KEYMAP[lastRaw] || lastRaw;
    let ks = (DOMKeyTable[name] || [])[0];
    if (ks === undefined && name.length === 1) ks = name.charCodeAt(0);
    if (ks === undefined || ks === null) return null;
    out.push([ks, name]);
    return out;
  }
  function pressKeys(seq) {
    if (!rfb) return;
    const parsed = parseKeys(seq);
    if (!parsed) {
      if (app.status) app.status('Invalid key sequence: ' + seq, true);
      return;
    }
    // combine with modifier keys the user toggled on the panel (#48)
    const sticky = [];
    for (const m of ['Ctrl', 'Alt', 'Shift', 'Win']) {
      if (mods[m] || locks[m]) {
        const ks = (DOMKeyTable[m] || [])[0];
        if (ks !== undefined) { rfb.sendKey(ks, m, true); sticky.push(m); }
      }
    }
    for (const [ks, name] of parsed.slice(0, -1)) rfb.sendKey(ks, name, true);
    const [fks, fname] = parsed[parsed.length - 1];
    rfb.sendKey(fks, fname, true);
    rfb.sendKey(fks, fname, false);
    for (const [ks, name] of parsed.slice(0, -1).reverse()) rfb.sendKey(ks, name, false);
    for (const m of sticky.reverse()) {
      const ks = (DOMKeyTable[m] || [])[0];
      if (ks !== undefined) rfb.sendKey(ks, m, false);
      if (!locks[m]) mods[m] = false;
    }
    for (const mm of ['Ctrl', 'Alt', 'Shift', 'Win'])
      if (mods[mm] && !locks[mm]) mods[mm] = false;
  }

  function buildExtraKeys() {
    let c = document.getElementById('extraKeys');
    if (!c) { c = document.createElement('div'); c.id = 'extraKeys'; document.body.appendChild(c); }
    c.textContent = '';
    const rows = [
      ['Esc', 'Tab', 'Enter', 'Ctrl', 'Alt', 'Shift', 'Win'],
      ['Home', 'Up', 'PgUp', 'End', 'Down', 'PgDn', 'Left', 'Right'],
      ['F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12'],
    ];
    const plainKey = (key) => {
      // single key press, honouring the sticky modifier toggles (#48)
      pressKeys(KEYMAP[key] || key);
    };
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
          plainKey(key);
          buildExtraKeys();
        });
        r.appendChild(b);
      }
      c.appendChild(r);
    }
    // user-editable shortcuts (defaults merged until customised, #48)
    if (customKeys.length) {
      const r = document.createElement('div');
      r.className = 'ekrow';
      for (const entry of customKeys) {
        const b = document.createElement('button');
        b.textContent = entry.label;
        b.addEventListener('click', () => { haptic(4); pressKeys(entry.keys); buildExtraKeys(); });
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
      pressKeys('Ctrl+c');
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

  /* ---------------- extra-keys editor (#48) ---------------- */
  function buildKeysEditor() {
    const mnu = app.menu;
    if (!mnu) return;
    if (keysSec) keysSec.remove();
    keysSec = document.createElement('div');
    keysSec.appendChild(document.createElement('hr'));
    const h = document.createElement('h3');
    h.textContent = L('more.keysEditor', 'Extra keys editor');
    keysSec.appendChild(h);
    const mkInput = (val, aria, onInput) => {
      const i = document.createElement('input');
      i.type = 'text'; i.value = val;
      i.setAttribute('aria-label', aria);
      i.addEventListener('change', () => onInput(i.value.trim()));
      return i;
    };
    const mkMini = (txt, aria, fn) => {
      const b = document.createElement('button');
      b.type = 'button'; b.textContent = txt;
      b.setAttribute('aria-label', aria);
      b.addEventListener('click', fn);
      return b;
    };
    const rebuild = () => { saveCustomKeys(); buildKeysEditor(); buildExtraKeys(); };
    customKeys.forEach((entry, i) => {
      const r = document.createElement('div');
      r.className = 'row';
      r.appendChild(mkInput(entry.label, L('aria.keyLabel', 'Key label'), v => {
        if (!v) return;
        entry.label = v.slice(0, 12);
        rebuild();
      }));
      r.appendChild(mkInput(entry.keys, L('aria.keyKeys', 'Keysym sequence'), v => {
        if (!parseKeys(v)) {
          if (app.status) app.status(L('status.badKeys', 'Invalid keys — try Ctrl+Alt+T, Escape or f'), true);
          return;
        }
        entry.keys = v.slice(0, 40);
        rebuild();
      }));
      r.appendChild(mkMini('↑', L('aria.moveUp', 'Move up'), () => {
        if (i > 0) { [customKeys[i - 1], customKeys[i]] = [customKeys[i], customKeys[i - 1]]; rebuild(); }
      }));
      r.appendChild(mkMini('↓', L('aria.moveDown', 'Move down'), () => {
        if (i < customKeys.length - 1) { [customKeys[i + 1], customKeys[i]] = [customKeys[i], customKeys[i + 1]]; rebuild(); }
      }));
      r.appendChild(mkMini('✕', L('aria.removeKey', 'Remove key'), () => {
        customKeys.splice(i, 1);
        rebuild();
      }));
      keysSec.appendChild(r);
    });
    // add row
    const add = document.createElement('div');
    add.className = 'row';
    const nl = mkInput('', L('aria.keyLabel', 'Key label'), () => {});
    const nk = mkInput('', L('aria.keyKeys', 'Keysym sequence'), () => {});
    nl.placeholder = 'Label'; nk.placeholder = 'Ctrl+Alt+T';
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.textContent = L('btn.addKey', 'Add key');
    addBtn.addEventListener('click', () => {
      const label = nl.value.trim(), keys = nk.value.trim();
      if (!label || !keys) { if (app.status) app.status(L('status.badKeys', 'Invalid keys — try Ctrl+Alt+T, Escape or f'), true); return; }
      if (!parseKeys(keys)) { if (app.status) app.status(L('status.badKeys', 'Invalid keys — try Ctrl+Alt+T, Escape or f'), true); return; }
      if (customKeys.length >= 24) { if (app.status) app.status('Key list is full', true); return; }
      customKeys.push({ label: label.slice(0, 12), keys: keys.slice(0, 40) });
      rebuild();
    });
    add.appendChild(nl); add.appendChild(nk); add.appendChild(addBtn);
    keysSec.appendChild(add);
    const rd = document.createElement('div');
    rd.className = 'row';
    const restore = document.createElement('button');
    restore.textContent = L('btn.restoreKeys', 'Restore default keys');
    restore.addEventListener('click', () => { customKeys = defaultCustomKeys(); rebuild(); });
    rd.appendChild(restore);
    keysSec.appendChild(rd);
    if (settingsSec && settingsSec.parentNode === mnu) mnu.insertBefore(keysSec, settingsSec.nextSibling);
    else mnu.appendChild(keysSec);
  }

  /* ---------------- settings section (appended to the app's menu) ---------------- */
  // Labels come from the shell's i18n helper when available (#54); the
  // section can be rebuilt so a language switch updates it without reload.
  const L = (key, fallback) => (app.t ? app.t(key, fallback) : fallback);
  let settingsSec = null, keysSec = null;

  function buildTouchMenu() {
    const mnu = app.menu;
    if (!mnu) return;
    if (settingsSec) settingsSec.remove();
    settingsSec = document.createElement('div');
    const hr = document.createElement('hr');
    settingsSec.appendChild(hr);
    const h = document.createElement('h3');
    h.textContent = L('more.touch', 'Touch & trackpad');
    settingsSec.appendChild(h);
    const row = (label, el) => {
      const r = document.createElement('div');
      r.className = 'row';
      const l = document.createElement('label');
      l.textContent = label;
      r.appendChild(l);
      r.appendChild(el);
      settingsSec.appendChild(r);
      return r;
    };
    const mkCheck = (val, fn, label) => {
      const wrap = document.createElement('div');
      wrap.className = 'check';
      const c = document.createElement('input');
      c.type = 'checkbox'; c.checked = val;
      if (label) c.setAttribute('aria-label', label);
      c.addEventListener('change', e => fn(e.target.checked));
      wrap.appendChild(c);
      return wrap;
    };
    const mkRange = (min, max, step, val, fn, aria) => {
      const i = document.createElement('input');
      i.type = 'range'; i.min = min; i.max = max; i.step = step; i.value = val;
      if (aria) i.setAttribute('aria-label', aria);
      i.addEventListener('input', e => fn(e.target.value));
      return i;
    };
    // input model: trackpad (relative) vs direct touch (#41)
    const seg = document.createElement('div');
    seg.className = 'seg';
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', L('more.inputMode', 'Input mode'));
    for (const [val, label] of [['trackpad', L('mode.trackpad', 'Trackpad')],
                                ['direct', L('mode.direct', 'Direct touch')]]) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.classList.toggle('on', settings.mode === val);
      b.setAttribute('aria-pressed', settings.mode === val ? 'true' : 'false');
      b.addEventListener('click', () => setMode(val));
      seg.appendChild(b);
    }
    row(L('more.inputMode', 'Input mode'), seg);
    row(L('more.pointerSpeed', 'Pointer speed'), mkRange('0.5', '3', '0.1', settings.pointerSpeed,
      v => { settings.pointerSpeed = parseFloat(v); saveSettings(); }, L('more.pointerSpeed', 'Pointer speed')));
    row(L('more.accel', 'Acceleration'), mkCheck(settings.accel, v => { settings.accel = v; saveSettings(); }));
    row(L('more.tapClick', 'Tap to click'), mkCheck(settings.tapClick, v => { settings.tapClick = v; saveSettings(); }, L('more.tapClick', 'Tap to click')));
    row(L('more.tapDrag', 'Double-tap + drag'), mkCheck(settings.tapDrag, v => { settings.tapDrag = v; saveSettings(); }, L('more.tapDrag', 'Double-tap + drag')));
    row(L('more.scrollInvert', 'Invert scroll'), mkCheck(settings.scrollInvert, v => { settings.scrollInvert = v; saveSettings(); }, L('more.scrollInvert', 'Invert scroll')));
    row(L('more.scrollSpeed', 'Scroll speed'), mkRange('10', '80', '2', settings.scrollPx,
      v => { settings.scrollPx = parseInt(v, 10); saveSettings(); }, L('more.scrollSpeed', 'Scroll speed')));
    row(L('more.tapSlop', 'Tap tolerance (px)'), mkRange('6', '40', '2', settings.tapSlop,
      v => { settings.tapSlop = parseInt(v, 10); saveSettings(); }, L('more.tapSlop', 'Tap tolerance')));
    row(L('more.longPress', 'Long-press delay (ms)'), mkRange('250', '800', '25', settings.longPressMs,
      v => { settings.longPressMs = parseInt(v, 10); saveSettings(); }, L('more.longPress', 'Long-press delay')));
    row(L('more.haptics', 'Haptic feedback'), mkCheck(settings.haptics, v => { settings.haptics = v; saveSettings(); }, L('more.haptics', 'Haptic feedback')));
    row(L('more.threeFinger', 'Three-finger swipe = Alt+Tab'), mkCheck(settings.threeFinger,
      v => { settings.threeFinger = v; saveSettings(); }, L('more.threeFinger', 'Three-finger swipe = Alt+Tab')));
    row(L('more.extraKeys', 'Extra keys bar'), mkCheck(settings.extraKeys, v => {
      settings.extraKeys = v; saveSettings();
      const c = document.getElementById('extraKeys');
      if (c) c.style.display = v ? 'block' : 'none';
      if (v) buildExtraKeys();
    }, L('more.extraKeys', 'Extra keys bar')));
    const rEdit = document.createElement('div');
    rEdit.className = 'row';
    const editBtn = document.createElement('button');
    editBtn.textContent = L('more.editKeys', 'Edit extra keys…');
    editBtn.addEventListener('click', () => {
      buildKeysEditor();
      keysSec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    rEdit.appendChild(editBtn);
    settingsSec.appendChild(rEdit);
    const r = document.createElement('div');
    r.className = 'row';
    const reset = document.createElement('button');
    reset.textContent = L('more.resetTouch', 'Reset touch settings');
    reset.addEventListener('click', () => {
      localStorage.removeItem('touchSettings');
      localStorage.removeItem('touchCustomKeys');
      settings = { ...defaults };
      saveSettings();
      customKeys = defaultCustomKeys();
      if (app.status) app.status(L('status.touchReset', 'Touch settings reset – reloading…'));
      setTimeout(() => location.reload(), 600);
    });
    r.appendChild(reset);
    settingsSec.appendChild(r);
    mnu.appendChild(settingsSec);
    buildExtraKeys();
  }
  function setMode(mode) {
    settings.mode = mode === 'direct' ? 'direct' : 'trackpad';
    saveSettings();
    buildTouchMenu();
    if (settings.mode === 'direct') hideCursor(); else showCursor();
    if (app.onModeChange) try { app.onModeChange(settings.mode); } catch (_) {}
    if (app.status) app.status(settings.mode === 'direct'
      ? L('status.directOn', 'Direct touch: taps land where you touch')
      : L('status.trackpadOn', 'Trackpad mode: the pointer follows your finger'));
  }
  buildTouchMenu();

  // keep the cursor ring meaningful across remote resizes / zoom
  let wasLandscape = innerWidth > innerHeight;
  const clampOnResize = () => {
    refreshFbSize();
    clampCursor();
    const land = innerWidth > innerHeight;
    if (land !== wasLandscape) {
      wasLandscape = land;
      resetZoom();                       // orientation change resets zoom (#42)
    } else {
      fillBase();                        // fill mode follows the fitted canvas
      clampPan();
      applyTransform();
    }
  };
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
    gest = null; swipe = null;
    if (dragging) { dragging = false; setPressed(false); }
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; pending = null; }
    sendCursor(0);
  };
  addEventListener('blur', panic);
  document.addEventListener('visibilitychange', () => { if (document.hidden) panic(); });

  window.__touch = { settings, cursor: cursorPos, recenter: () => { centerCursor(); showCursor(); },
    setRfb(next) {
      rfb = next;
      refreshFbSize();
      clampCursor();
      resetZoom();                       // zoom resets cleanly on reconnect (#42)
    },
    setVpMode,                           // shell tells the layer about #43 modes
    getZoom: () => zoom.z,
    resetZoom,
    setMode,
    toggleMode: () => setMode(settings.mode === 'direct' ? 'trackpad' : 'direct'),
    rebuild: () => { buildTouchMenu(); buildKeysEditor(); } };
  window.__touchActions = {
    copy: () => pressKeys('Ctrl+c'),
    paste: async () => {
      try { const t = await navigator.clipboard.readText(); if (t) rfb.clipboardPasteFrom(t); } catch (_) {}
    },
    startKeyboard: openKeyboard,
  };
}
