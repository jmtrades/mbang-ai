A 28px round chip for a live state or a filter, taller and softer than a Badge. The header's AI pill is its canonical use: a pulsing `good` dot with "AI connected", or a `faint` dot with "AI offline: saved AI runs replay (labelled)". Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- Tones: `neutral` (default: `panel` fill, `line` border, `muted` text) and `good` (`good-soft`, `good-line`, `good-ink`).
- The consumer provides the dot and the text; the pill truncates long text and keeps 46vw max in the header.
