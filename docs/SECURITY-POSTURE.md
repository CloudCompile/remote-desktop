# Security Posture — Threat Model (STRIDE)

Status: v2.6.0 · Review cadence: every release that touches auth, the
HTTP surface or the proxy, and at least annually · Owner: repository
maintainers (see CODEOWNERS / SECURITY.md).

This document maps what this software protects, who the actors are,
where the trust boundaries sit, and how the STRIDE threats are
mitigated or accepted. It is written so a new contributor can tell —
for any line they touch — what it may trust and what it must verify.

## 1. Assets

| Asset | Where it lives | Impact if compromised |
|---|---|---|
| Desktop session | Xvnc `:1`, the user's running apps | Full access to the desktop user's session |
| Host shell | `apt` installs (store), app launch, xdotool | Effectively shell as the desktop user |
| File roots | `RD_FILES_ROOTS` (default `~/Downloads`) | Read/write of exposed trees |
| Clipboard | X selections + per-user history | Data disclosure/injection |
| Credentials | password hashes, session tokens, TOTP secrets, CSRF tokens, WebAuthn credentials | Account takeover |
| Audit log | JSONL with hash chain (#113) | Tampering hides attacker actions |
| The tunnel | Tailscale Funnel / reverse proxy | Public exposure if bypassed |

## 2. Actors

- **Admin** — full control (accounts, store, backups). The original
  single-user (RD_USER) account is always an admin.
- **Operator** — desktop + files + apps, no account/admin management.
- **Viewer** — read-only desktop, self-service personalization.
- **Anonymous remote** — anything reaching the tunnel or port.
- **Local host user** — other accounts on the same machine.

## 3. Trust boundaries and deployment topologies

```
[internet] --HTTPS--> [tunnel/reverse proxy] --HTTP--> [control.py 127.0.0.1:8090]
                                                            |--HTTP--> Xvnc :1 (loopback, see #122)
                                                            |--PulseAudio/ffmpeg subprocesses
                                                            |--xdotool / apt / tar subprocesses
                                                            |--files roots (openat containment)
[internet] <--ws(s)--> tunnel <---ws---> control.py <---RFB---> Xvnc
```

Topologies:

1. **Loopback only** (default): everything binds 127.0.0.1; the remote
   surface is only as open as the tunnel you add.
2. **Behind the Funnel / reverse proxy**: the proxy must terminate TLS
   and MUST NOT forward unauthenticated requests to privileged paths;
   `RD_TRUSTED_PROXIES` controls which proxies may set X-Forwarded-For.
3. **Direct LAN bind** (RD_HOST): only for trusted networks; every
   mitigation below still applies.

Trusted components (may be relied upon without re-verification):
stdlib, the OS package manager, the vendored noVNC tree (see
VENDORED.md + VENDORED.sums integrity check in CI).

NOT trusted (must verify per request): cookies, CSRF tokens, Origin /
Sec-Fetch-Site headers, X-Forwarded-For (only from trusted proxies),
file paths, upload payloads, archive members, webhook bodies.

## 4. STRIDE per surface

### 4.1 HTTP API (`remote_desktop/http.py`, `server.py`)

| Threat | Mitigation | Where tested |
|---|---|---|
| **S**poofing | HMAC-signed session tokens (7 d TTL); TOTP/passkey second factors; login rate limit | `auth.py` (`_verify_token`), `tests/security/test_totp_v260.py`, `test_csrf_sessions_v260.py` |
| **T**ampering | Requests cannot elevate: role caps checked per endpoint (`_POST_CAPS`/`_GET_CAPS`/`_DELETE_CAPS`) | `server.py::_require`, `tests/security/test_http_matrix_v260.py` |
| **R**epudiation | Audit log with hash chain, per-request ids in logs | `audit.py`, `tests/security/test_audit_v260.py` |
| **I**nformation disclosure | 401 before any data; per-user file/clipboard namespaces; health endpoint exposes only a timestamp (accepted risk) | `tests/security/test_users_v260.py`, files tests |
| **D**enial of service | Bounded bodies, connection caps, idle timeouts, bounded rate-limit map | `auth.py` (`LOGIN_ATTEMPTS_MAX_IPS`), `server.py` caps, `test_integration.py` |
| **E**levation of privilege | Fail-closed capability lookup (`_cap_needed` returns CAP_ADMIN on unknown path) | `server.py::_cap_needed`, `test_http_matrix_v260.py` |

### 4.2 CSRF and origins (#116)

State-changing endpoints require a CSRF token (double submit) AND a
same-origin check (Origin / Sec-Fetch-Site); GET is safe-only. The
websocket upgrade is exempt because it performs no state change and
requires its own authenticated session — verified in
`tests/security/test_csrf_sessions_v260.py`.

### 4.3 Websocket / VNC proxy (`vncproxy.py`, `ws.py`)

- The proxy relays bytes between the browser and Xvnc; it does not
  parse VNC beyond framing.
- The upstream is loopback plaintext **by default** (accepted risk for
  the loopback topology). `RD_VNC_PASSWORD_FILE` enables classic VNC
  auth — the proxy then MUST complete the credential exchange and fails
  closed if credentials are missing (#122, `tests/security/test_vnc_auth_v260.py`).
- The ws upgrade requires a live session cookie; per-message size caps
  and backpressure bound memory (`test_ws_limits` in integration tests).

### 4.4 Audio (`audio.py`)

- Streams are long-lived GETs on the authenticated surface; sharing
  fans out server-side so N listeners cost one capture (#105).
- Accepted risk: audio carries whatever the desktop plays — per-user
  audio isolation is NOT a goal of v2.6 (documented limitation).

### 4.5 Files (`files.py`)

- **S/E**: path containment via per-operation dirfds, every component
  opened `O_NOFOLLOW`, kernel-verified final component — no
  verify-then-use window (#91). Symlink-swap races tested.
- **T**: uploads are size-capped, chunk-checksummed, atomic on landing;
  archives extracted with zip-slip/bomb protection (#85).
- **I**: listings expose per-user roots only; previews render from a
  private temp copy, never inline-executed (#82).
- **R**: every mutation audited (user, ip, path, size, result) (#92/#113).

### 4.6 App store (`store.py`)

The store can install system packages: **API access implies host-shell
trust** (accepted, documented in SECURITY.md). Installs are
role-capped (CAP_ADMIN), queued serially, and audited.

### 4.7 Tunnel (#126 explicitly covers this)

- The tunnel is the ONLY public surface; the app must assume direct
  internet scanners hit it. Mitigations: unauthenticated surface is
  limited to `/login`, `/api/health`, static shell and security.txt;
  everything else 401s. Strict CSP + security headers (#117) bound
  what an injected script could do.
- Accepted risk: the Funnel terminates TLS at the Tailscale daemon —
  transport security depends on that platform (recorded, out of scope).

### 4.8 Accounts and session lifecycle (#111, #114, #118, #119)

- Password hashes: crypt SHA-512 via `PASS_FILE`; per-user accounts in
  the state-dir user store; TOTP secrets at rest are plaintext-in-state
  (accepted: same disk as the session keys; rotate on suspicion).
- Session registry persisted with mtime cache; revoke is immediate.
- Signing key rotation without downtime (#118): verify-then-replace
  accepts the previous key during the overlap window (bounded by TTL).
- Cookies: `__Host-` prefix when served over HTTPS behind a trusted
  proxy, `Secure`, `HttpOnly`, `SameSite=Lax` (#119).

## 5. Accepted risks (explicit)

1. `/api/health` and the static shell are unauthenticated (needed for
   readiness probes); they leak only version/timestamp.
2. VNC upstream is loopback plaintext unless #122 features are enabled.
3. The app store is host-shell-equivalent by design.
4. TOTP secrets and password hashes share the state directory's
   filesystem permissions (0600) as their boundary.
5. Per-user desktop isolation does not exist: every account controls
   the SAME physical desktop session — roles cap API power, not the X
   server. v2.6 isolation covers files/clipboard/pins/personalization.

## 6. Review cadence and change rules

- Any PR touching `auth.py`, `http.py`, `server.py`, `files.py`
  containment or the proxy requires the security regression suite
  (#125) to pass and a threat-model delta note in the PR body.
- Full STRIDE re-review: every minor release touching those files,
  otherwise annually.
- Vulnerabilities: see SECURITY.md and /.well-known/security.txt.
