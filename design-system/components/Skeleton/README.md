Loading placeholders: a `neutral-soft` bar with a `hover` shimmer sweeping across in 1.4s, 7px corners. `rows` renders a `Loading` block of lines whose widths step down so the shape reads as text. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- 16px tall by default; size a single Skeleton with `className` or `style` to the shape it stands in for.
- The block carries `aria-busy` and an accessible "Loading" label.
- Under reduced motion the shimmer stops.
