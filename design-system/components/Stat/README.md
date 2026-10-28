A KPI card: a 12.5px `muted` label row of fixed height, a 26px tabular value and an optional 12px hint. The label row stays 20px tall whether or not the DEMO badge is shown so values line up across a row. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- `tone` paints the value `good`, `bad` or `warn`; leave it unset for a neutral count.
- `demo` adds the violet DEMO badge on the right of the label; set it whenever the number comes from fictional data.
- `icon` is a 14px lucide glyph before the label.
- The consumer provides a formatted value (thousands separators, units) and a hint that says what the number is counting.
