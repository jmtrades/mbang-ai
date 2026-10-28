The desktop navigation: a 260px column on `side` with the tenant's mark and name at the top, role-ordered groups of NavItems in the middle, and the signed-in person at the bottom with a sign-out button. On phones it becomes a 288px sheet (86vw at most) that slides in from the left in 300ms. Hand-written from `App.tsx`.

- The brand row shows the tenant's logo or monogram (BrandMark), the brand name at 14px / 600 and the workspace name at 12px `muted`.
- Groups are collapsible and remembered per device; the list fades at its top and bottom edges rather than being sliced, and scrolls only when the active page is out of view.
- The footer shows the Avatar, the display name at 13px / 500 and the role at 11px `muted`, with "(DEMO) · PITCH DEMO" appended in the demo tenant.
- The consumer provides `brand`, `tenant`, `sections` (group, items with key, label and a lucide icon name), the current `page`, the `user` and `role`, and the navigation and sign-out handlers.
