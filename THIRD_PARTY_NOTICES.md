# Third-party notices

This project vendors code from other projects. Their licenses apply to those
files, not the MIT-licensed project code.

## noVNC — `web/core/`, `web/app/`, `web/vendor/`

- Upstream: https://github.com/novnc/noVNC
- License: Mozilla Public License 2.0 (MPL-2.0)
- Files carry their original copyright headers. A trimmed subset of the
  noVNC client is used by `web/index.html` (RFB/websock/display/input);
  the rest is included for completeness.
- Full license text: [`LICENSES/MPL-2.0.txt`](LICENSES/MPL-2.0.txt)

Modifications to MPL-covered files, if any, remain under MPL-2.0. The
project's own files (`control.py`, `web/index.html`, `web/touch.js`,
`session.sh`, `start.sh`, `webapps.sh`, `tunnel-url.sh`, docs) are MIT.

## pako — `web/vendor/pako/`

- Upstream: https://github.com/nodeca/pako
- License: MIT
- Copyright (c) 2014-2017 Vitaly Puzrin, Andrey Tupitsin

```text
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Fonts and sounds — `web/app/styles/`, `web/app/sounds/`

- Orbitron font: SIL Open Font License 1.1 (see upstream distribution).
- Sound effects: see `web/app/sounds/CREDITS`.
