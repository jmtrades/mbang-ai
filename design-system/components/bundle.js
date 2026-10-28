/* @ds-bundle: {"format":4,"namespace":"Operator","components":[{"name":"Button"},{"name":"IconButton"},{"name":"Badge"},{"name":"Pill"},{"name":"Kbd"},{"name":"Avatar"},{"name":"BrandMark"},{"name":"Card"},{"name":"Stat"},{"name":"PageHeader"},{"name":"Field"},{"name":"Input"},{"name":"TextArea"},{"name":"Select"},{"name":"Tabs"},{"name":"Segmented"},{"name":"Table"},{"name":"Empty"},{"name":"ErrorBox"},{"name":"Skeleton"},{"name":"Toast"},{"name":"Citation"},{"name":"RunSteps"},{"name":"Modal"},{"name":"CommandPalette"},{"name":"NavItem"},{"name":"Drawer"},{"name":"Toaster"},{"name":"Sidebar"},{"name":"TabBar"},{"name":"Icon"}]} */
/* Operator AI — hand-written from web/src/components/ui.tsx, App.tsx and CommandPalette.tsx. Reads window.React / window.ReactDOM. */
(function () {
  var React = window.React;
  var h = React.createElement;
  var useState = React.useState, useRef = React.useRef, useLayoutEffect = React.useLayoutEffect;
  function cx() { var out = []; for (var i = 0; i < arguments.length; i++) if (arguments[i]) out.push(arguments[i]); return out.join(" "); }

  // lucide 1.61.0 (ISC) — the glyphs the primitives use
  var ICONS = {
    check: [["path", { d: "M20 6 9 17l-5-5" }]],
    x: [["path", { d: "M18 6 6 18" }], ["path", { d: "m6 6 12 12" }]],
    "triangle-alert": [["path", { d: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" }], ["path", { d: "M12 9v4" }], ["path", { d: "M12 17h.01" }]],
    loader: [["path", { d: "M21 12a9 9 0 1 1-6.219-8.56" }]],
    inbox: [["polyline", { points: "22 12 16 12 14 15 10 15 8 12 2 12" }], ["path", { d: "M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" }]],
    "file-text": [["path", { d: "M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z" }], ["path", { d: "M14 2v5a1 1 0 0 0 1 1h5" }], ["path", { d: "M10 9H8" }], ["path", { d: "M16 13H8" }], ["path", { d: "M16 17H8" }]],
    "chevron-right": [["path", { d: "m9 18 6-6-6-6" }]],
    "chevron-down": [["path", { d: "m6 9 6 6 6-6" }]],
    search: [["path", { d: "m21 21-4.34-4.34" }], ["circle", { cx: "11", cy: "11", r: "8" }]],
    "circle-check": [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "m16 9-5.5 5.5L8 12" }]],
    "circle-x": [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "m15 9-6 6" }], ["path", { d: "m9 9 6 6" }]],
    copy: [["rect", { width: "14", height: "14", x: "8", y: "8", rx: "2", ry: "2" }], ["path", { d: "M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" }]],
    clock: [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "M12 6v6l4 2" }]],
    "circle-dot": [["circle", { cx: "12", cy: "12", r: "1" }], ["circle", { cx: "12", cy: "12", r: "10" }]],
    "corner-down-left": [["path", { d: "M20 4v7a4 4 0 0 1-4 4H4" }], ["path", { d: "m9 10-5 5 5 5" }]],
    sparkles: [["path", { d: "M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z" }], ["path", { d: "M20 2v4" }], ["path", { d: "M22 4h-4" }], ["circle", { cx: "4", cy: "20", r: "2" }]],
    "rotate-cw": [["path", { d: "M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" }], ["path", { d: "M21 3v5h-5" }]],
    info: [["circle", { cx: "12", cy: "12", r: "10" }], ["path", { d: "M12 16v-4" }], ["path", { d: "M12 8h.01" }]],
    house: [["path", { d: "M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8" }], ["path", { d: "M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" }]],
    users: [["path", { d: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" }], ["path", { d: "M16 3.128a4 4 0 0 1 0 7.744" }], ["path", { d: "M22 21v-2a4 4 0 0 0-3-3.87" }], ["circle", { cx: "9", cy: "7", r: "4" }]],
    send: [["path", { d: "M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z" }], ["path", { d: "m21.854 2.147-10.94 10.939" }]],
    rocket: [["path", { d: "M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" }], ["path", { d: "M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09" }], ["path", { d: "M9 12a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.4 22.4 0 0 1-4 2z" }], ["path", { d: "M9 12H4s.55-3.03 2-4c1.62-1.08 5 .05 5 .05" }]],
    gauge: [["path", { d: "m12 14 4-4" }], ["path", { d: "M3.34 19a10 10 0 1 1 17.32 0" }]],
    "message-circle": [["path", { d: "M2.992 16.342a2 2 0 0 1 .094 1.167l-1.065 3.29a1 1 0 0 0 1.236 1.168l3.413-.998a2 2 0 0 1 1.099.092 10 10 0 1 0-4.777-4.719" }]],
    "log-out": [["path", { d: "m16 17 5-5-5-5" }], ["path", { d: "M21 12H9" }], ["path", { d: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" }]],
    plus: [["path", { d: "M5 12h14" }], ["path", { d: "M12 5v14" }]],
    pencil: [["path", { d: "M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z" }], ["path", { d: "m15 5 4 4" }]],
    "arrow-right": [["path", { d: "M5 12h14" }], ["path", { d: "m12 5 7 7-7 7" }]]
  };
  function Icon(p) {
    var nodes = ICONS[p.name] || [];
    var size = p.size || 16;
    return h("svg", { xmlns: "http://www.w3.org/2000/svg", width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: p.strokeWidth || 2, strokeLinecap: "round", strokeLinejoin: "round", className: cx(p.className, p.spin && "op-spin"), "aria-hidden": true },
      nodes.map(function (n, i) { return h(n[0], Object.assign({ key: i }, n[1])); }));
  }

  function Button(p) {
    var tone = p.tone || "secondary", size = p.size || "md";
    var rest = Object.assign({}, p); delete rest.tone; delete rest.size; delete rest.loading; delete rest.className; delete rest.children;
    return h("button", Object.assign({ type: "button" }, rest, { disabled: p.disabled || p.loading, "aria-busy": p.loading || undefined, className: cx("op-btn", "op-btn-" + size, "op-btn-" + tone, p.className) }),
      p.loading && h(Icon, { name: "loader", spin: true }), p.children);
  }
  function IconButton(p) {
    var rest = Object.assign({}, p); delete rest.label;
    return h(Button, Object.assign({ tone: p.tone || "ghost", size: p.size || "icon", "aria-label": p.label, title: p.title || p.label }, rest));
  }
  function Badge(p) {
    return h("span", { title: p.title, className: cx("op-badge", p.tone && p.tone !== "neutral" && "op-badge-" + p.tone, p.caps && "op-badge-caps", p.className) }, p.dot && h("span", { className: "op-badge-dot" }), p.children);
  }
  function Pill(p) { return h("span", { className: cx("op-pill", p.tone === "good" && "op-pill-good", p.className) }, p.children); }
  function Kbd(p) { return h("kbd", { className: cx("op-kbd", p.className) }, p.children); }
  function Avatar(p) {
    var initials = String(p.name || "").replace(/\(.*?\)/g, "").trim().split(/\s+/).map(function (w) { return w[0]; }).filter(Boolean).slice(0, 2).join("").toUpperCase() || "?";
    return h("span", { "aria-hidden": true, className: cx("op-avatar", p.small && "op-avatar-sm", p.className) }, initials);
  }
  function BrandMark(p) {
    if (p.logo) return h("img", { src: p.logo, alt: "", "aria-hidden": true, className: cx("op-mark-img", p.className) });
    return h("span", { "aria-hidden": true, className: cx("op-mark", p.small && "op-mark-sm", p.className) }, (String(p.name || "").trim()[0] || "O").toUpperCase());
  }
  function Card(p) {
    var pad = p.pad !== false;
    return h("section", { className: cx("op-card", p.hover && "op-card-hover", p.className) },
      (p.title || p.actions) && h("header", { className: cx("op-card-head", p.subtitle && "has-sub") },
        h("div", { className: "op-card-title" }, p.title && h("h3", { className: "op-h3" }, p.title), p.subtitle && h("p", { className: "op-card-sub" }, p.subtitle)),
        p.actions && h("div", { className: "op-card-actions" }, p.actions)),
      h("div", { className: cx("op-card-body", !pad && "flush", p.bodyClassName) }, p.children));
  }
  function Stat(p) {
    return h("div", { className: "op-card op-stat" },
      h("div", { className: "op-stat-label" }, h("span", null, p.icon, p.label), p.demo && h(Badge, { tone: "demo", caps: true, title: "Fictional demo data" }, "DEMO")),
      h("div", { className: cx("op-stat-value", p.tone && "op-stat-" + p.tone) }, p.value),
      p.hint && h("div", { className: "op-stat-hint" }, p.hint));
  }
  function PageHeader(p) {
    return h("div", { className: "op-ph" },
      h("div", { className: "op-ph-main" }, p.eyebrow && h("div", { className: "op-overline op-ph-eyebrow" }, p.eyebrow), h("h1", { className: "op-h1" }, p.title, p.badge), p.subtitle && h("p", { className: "op-ph-sub" }, p.subtitle)),
      p.right && h("div", { className: "op-ph-right" }, p.right));
  }
  function Field(p) { return h("label", { className: "op-field" }, h("span", { className: "op-label" }, p.label), p.children, p.hint && h("span", { className: "op-hint" }, p.hint)); }
  function Input(p) { return h("input", Object.assign({}, p, { className: cx("op-input", p.className) })); }
  function TextArea(p) { return h("textarea", Object.assign({ rows: 5 }, p, { className: cx("op-input", p.className) })); }
  function Select(p) {
    var rest = Object.assign({}, p); delete rest.options;
    return h("select", Object.assign({}, rest, { className: cx("op-input", p.className) }), (p.options || []).map(function (o) {
      return typeof o === "string" ? h("option", { key: o, value: o }, o) : h("option", { key: o.value, value: o.value }, o.label);
    }));
  }
  function Tabs(p) {
    var ref = useRef(null); var st = useState(null); var bar = st[0], setBar = st[1];
    useLayoutEffect(function () {
      var list = ref.current; if (!list) return;
      var el = list.querySelector('[data-tab="' + p.value + '"]');
      setBar(el ? { x: el.offsetLeft + 8, w: Math.max(0, el.offsetWidth - 16) } : null);
    }, [p.value, p.tabs.length]);
    return h("div", { ref: ref, className: "op-tabs", role: "tablist" },
      bar && h("span", { "aria-hidden": true, className: "op-tab-bar", style: { width: bar.w, transform: "translateX(" + bar.x + "px)" } }),
      p.tabs.map(function (t) { return h("button", { key: t.key, type: "button", role: "tab", "data-tab": t.key, "aria-selected": p.value === t.key, tabIndex: p.value === t.key ? 0 : -1, onClick: function () { p.onChange && p.onChange(t.key); }, className: "op-tab" }, t.label); }));
  }
  function Segmented(p) {
    return h("div", { role: "radiogroup", "aria-label": p.label, className: "op-seg" }, p.options.map(function (o) {
      return h("button", { key: o.value, type: "button", role: "radio", "aria-checked": p.value === o.value, onClick: function () { p.onChange && p.onChange(o.value); }, className: "op-seg-opt" }, o.icon, o.label);
    }));
  }
  function Table(p) {
    if (!p.rows || !p.rows.length) return h(Empty, { title: p.empty || "Nothing here yet" });
    return h("div", { className: "op-table-wrap" }, h("table", { className: "op-table" },
      h("thead", null, h("tr", null, p.cols.map(function (c) { return h("th", { key: c.key, scope: "col", className: c.className }, c.label); }))),
      h("tbody", null, p.rows.map(function (r, i) {
        var on = p.selected ? !!p.selected(r) : false;
        return h("tr", { key: r.id != null ? r.id : i, "aria-selected": p.selected ? on : undefined, className: cx(p.onRow && "clickable"), onClick: function () { p.onRow && p.onRow(r); }, tabIndex: p.onRow ? 0 : undefined },
          p.cols.map(function (c) { return h("td", { key: c.key, className: c.className }, c.render ? c.render(r) : String(r[c.key] == null ? "" : r[c.key])); }));
      }))));
  }
  function Empty(p) {
    return h("div", { className: cx("op-empty", p.className) },
      h("div", { className: "op-empty-icon" }, p.icon || h(Icon, { name: "inbox", size: 20 })),
      h("div", null, h("p", { className: "op-empty-title" }, p.title), p.hint && h("p", { className: "op-empty-hint" }, p.hint)),
      p.action && h("div", { className: "op-empty-action" }, p.action));
  }
  function ErrorBox(p) {
    if (!p.error) return null;
    var msg = typeof p.error === "string" ? p.error : p.error.message;
    return h("div", { role: "alert", className: "op-error" }, h(Icon, { name: "triangle-alert" }), h("span", { className: "op-error-msg" }, msg),
      p.retry && h(Button, { size: "sm", onClick: p.retry }, h(Icon, { name: "rotate-cw", size: 14 }), " Retry"));
  }
  function Skeleton(p) {
    if (p.rows) return h("div", { "aria-busy": "true", "aria-label": "Loading", style: { display: "flex", flexDirection: "column", gap: 10, padding: "4px 0" } },
      Array.from({ length: p.rows }).map(function (_, i) { return h("div", { key: i, className: "op-skel", style: { width: (92 - ((i * 17) % 40)) + "%" } }); }));
    return h("div", { className: cx("op-skel", p.className), style: p.style });
  }
  function Toast(p) {
    var tone = p.tone || "info";
    return h("div", { role: "status", className: "op-toast" },
      h(Icon, { name: tone === "good" ? "circle-check" : tone === "bad" ? "triangle-alert" : "info", className: tone === "good" ? "op-good" : tone === "bad" ? "op-bad" : "op-brand" }),
      h("span", { className: "op-toast-msg" }, p.children),
      h("button", { type: "button", className: "op-toast-x", "aria-label": "Dismiss", onClick: p.onDismiss }, h(Icon, { name: "x", size: 14 })));
  }
  function Citation(p) {
    var c = p.cite;
    return h("button", { type: "button", className: "op-cite", title: c.snippet, onClick: function () { p.onOpen && p.onOpen(c); } },
      h(Icon, { name: "file-text", size: 12 }), h("span", { className: "op-cite-id" }, "[" + c.id + "]"), h("span", { className: "op-cite-title" }, c.title || c.file),
      c.lines && h("span", { className: "op-cite-meta" }, "L" + c.lines), c.ts && h("span", { className: "op-cite-meta" }, c.ts));
  }
  Citation.List = function (p) { return h("div", { className: "op-cites" }, (p.items || []).map(function (c) { return h(Citation, { key: c.id + c.file + (c.lines || ""), cite: c, onOpen: p.onOpen }); })); };
  function StepIcon(p) {
    var s = p.status;
    if (s === "ok" || s === "done") return h(Icon, { name: "circle-check", className: "op-good" });
    if (s === "error" || s === "failed") return h(Icon, { name: "circle-x", className: "op-bad" });
    if (s === "skipped") return h(Icon, { name: "circle-dot", className: "op-muted" });
    if (s === "running") return h(Icon, { name: "loader", spin: true, className: "op-brand" });
    return h(Icon, { name: "clock", className: "op-faint" });
  }
  function RunSteps(p) {
    var st = useState(p.defaultOpen != null ? p.defaultOpen : null); var open = st[0], setOpen = st[1];
    return h("ol", { className: "op-steps" }, p.steps.map(function (s, i) {
      return h("li", { key: i, className: cx("op-step", s.status === "running" && "running") },
        h("button", { type: "button", className: "op-step-btn", onClick: function () { setOpen(open === i ? null : i); }, "aria-expanded": !p.compact ? open === i : undefined },
          h(StepIcon, { status: s.status }), h("span", { className: cx("op-step-label", s.status === "pending" && "pending") }, s.label || s.tool),
          s.ms != null && h("span", { className: "op-step-ms" }, s.ms + " ms"),
          !p.compact && h(Icon, { name: "chevron-right", className: cx("op-step-chev", open === i && "open") })),
        !p.compact && open === i && h("div", { className: "op-step-body" },
          h("div", null, h("div", { className: "op-overline", style: { marginBottom: 4 } }, "Input"), h("pre", { className: "op-pre" }, JSON.stringify(s.input, null, 2))),
          h("div", null, h("div", { className: "op-overline", style: { marginBottom: 4 } }, "Output"), h("pre", { className: "op-pre" }, JSON.stringify(s.output, null, 2)))));
    }));
  }
  function Modal(p) {
    if (p.open === false) return null;
    return h("div", { className: "op-scrim", role: "presentation" }, h("div", { role: "dialog", "aria-modal": "true", className: "op-modal", style: p.wide ? { maxWidth: "56rem" } : null },
      h("div", { className: "op-modal-head" }, h("h2", { className: "op-h3" }, p.title), h(IconButton, { label: "Close", size: "icon-sm", onClick: p.onClose }, h(Icon, { name: "x" }))),
      h("div", { className: "op-modal-body" }, p.children)));
  }
  function CommandPalette(p) {
    var st = useState(p.active || 0); var active = st[0], setActive = st[1];
    var idx = -1;
    return h("div", { className: "op-palette", role: "dialog", "aria-label": "Command palette" },
      h("div", { className: "op-palette-search" }, h(Icon, { name: "search", size: 18 }), h("input", { className: "op-palette-input", placeholder: "Search pages and actions…", "aria-label": "Search pages and actions", defaultValue: p.query || "", autoComplete: "off", spellCheck: false }), h(Kbd, null, "Esc")),
      h("div", { className: "op-palette-list", role: "listbox" }, (p.sections || []).map(function (s) {
        return h("div", { key: s.name, role: "group", "aria-label": s.name }, h("div", { className: "op-palette-group" }, s.name), s.items.map(function (c) {
          idx += 1; var i = idx; var on = i === active;
          return h("div", { key: c.id, role: "option", "aria-selected": on, className: "op-palette-item", onMouseMove: function () { setActive(i); }, onClick: function () { c.run && c.run(); } },
            h("span", { className: "op-palette-icon" }, h(Icon, { name: c.icon || "house", size: 14 })), h("span", { className: "op-palette-label" }, c.label),
            c.hint && h("span", { className: "op-palette-hint" }, c.hint), c.shortcut && h(Kbd, null, c.shortcut), on && h(Icon, { name: "corner-down-left", size: 14, className: "op-muted" }));
        }));
      })),
      h("div", { className: "op-palette-foot" }, h("span", null, h(Kbd, null, "↑"), h(Kbd, null, "↓"), " to move"), h("span", null, h(Kbd, null, "↵"), " to open"), h("span", null, h(Kbd, null, "Esc"), " to close")));
  }
  function NavItem(p) {
    return h("a", { href: p.href || "#", "aria-current": p.active ? "page" : undefined, className: "op-nav", onClick: function (e) { e.preventDefault(); p.onSelect && p.onSelect(); } },
      h(Icon, { name: p.icon || "house", strokeWidth: 1.9 }), h("span", { className: "op-nav-text" }, p.label));
  }
  NavItem.Group = function (p) {
    return h("div", { style: { marginBottom: 12 } },
      h("button", { type: "button", className: "op-nav-group", "aria-expanded": true }, h("span", { style: { flex: 1 } }, p.label)),
      h("ul", { className: "op-nav-list" }, React.Children.map(p.children, function (c) { return h("li", null, c); })));
  };

  function Drawer(p) {
    if (p.open === false) return null;
    return h("div", { className: "op-scrim op-scrim-right", role: "presentation" }, h("aside", { role: "dialog", "aria-modal": "true", className: "op-drawer" },
      h("div", { className: "op-modal-head" }, h("h2", { className: "op-h3" }, p.title), h(IconButton, { label: "Close", size: "icon-sm", onClick: p.onClose }, h(Icon, { name: "x" }))),
      h("div", { className: "op-drawer-body" }, p.children)));
  }
  function Toaster(p) {
    return h("div", { className: "op-toaster" }, (p.toasts || []).map(function (t) { return h(Toast, { key: t.id, tone: t.tone, onDismiss: function () { p.onDismiss && p.onDismiss(t.id); } }, t.msg); }));
  }
  function Sidebar(p) {
    return h("aside", { className: "op-sidebar" }, h("nav", { className: "op-sidebar-nav", "aria-label": "Main" },
      h("div", { className: "op-sidebar-brand" }, h(BrandMark, { name: p.brand, logo: p.logo }), h("div", { className: "op-sidebar-brand-text" }, h("div", { className: "op-sidebar-brand-name" }, p.brand), h("div", { className: "op-sidebar-brand-tenant" }, p.tenant))),
      h("div", { className: "op-sidebar-list" }, (p.sections || []).map(function (s) {
        return h(NavItem.Group, { key: s.group, label: s.group }, s.items.map(function (it) { return h(NavItem, { key: it.key, label: it.label, icon: it.icon, active: it.key === p.page, onSelect: function () { p.onNavigate && p.onNavigate(it.key); } }); }));
      })),
      h("div", { className: "op-sidebar-foot" }, h(Avatar, { name: p.user || "", small: true }), h("div", { className: "op-sidebar-user" }, h("div", { className: "op-sidebar-user-name" }, p.user), h("div", { className: "op-sidebar-user-role" }, p.role)), h(IconButton, { label: "Sign out", size: "icon-sm", onClick: p.onLogout }, h(Icon, { name: "log-out" })))));
  }
  function TabBar(p) {
    var inTabs = p.tabs.some(function (t) { return t.key === p.page; });
    return h("nav", { "aria-label": "Shortcuts", className: "op-tabbar" }, h("ul", { className: "op-tabbar-list", style: { gridTemplateColumns: "repeat(" + (p.tabs.length + 1) + ", minmax(0, 1fr))" } },
      p.tabs.map(function (t) {
        var active = t.key === p.page;
        return h("li", { key: t.key }, h("a", { href: "#", "aria-current": active ? "page" : undefined, className: cx("op-tab-item", active && "active"), onClick: function (e) { e.preventDefault(); p.onNavigate && p.onNavigate(t.key); } },
          active && h("span", { "aria-hidden": true, className: "op-tab-item-bar" }), h(Icon, { name: t.icon || "house", size: 21, strokeWidth: active ? 2.1 : 1.8 }), h("span", { className: "op-tab-item-label" }, t.short || t.label)));
      }),
      h("li", null, h("button", { type: "button", "aria-label": "More pages", className: cx("op-tab-item", !inTabs && "active"), onClick: p.onMore }, h(Icon, { name: "layout-grid", size: 21, strokeWidth: 1.8 }), h("span", { className: "op-tab-item-label" }, "More")))));
  }

  window.Operator = { Button: Button, IconButton: IconButton, Badge: Badge, Pill: Pill, Kbd: Kbd, Avatar: Avatar, BrandMark: BrandMark, Card: Card, Stat: Stat, PageHeader: PageHeader, Field: Field, Input: Input, TextArea: TextArea, Select: Select, Tabs: Tabs, Segmented: Segmented, Table: Table, Empty: Empty, ErrorBox: ErrorBox, Skeleton: Skeleton, Toast: Toast, Citation: Citation, RunSteps: RunSteps, Modal: Modal, CommandPalette: CommandPalette, NavItem: NavItem, Drawer: Drawer, Toaster: Toaster, Sidebar: Sidebar, TabBar: TabBar, Icon: Icon, cx: cx };
})();
