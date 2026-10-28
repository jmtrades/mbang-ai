A sidebar link: 32px, 8px corners, 13.5px / 500 in `side-ink` with a 16px lucide icon in `muted` at stroke 1.9. Hover takes the `hover` wash and `ink`; the current page takes `side-active`, `ink` text and a `brand-fg` icon. Groups are collapsible 28px headers in 12px `muted` whose chevron appears on hover. Hand-written from `App.tsx`.

- Links carry `aria-current="page"`; the group header carries `aria-expanded`.
- Groups are role-aware (Today, Learn, Pipeline, Students, Clients, Studio, Owner); phones show four of these pages in a tab bar plus "More".
- The consumer provides `label`, `icon` (a lucide name), `active` and `onSelect`.
