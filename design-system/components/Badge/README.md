One or two words of state in a 20px chip, read as a word first and a colour second. Seven tones map to the status families: `neutral` (default), `good`, `bad`, `warn`, `info`, `brand` and `demo`, which is reserved for the violet `DEMO` and `PITCH DEMO` labels on fictional data and is never used for anything else. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- `dot` adds a 6px disc in the text colour for live states ("done", "running", "REAL_OK").
- `caps` gives the 600 weight and 0.04em tracking of the DEMO badge; use it for that badge only.
- Fill is `<tone>-soft`, text `<tone>-ink`, a 1px inset ring of `<tone>-line`. 11.5px / 500.
- The consumer provides the words; keep them to two, in sentence case except acronyms.
