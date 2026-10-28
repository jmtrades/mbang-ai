A panel that slides in from the right on the `overlay` scrim for detail that belongs beside the page rather than over it: a run's trace, a lead's record. Full height, 36rem wide at most, `panel` with a `line` left edge and `sh-xl`; the same header as Modal (a `t-h3` title and a 28px close button on a `line-2` rule) and a scrolling 20px body. Shown here in its open state; the original slides in over 280ms. Hand-written from `web/src/components/ui.tsx`.

- Escape closes, focus is trapped and restored, as in Modal.
- Use Modal for a decision and Drawer for reading; a drawer never holds the page's primary action.
- The consumer provides `title`, `children`, `open` and `onClose`; TraceDrawer in the app is this with a run loaded.
