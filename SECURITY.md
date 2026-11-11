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
