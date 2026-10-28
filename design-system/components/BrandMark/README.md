The tenant in the shell: their uploaded logo in a 32px rounded square on `panel` with a `line` ring, or, when none is saved, a monogram of the brand name's first letter on the brand gradient in `brand-ink`. Hand-written from `App.tsx`.

- The gradient runs from a lighter brand tint through `brand` to `brand-hover`; the original mixes the brand with white and black in oklab.
- Only `https:` or root-relative image links are accepted for `logo`; a broken image falls back to the monogram.
- 32px / 14px text by default; `small` gives the 28px / 13px header variant on phones.
