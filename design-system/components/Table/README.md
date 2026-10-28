A dense list: 12px `muted` heads over a `line` rule, 13.5px cells on `line-2` rows with 10px / 12px padding, no outer border because it sits inside a Card with `pad={false}`. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- With `onRow` rows are focusable, show the `hover` wash and open on Enter or Space.
- `selected(row)` paints the row `brand-soft` with a 2px `brand-solid` bar inset on the first cell.
- No rows renders `Empty` with the `empty` text ("Nothing here yet" by default).
- Wide tables scroll inside the card: the wrapper handles overflow.
- The consumer provides `cols` (key, label, optional render) and `rows`; put badges and relative times in `render`.
