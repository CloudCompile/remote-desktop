# Shell locales

Translations for the custom mobile shell (toolbar, sheet titles, common
buttons, touch settings). noVNC's own interface strings live in
`web/app/locale/` and are separate.

## Adding a language

1. Copy `en.json` (the source of truth) to `<code>.json` using an ISO 639-1
   code, e.g. `sv.json`.
2. Translate the values. Keys are stable identifiers — never rename them.
   Missing keys fall back to English automatically, so partial translations
   are fine.
3. Register the code in the `LOCALES` list in `web/index.html` and add an
   `<option>` to the language picker in the More sheet.

Strings are plain key/value JSON; no plural forms or interpolation are
supported yet. Please keep placeable text (keys, shortcuts) untranslated.
