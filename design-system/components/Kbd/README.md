A 20px key cap for shortcuts: `⌘K` / `Ctrl K` in the header's search button, `Esc`, `↑ ↓ ↵` in the command palette footer. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- `panel` fill, `line` border, a 1px `line` drop shadow below; 11px / 500 in the sans family, text `muted`.
- Show the platform's modifier: the app detects Mac and prints `⌘`, otherwise `Ctrl`.
- Hidden on coarse pointers: a phone shows no key caps.
