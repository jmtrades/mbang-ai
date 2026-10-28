Operator AI is a white-label console for a coaching programme: the owner, their coaches, setters and closers, and their students all work in it. It answers from the owner's own material with citations and drafts the work, and every outgoing item stops in front of a person first. The interface says exactly that: quiet surfaces, one brand colour the tenant chooses, honest status, nothing decorative.

> "Your programme's AI. It's trained on your material, it speaks under your brand, and it does the work for you and your students. Nothing goes out without your approval."

## Principles

- **The tenant's brand, derived.** The owner sets one colour in Setup. Everything brand-coloured is computed from it (`brand-solid`, `brand-fg`, `brand-soft`, `brand-line`, `brand-ring`), so any colour stays readable. Never hard-code a blue; never paint with `brand` raw.
- **Approval first, visibly.** The UI never claims a send happened. Copy and status reflect the real state: "Waiting for you to send", "Marked as sent", "Approved — now send it yourself and mark it sent".
- **Honest labelling.** Fictional data carries the violet `DEMO` badge everywhere it appears. AI involvement is labelled: `AI`, `Replayed AI`, `Rules`, `Template (AI off)`.
- **Neutral chrome, status in colour.** Surfaces are near-greyscale; colour means brand, status or fictional. Status is never colour alone: each badge carries a word.
- **Two themes, one file.** Every colour has a light and a dark value under `[data-theme]`; the choice follows the system unless the person picks.

## Content fundamentals

- **Voice:** plain, direct, second person. Say what the system did and what happens next: "Approved and sent", "Invite drafted — the owner approves it", "Link code ready. It works for an hour."
- **Errors name the fix:** "Couldn't change that. Try again." Never blame, never jargon, never a code in the message.
- **Sentence case everywhere**: titles, buttons, tabs, badges. The exceptions are acronyms (AI, DM, SMS, VSL, KPI, CRM, CTA, URL, API, MCP, ROAS) and the `DEMO` / `PITCH DEMO` badge, which is upper case on purpose.
- **Raw keys never reach the screen.** `needs_human` becomes "Needs human"; "4 draft(s)" becomes "4 drafts" / "1 draft that needs". Humanise on the client; the server text stays as it is.
- **Buttons are verbs**, short, with the object when it matters: "Approve", "Retry", "I sent it", "Check due items now", "Start today's mission". The primary button is one per view.
- **Numbers**: tabular numerals (`tabular-nums`) in Stat values, durations ("312 ms") and counts. Relative time reads "just now", "3m ago", "5h ago", "2d ago", then "Oct 25".
- **Empty states** state the fact, then the hint: "Nothing here yet" with a one-line reason and, when there is one, the action.
- **No impersonation.** Styled answers name who they are styled after and that it is an AI: "It's an AI assistant, not …". Banned names are scrubbed before display.
- **No emoji, no exclamation marks, no fake urgency.** No marketing superlatives; a hedged figure only where the owner holds a proof record.

## Colour

Paint surfaces with `side` (chrome and sidebar), `bg` (canvas), `panel` (cards and inputs) and `panel-2` (quiet fills). Text is `ink` on any of them; `ink-2` for secondary; `muted` for captions and hints, never below 12px; `faint` is decoration only and is never text. Hairlines are `line`; `line-2` inside a card; `line-strong` on hover and for dashed empty-state borders.

Brand tones, from the tenant's `brand`:

| Tone | Use |
|---|---|
| `brand-solid` | Primary button fill, active tab bar, selected row bar, `accent-color` |
| `brand-hover` | Primary button hover |
| `brand-fg` | Brand-coloured text and icons, links, the focus outline. 7.5:1 on `panel` |
| `brand-soft` / `brand-soft-2` | Tints: brand badge, selected row, avatar, highlighted source lines |
| `brand-line` | Rings on those tints |
| `brand-ring` | 3px ring on focused inputs |
| `brand-ink` | Text on `brand-solid`. White, or `#0b0b0d` when the brand is light |

