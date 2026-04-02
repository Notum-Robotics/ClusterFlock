# Notum AHI — Automatic Human Interface

> **[Notum Robotics](https://n-r.hr)** — Zero-dependency UI for machines and humans.

A self-contained UI/UX component library built on the principles of **Functional Realism** and **Utilitarian Necessity**. Zero external dependencies — all CSS, JavaScript, fonts, and icons are bundled locally.

---

## 🤖 Notum AHI — Automatic Human Interface for AI Agents

**Notum AHI** is a declarative JSON protocol layer that lets **coding agents**, **tool-calling LLMs**, **MCP servers**, and **any AI system** build rich, interactive UIs for humans — on the fly, without touching the DOM.

**You send JSON → Humans see a dashboard → They interact → You get structured events back.**

### Agent Quick Start

```python
# Python agent via WebSocket
ws.send(json.dumps({
    "jsonrpc": "2.0", "method": "render", "id": 1,
    "params": { "controls": [
        {"id": "light", "type": "card", "cols": 2, "rows": 2,
         "state": "on", "icon": "ph-lightbulb", "name": "AMBIENT",
         "on": "ACTIVE", "off": "INACTIVE"},
        {"id": "brightness", "type": "slider", "cols": 4, "rows": 2,
         "label": "BRIGHTNESS", "max": 20, "value": 14}
    ]}
}))
```

### AHI Features

- **15 tool calls**: `render`, `patch`, `insert`, `remove`, `dialog`, `dismiss`, `lock`, `unlock`, `read`, `toast`, `notify`, `flow`, `ping`, `init`, `destroy`
- **14 control types**: card, slider, toggle, button, stepper, bar, status, gauge, wave, matrix, ring, spark, scope, level
- **9 card sizes**: 3×3, 3×2, 2×3, 2×2, 3×1, 2×1, 1×3, 1×2, 1×1
- **AHI extensions**: `id`, `disabled`, `hidden`, `badge`, `tooltip`, `confirm`, `progress`, `status`, `onEvent`
- **Multi-step flows**: Chain dialogs, renders, notifications, and toasts into wizards with `$back`/`$abort` navigation
- **3 transport modes**: Embedded (same process), WebSocket (remote agent), PostMessage (iframe)
- **JSON-RPC 2.0** protocol, fully schema-validated (`js/ahi/ahi-schema.json`)
- **Procedural audio** feedback via nbeep.js

### AHI Files

| File | Description |
|------|-------------|
| `js/ahi/ahi.js` | Core AHI API — global `notumAHI` |
| `js/ahi/ahi-protocol.js` | JSON-RPC 2.0 message handler |
| `js/ahi/ahi-transport-ws.js` | WebSocket + PostMessage adapters |
| `js/ahi/ahi-flow.js` | Multi-step wizard engine |
| `js/ahi/ahi-schema.json` | Full JSON Schema for validation |
| `CODING_AGENT.md` | **Complete agent reference** — tool calls, schemas, examples |

> **For AI agents:** Read [`CODING_AGENT.md`](CODING_AGENT.md) — it contains every tool call definition, parameter schema, event format, and usage example you need.

---

## Design System Quick Start

Open `index.html` in any browser. No build step, no server, no dependencies. The homepage is the AHI dashboard with scenario tabs showcasing all components.

```bash
# or use any local server:
python3 -m http.server 8000
# then open http://localhost:8000
```

## Structure

```
index.html              ← Homepage (AHI dashboard + scenarios)
ahi.html                ← AHI standalone page
components.html         ← Component showcase page
dynamic.html            ← Dynamic grid layout demo
interactive.html        ← Interactive editor demo
DESIGN_GUDELINES.md     ← Full design system specification
CODING_AGENT.md         ← AI agent / LLM reference (AHI framework)
css/
  style.css             ← Core design tokens & base styles
  notum.css             ← Component styles (single source of truth)
  subpage.css           ← Shared sub-page viewport layout + .nd-grid overrides
  demo.css              ← Deprecated (empty)
  phosphor.css          ← Phosphor icon font stylesheet
js/
  nRegistry.js          ← Module dependency system (Notum.register/require)
  nUtils.js             ← Shared utilities (escHtml, flashOutline, etc.)
  nComp.js              ← Component properties (progress, status, active)
  nCatalog.js           ← Shared control catalog (GRID_CATALOG)
  nbeep.js              ← Procedural audio engine
  nDynamic.js           ← Viewport-filling grid layout engine
  nInteractive.js       ← Hold-to-edit interaction layer
  nNotify.js            ← In-grid notification system
  nStore.js             ← Reactive state store with auto-rendering
  notum.js              ← Legacy component page behaviors
  notumDemo.js          ← Demo wiring for components.html showcase
  ahi/                  ← Automatic Human Interface framework
    ahi.js              ← AHI core API (notumAHI)
    ahi-protocol.js     ← JSON-RPC message handler
    ahi-transport-ws.js ← WebSocket + PostMessage transport
    ahi-flow.js         ← Flow/wizard engine
    ahi-schema.json     ← JSON Schema for AHI controls & protocol
fonts/                  ← Self-hosted .woff2 fonts (Rajdhani, IBM Plex Mono, Phosphor)
```

## Design Principles

- **Dark, high-contrast palette** — near-black backgrounds, crisp white text, electric cyan accent
- **Sharp geometry** — 0–2px border radii, hairline borders, corner anchor glyphs on all containers
- **Monospace data** — IBM Plex Mono for dynamic values; Rajdhani for labels
- **Mechanical motion** — no easing, no bounce; instant snaps, data-scramble reveals, strobe feedback
- **Self-hosted everything** — zero CDN calls, works fully offline / air-gapped

## Components

| Section | Description |
|---------|-------------|
| Buttons | Default, primary, warning, danger variants with flash feedback |
| Dialogs | Promise-based modal system with dithered overlay |
| Toggle Groups | Mutually exclusive option selectors |
| Segmented Sliders | Horizontal and vertical draggable bar-segment controls |
| Segmented Bars | Horizontal and vertical static progress indicators with travel/blink animations |
| Numeric Steppers | Increment/decrement inputs |
| Icon Toggle Buttons | Compact icon-only on/off toggles for toolbar layouts |
| Sub-menus | List items with status values, chevrons, and color variants |
| Status Indicators | Key-value readout rows with color coding |
| Active State Cards | Toggleable cards with 9 size variants (1×1 through 3×3) |
| Gauge | Semicircular arc gauge with needle, SVG ticks, and `[ XX% ]` readout |
| Wave | Multi-layer animated waveform — pattern derived from widget name via hash |
| Matrix | Deterministic symmetric dot-matrix — pattern derived from name + value |
| Ring | Concentric arc chart (1–4 rings) with per-ring name/value corner labels |
| Sparkline | Rolling time-series canvas chart with gradient fill and glow dot |
| Scope | Oscilloscope display with phosphor afterglow, grid, and scanning line |
| Level Meter | Vertical segmented meter with warn/crit color zones and peak hold |
| Auto-fit Grid | Bin-packed responsive layout with mixed components |
| Component Properties | nComp API — attach progress bars, status pips, active states to any element |
| Toast Notifications | Ephemeral stacking messages with severity levels (AHI) |
| In-Grid Notifications | Rate-limited, queued, in-grid notifications with dithered overlay and corner flashers (nNotify) |

> **For usage details:** See [`USAGE.md`](USAGE.md) — complete HTML markup examples, API references, and configuration for every component.

## License

MIT License — Copyright (c) 2026 [Notum Robotics](https://n-r.hr)

