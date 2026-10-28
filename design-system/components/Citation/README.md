The chip that makes an answer trustworthy: `[n]` and the source title, a 12px `brand-fg` document icon, the cited lines or timestamp in `muted`. Clicking opens the source at those lines. 24px tall, `panel` on `line`, 11.5px `ink-2`; hover turns the border `brand-line` and the fill `brand-soft`. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- `Citation.List` lays chips out with 6px gaps under the answer.
- The consumer provides `cite` ({id, file, title, lines, ts, snippet}) and `onOpen`; the snippet becomes the tooltip.
- Never render an answer from the material without its chips; an uncited answer is "not covered".