Status comes as a family of five tokens: `<status>` for text and icons on `panel`, `<status>-soft` for the tint, `<status>-ink` for text on the tint, `<status>-line` for its ring, and `<status>-solid` where a solid fill sits under white text. Families: `good`, `bad`, `warn`, `info`, `demo` (violet, fictional data only) and `neutral` (soft and line only). Pair them as families; never set `good-ink` on `panel` or `good` on `good-soft`.

Contrast as shipped (WCAG 2): `ink` 17.9:1, `ink-2` 11.3:1, `muted` at least 5.2:1 on every surface, `placeholder` 4.6:1, `side-ink` 7.3:1, every `<status>-ink` on its `-soft` 6.8:1 or better, `brand-ink` on `brand-solid` 5.1:1; dark values are all higher. Two shipped pairs miss 4.5:1 and are kept as the source has them: `faint` (2.7:1, decoration only) and the chart series `viz-2` and `viz-3` in light (3.7:1 and 3.2:1), which are fills with a legend, never text.

Charts use `viz-1` to `viz-5` for categorical series (blue, teal, amber, purple, rose: distinct in lightness, not only hue) and `ramp-1` to `ramp-7` for ordered stages such as funnels, lightest first and ending on the brand.

## Theming

`<html data-theme="light|dark">` selects the theme; an inline script applies the saved choice (`localStorage["op.theme"]`) before first paint, and `color-scheme` follows it so native controls match. Default is the system preference; the person can pin Light or Dark. The `theme-color` meta switches between `side` values (`#f2f2f4` / `#0a0a0c`). A screen that still uses a Tailwind tint (`bg-green-50`) is remapped to a dark-safe value by a safety net; new screens use the tokens (`good-soft`, `warn-ink`).

## Typography

One family: Inter Variable with optical sizing, bundled from `@fontsource-variable/inter` and never fetched from another host. Features `cv02 cv03 cv04 cv11`, antialiased, `optimizeLegibility`. Body is 14px / 1.5 with letter-spacing -0.006em.

- `t-display` for the single hero line on Day One and sign-in (fluid 30–44px, weight 640, balanced wrapping).
- `t-h1` once per page in PageHeader; `t-h2` for sections; `t-h3` for card, modal and drawer titles.
- `t-lead` under a display line (painted `muted`); `t-body` for copy; `t-small` for dense lists; `t-caption` for hints, table heads and metadata; `t-overline` for an uppercase eyebrow.
- Interface sizes are half-pixel: buttons 12.5 / 13.5 / 14.5px, labels 12.5px, badges 11.5px, Kbd 11px, Stat values 26px / 600 / -0.02em.
- Code and source lines use the `mono` stack at 12px, or 0.92em inline with a `neutral-soft` background and `line-2` border.
- Headings use `text-wrap: balance`, paragraphs `text-wrap: pretty`.

## Spacing and layout

A 4px scale (Tailwind 4 defaults). Cards pad 16px, 20px from the `sm` breakpoint; card headers 12px / 16–20px with a `line-2` rule; table cells 10px / 12px. PageHeader keeps 24px below itself (28px from `md`).

The shell: a 260px sidebar on `side` (a 288px sheet on phones), a sticky 56px header on `bg` at 82% with a blur, a 58px tab bar on phones showing four role-specific pages plus "More". Content sits on `bg`; grid children get `min-width: 0` so wide tables scroll inside their card, never the page. The sidebar only scrolls when the active page is out of view and fades at its edges rather than being sliced.

## Shape, depth and borders

Corners: `radius-sm` 6px, `radius` 10px (inputs, step rows, tooltips), `radius-lg` 12px (cards, toasts, empty states), `radius-xl` 16px (modals, command palette). Buttons use 7 / 9 / 11px by size; badges 6px; pills and avatars are round.

Borders carry structure; shadows are faint. Cards are `panel` + 1px `line` + `card-highlight` (a dark-only top highlight) + `sh-xs`; on hover `line-strong` and `sh-md`. Primary buttons get `shadow-primary`, secondary ones `shadow-btn`. `sh-lg` is for toasts, `sh-xl` for modals, drawers and the palette. Nothing floats on shadow alone.

