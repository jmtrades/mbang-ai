Every page opens with one: a `t-h1` title, an optional `t-overline` eyebrow above it, an optional badge beside it, a 14px `muted` subtitle that states the page's rule in full sentences, and right-aligned controls. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- 24px below (28px from `md`); the title block is capped at 48rem so subtitles wrap at a readable measure.
- Selects placed in `right` size to their content with a 9rem minimum.
- One per page; section headings inside the page are `t-h2`.
