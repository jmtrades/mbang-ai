# Using this folder

This is the Operator AI design system, extracted from `web/src/styles.css`, `web/src/components/ui.tsx`,
`web/src/App.tsx` and `web/src/components/CommandPalette.tsx` at commit `d6c998b`. The live, browsable version is the
Design System artifact on claude.ai (ask the owner for the link).

| Path | What |
|---|---|
| `README.md` | The brand book: voice, colour, type, layout, motion, states, icons, components |
| `tokens.json` | Every token with light and dark values and a usage note |
| `tokens.css` | The same tokens as CSS custom properties, generated from `tokens.json` |
| `components/bundle.js`, `bundle.css`, `index.d.ts` | Hand-written React 18 renditions of the app's components (`window.Operator`) |
| `components/<Name>/README.md`, `preview.html` | Guidelines and a live preview per component |
| `fonts/` | Inter Variable, Latin and Latin Extended (SIL Open Font License, see `LICENSE-Inter.txt`) |
| `assets/Logos/` | The app icon |

The app itself does not read this folder: `web/src/styles.css` stays the source of truth. When a token changes there,
update `tokens.json` to match.
