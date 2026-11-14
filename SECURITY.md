# Security Policy

## Supported versions

Only the latest `main` is supported.

## Reporting a vulnerability

Do not open a public issue for security problems. Use GitHub's private
vulnerability reporting (Security → Report a vulnerability) or contact the
maintainer through the GitHub profile. Include reproduction steps and the
affected endpoint. You will get an acknowledgement as soon as possible.

## Deployment assumptions

This project is designed for **one trusted user on a machine you control**:

- `control.py` binds `127.0.0.1:8090`. It must sit behind an HTTPS tunnel
  or reverse proxy; never expose it directly.
- The VNC server binds localhost and has no VNC-level password — the HTTP
  login is the gate.
- The login cookie is HMAC-signed with a key derived from the password
  hash. Rotating the password invalidates all sessions.
- Login rate limiting is in-memory (5 attempts / 10 min / IP) and resets
  on restart. It slows brute force; it is not a WAF.
- `/api/health` is intentionally unauthenticated and exposes only a
  timestamp.
- The app store can run `sudo apt-get install`; treat API access as shell
  access to the host.

## Hardening checklist

- Strong password; `.passhash` mode 600.
- Keep the OS, Chrome and noVNC sources up to date.
- Restrict who can reach the tunnel URL.
- Back up nothing secret to the repo: `.passhash`, `.pins.json` and
  `.cliphistory.json` are gitignored.

## Response targets

- **Acknowledgement:** within 72 hours of a report.
- **Triage decision** (accepted / fixed / needs-info): within 7 days.
- **Fix or mitigation:** severity-based — critical inside 7 days,
  high inside 30, others on the next scheduled release.
- **Disclosure:** coordinated with the reporter; credit on request.

## Safe harbour

We consider security research conducted in good faith — including
automated scanning, probing and proof-of-concept reproduction against
deployments you own or are authorised to test — to be authorised
activity. We will not pursue legal action for such reports and we do
not ask you to stop publishing defensively written findings after the
fix ships.

## Out of scope

- Volumetric DoS of a deployment you do not own.
- Social engineering of the host's human users.
- Findings that require the attacker to already have shell on the host
  (the control API is host-trust-equivalent; see the store note above).
- Missing security features on the ROADMAP (v2.6.0+ issues) that are
  tracked and scheduled.
- Vulnerabilities in the tunnel/proxy platform itself (Tailscale,
  Cloudflare) — report upstream.

## Reporting a compromised host (not a code bug)

If a deployment you use shows signs of compromise (unknown sessions,
unrecognised audit entries, new files in the roots):

1. **Revoke sessions now** — More sheet → Security → sign out all
   devices, or `POST /api/admin/sessions/revoke` as admin. Sessions die
   immediately (#114).
2. **Rotate the credential** — change the password (`/api/account/password`);
   the signing key rotates in the same call, old tokens die when their
   grace expires (#118).
3. **Check the audit log** — `/api/admin/audit` + `/api/admin/audit/verify`
   show what was done and whether the chain is intact (#113).
4. **Then rotate host credentials** — the control API is
   host-trust-equivalent, so treat every host account as exposed.

Machine-readable contact: `/.well-known/security.txt` (RFC 9116).

Threat model and STRIDE review: [docs/SECURITY-POSTURE.md](docs/SECURITY-POSTURE.md).