## Motion

Ease `cubic-bezier(0.2, 0.8, 0.2, 1)` everywhere. Durations: 150ms for colour and border changes, 180–220ms for modal and toast entry, 280ms for drawers and the sidebar sheet, 300ms for the tab indicator. `Reveal` fades and lifts list items in by 6px with a 35ms stagger (capped at 10). Theme switches use a 220ms view transition. The `pulse-dot` ring on the AI-connected pill repeats every 2.4s. Every animation collapses to near-zero under `prefers-reduced-motion`, and `MotionConfig reducedMotion="user"` turns off the JavaScript ones.

## States

- **Focus:** one visible ring everywhere a keyboard lands: 2px `focus` (= `brand-fg`) outline, 2px offset; inputs instead get a `brand` border and a 3px `brand-ring`.
- **Hover:** `hover` wash on rows and ghost buttons; `line-strong` border on cards, inputs and secondary buttons. **Press:** `press` wash, buttons move down 1px.
- **Disabled:** 50% opacity (60% on inputs), pointer events off. **Loading:** the button keeps its size, shows a spinner and `aria-busy`.
- **Selected:** table rows take `brand-soft` and a 2px `brand-solid` inset bar on the first cell; the active sidebar link takes `side-active` with a `brand-fg` icon; the active tab gets a 2px `brand-solid` underline that slides.
- **Skeleton:** `neutral-soft` to `hover` shimmer, 7px corners, 1.4s.
- **Selection:** text selection is `brand` at 26%.

## Iconography

Icons are lucide-react 1.61 (the `lucide` set), stroke 1.8–1.9 in navigation, 2.1 when active in the tab bar. Sizes: 14px inside small buttons and badges, 16px default, 18px in large buttons and the palette search, 20px in empty states, 21px in the tab bar. Icons take the current text colour; a brand-coloured icon is `brand-fg`, a status icon its `<status>` token. No emoji, no illustration. The app icon (`assets/Logos`) is a ring and dot in `#8b96ff` on `#14161b`; it is the PWA icon, not a logo, and the tenant's own logo or a monogram (`BrandMark`) represents them in the shell.

## Components

Built from `web/src/components/ui.tsx` and the shell in `App.tsx`:

| Component | Use |
|---|---|
| Button, IconButton | Six tones; secondary is the default, one primary per view, danger stays outlined |
| Badge, Pill | Status words; Pill for the AI-connected state and filter chips |
| Card, Stat, PageHeader | The page skeleton |
| Field, Input, TextArea, Select | Forms, 36px controls |
| Tabs, Segmented | In-page navigation and small exclusive choices |
| Table | Dense lists with optional row selection |
| Empty, ErrorBox, Skeleton, Toast | The four non-happy states |
| Kbd, Avatar, BrandMark | Small marks |
| Citation, RunSteps | The two things that make an answer trustworthy: where it came from and what it did |
| Modal, Drawer, CommandPalette | Overlays |
| NavItem, Sidebar, TabBar | Navigation: the desktop sidebar and the phone tab bar |
| Toaster | The toast stack, bottom-right on desktop and above the tab bar on phones |

Not in the system: TraceDrawer and SourceModal (data-bound uses of Drawer and Modal), Reveal (motion only), FlagList, Json, Loading, StepIcon, AiModeBadge, DemoBadge, StatusBadge (one-line wrappers around Badge), the Header, the sign-in screen and every page.

## Not synced

Values skipped: `--chevron` (a data-URI icon) and the dark-theme remap of Tailwind's palette (`--color-red-50` …), which are implementation safety nets rather than tokens. Brand tones are stored as their resolved values for the default brand `#5b6cff`; in code they are `color-mix()` expressions. The app declares no spacing variables, so the spacing family records the Tailwind steps in use. Fonts: only the Latin and Latin Extended subsets of Inter Variable are carried (the app also bundles Cyrillic, Greek and Vietnamese). Components were hand-written from the source as static React renditions of the Tailwind originals, not built from the repository (its build was not run); Reveal, the Header and the pages are left out.
