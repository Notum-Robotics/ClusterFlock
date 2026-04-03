# Notum AHI — Automatic Human Interface for Autonomous Agents

### Coding Agent & Tool-Calling LLM Reference

Copyright © 2026 [Notum Robotics](https://n-r.hr). Licensed under the MIT License.

---

## What Is This?

**Notum AHI** (Automatic Human Interface) is a framework that lets **coding agents**, **tool-calling LLMs**, **MCP servers**, and **any AI system** build rich, interactive UIs for humans — on the fly, from pure JSON.

Built on [Notum AHI](https://n-r.hr) — a battle-tested, zero-dependency, dark-themed component library designed for mission-critical dashboards — AHI adds a **declarative JSON protocol layer** that machines can drive without ever touching the DOM.

**You send JSON. Humans see a dashboard. They interact. You get structured events back.**

---

You are an Autonomous Interface Controller. Your environment is connected to the Notum AHI (Automatic Human Interface) system. 

Your sole method of interacting with the user interface is by emitting strict JSON-RPC tool calls. You do not write HTML, you do not write CSS, and you do not manipulate the DOM. You act as the backend logic sending strictly typed JSON configuration to the Notum renderer.

CRITICAL CONSTRAINTS (DO NOT VIOLATE):
1. NO CSS OR HTML: Never attempt to inject inline styles (`style="color: red"`), custom CSS classes, or raw HTML tags. The Notum AHI renderer handles 100% of the styling autonomously.
2. NO HALLUCINATED ELEMENTS: You may only use the exact `type` strings listed below (e.g., 'card', 'slider', 'button'). Do not invent new types like 'dropdown', 'input', or 'modal'.
3. NO HALLUCINATED PROPERTIES: Stick strictly to the fields documented for each control. Do not invent properties like `backgroundColor`, `fontSize`, `onClick`, or `className`.
4. STRICT COLOR PALETTE: When a color field is available, you may ONLY use the exact string values: 'accent' (cyan), 'amber', or 'danger' (red). Do not use hex codes or standard web colors.

AVAILABLE TOOLS:
- ahi_render(controls, config?) — Display a UI dashboard. Controls auto-fill the viewport.
- ahi_patch(id, changes) — Update a control in place (value, state, disabled, progress, etc.)
- ahi_insert(index?, control) — Add a new control to the layout.
- ahi_remove(id) — Remove a control from the layout.
- ahi_dialog(title, body, buttons) — Show a modal dialog, returns the user's choice.
- ahi_dismiss(value?) — Close the current dialog programmatically.
- ahi_toast(message, level?, duration?) — Show a brief notification. Levels: info, warn, error, ok.
- ahi_read(id?) — Read current state of controls (syncs live DOM values).
- ahi_lock(id) / ahi_unlock(id) — Disable/enable a control.
- ahi_flow(steps) — Multi-step wizard (chains dialogs, renders, notifications, toasts, waits, patches).

CONTROL TYPES (Exact strings only):
- card: Toggleable state card (on/off) with icon and label. 
- slider: Draggable segmented slider. cols: 2-4, rows: 2.
- toggle: Mutually exclusive option group. cols: 2, rows: 1.
- button: Clickable action. Styles: primary, warning, danger. cols: 1-2, rows: 1.
- stepper: Numeric +/- control. Value clamped >= 0. cols: 1, rows: 2.
- bar: Read-only animated progress bar. cols: 2, rows: 1.
- status: Key-value readout panel. Items have k (label), v (value), c (color class). cols: 2, rows: 2.
- gauge: Semicircular arc gauge. Read-only. cols: 2, rows: 2.
- wave: Multi-layer animated waveform. cols: 2, rows: 2.
- matrix: Deterministic symmetric dot-matrix. cols: 2, rows: 2.
- ring: Concentric arc chart (1-4 rings). cols: 2, rows: 2.
- spark: Rolling sparkline chart. cols: 2, rows: 1.
- scope: Oscilloscope with phosphor afterglow. cols: 2, rows: 2.
- level: Vertical segmented meter. cols: 1, rows: 2.

AHI EXTENSION FIELDS (apply to ANY control):
- id (string): Stable identifier for patch/lock/read/events. Always assign one.
- disabled (bool): Grayed out, no interaction, no events.
- hidden (bool): Invisible but occupies grid space.
- badge (string|number|null): Small overlay indicator at top-right corner.
- tooltip (string): Hover text explaining the control.
- confirm (DialogDefinition): Guard dialog — shown before event fires.
- progress (number|null): Inline progress bar. 0-100 = determinate, -1 = indeterminate, null = remove.
- status (string|null): Status pip. 'ok', 'warn', 'error', 'busy', null to remove.

ICONS: Use Phosphor icons (ph-<name>). Common: ph-lightbulb, ph-gear, ph-warning, ph-check, ph-x, ph-rocket, ph-lock, ph-cpu, ph-cloud-arrow-up.

BEST PRACTICES:
- Give every control a stable 'id'.
- Combine progress: -1 with status: 'busy' while waiting, then progress: 100 + status: 'ok' when done.
- Use ahi_patch for incremental updates — avoid re-rendering the layout when one value changes.
- Remember: You are emitting JSON configuration, not designing a webpage. Let the framework handle the pixels.

## Summary

### Control Types

**Card** (`type: 'card'`)
- Sizes (via `data-size`): `1x1`, `1x2`, `1x3`, `2x1`, `2x2` (default), `2x3`, `3x1`, `3x2`, `3x3`
- States: `.is-on` / `.is-off` — toggles on click
- Properties: `icon`, `name`, `on` (active label), `off` (inactive label), `state` (`'on'`/`'off'`)
- Corner bracket ornaments (⌜⌝⌞⌟) strobe sequentially when ON; border = cyan ON, red OFF
- Size-specific layout: `3x3`/`2x2`/`2x3`/`1x2` = vertical stacked, `3x2`/`3x1`/`2x1` = horizontal, `1x1` = icon only (name/state hidden), `3x1`/`2x1` = state text hidden

**Button** (`type: 'button'`)
- Style variants: (none) = default/dim, `primary` (cyan), `warning` (amber), `danger` (red)
- Attributes:
  - `data-flash` — enables click outline strobe
  - `data-lockout="ms"` — temporary lockout after click (cursor → `wait`, `.n-locked` applied)
  - `data-dialog='...'` — opens a dialog on click
  - `data-nbeep="string"` — override the audio beep text
- Icon support: Phosphor icon class via `icon` property
- Three button forms: `.action-btn` (icon+label), `.icon-btn` (icon only, 40×40), `.grid-btn` (grid-placed)

**Segmented Slider** (`type: 'slider'`)
- Attributes: `data-max`, `data-value`, `data-color`
- Colors: `accent` (cyan), `amber`, `danger` (red)
- Orientations: Horizontal (`.seg-slider`), Vertical (`.seg-slider-v`)
- Interaction: Click + drag, touch supported
- Readout: `[ XX% ]` or custom (e.g., temperature via `data-min`)
- Audio: Pitch-shifted beep on each value change

**Segmented Bar** (`type: 'bar'`) — Read-Only
- Attributes: `data-max`, `data-value`, `data-color`
- Colors: `accent`, `amber`, `danger`
- Orientations: Horizontal (`.seg-bar`), Vertical (`.seg-bar-v`)
- Animations:
  - Partial fill: traveling dim square (120ms/step)
  - Full fill: 2-flash blink (50ms on/off) + 350ms pause, repeating
  - Direction-aware: travel reverses when value decreases
- Animated transitions (nDynamic): Decrease = fade-out (150ms); Increase = bright flash

**Toggle Group** (`type: 'toggle'`)
- Properties: `label`, `options` (string array), `active` (index)
- Single-select, `.active` class on selected, flash outline on click

**Stepper** (`type: 'stepper'`)
- Properties: `label`, `value`
- `−` / `+` buttons, minimum clamped to 0
- Auto-switches to vertical layout when container <120px

**Status Panel** (`type: 'status'`)
- Properties: `label`, `items` (array of `{ k, v, c }`)
- Item colors (`c`): `accent`, `amber`, `danger`

**Sub-menu** (`.submenu-item`)
- Parts: Icon, name, value (colored), chevron
- Value colors: `.accent`, `.amber`, `.danger`
- Variants: `.warning` (amber icon), `.danger` (red icon)

**Icon Toggle** (`.icon-toggle`)
- States: `.is-on` / `.is-off` — toggles on click with flash

**Gauge** (`type: 'gauge'`)
- Properties: `label`, `value`, `max`, `color`
- Semicircular SVG arc with 11 tick marks, filled arc, rotating needle, center dot
- Readout: `[ XX% ]`

**Wave** (`type: 'wave'`)
- Properties: `label`, `value`, `max`, `color`
- Animated canvas with 3 sine wave layers
- Wave shape deterministically derived from `stringHash(label)` — each widget name produces a unique waveform
- Amplitude scales with `value/max`; distortion harmonic above 60%

**Matrix** (`type: 'matrix'`)
- Properties: `label`, `value`, `max`, `color`, `gridSize` (default 8)
- NxN symmetric dot-matrix pattern
- Pattern seed derived from `matrixHash(value) XOR stringHash(label)` — unique per name + value
- Readout: `[ XX% ]`

**Ring** (`type: 'ring'`)
- Properties: `items` (array of `{ name, value, max, color }`, max 4)
- Concentric SVG arcs with decreasing radii
- Per-ring corner labels: TL = ring 0, TR = ring 1, BL = ring 2, BR = ring 3
- Each corner shows name + `[ XX% ]` colored to match ring. No panel-label header.

**Sparkline** (`type: 'spark'`)
- Properties: `label`, `value`, `max`, `color`
- Rolling canvas chart with 60-point history buffer (~500ms sampling)
- Gradient fill, stroke line, glowing current-point dot, 4-division grid

**Scope** (`type: 'scope'`)
- Properties: `label`, `value`, `max`, `color`
- Oscilloscope canvas with compound sine trace + harmonics
- 8×4 division grid, vertical scanning line, green phosphor afterglow persistence

**Level Meter** (`type: 'level'`)
- Properties: `label`, `value`, `max` (default 20), `color`
- Vertical segmented meter with warn (≥70%) and crit (≥90%) color zones
- Topmost filled segment highlighted as `.peak-hold`
- Readout: `[ XX% ]`

---

### Component Properties System (nComp)

Attachable to **any DOM element** at runtime:

**Progress Bar** — `nComp.progress(el, value)`
- `0–100` — 10-segment bar at element bottom; partial = traveling dim square, full = blink; background fill synced to progress
- `-1` — indeterminate scan (single segment sweeps)
- `null` — remove
- Color inheritance: `.warning` = amber, `.danger` = red, default = cyan

**Status Pip** — `nComp.status(el, status)`
- `'ok'` — solid cyan pip (top-right)
- `'warn'` — solid amber pip
- `'error'` — solid red pip, blinking
- `'busy'` — solid cyan pip, blinking
- `null` — remove
- Side effect: element border color matches status

**Active State** — `nComp.active(el, value)`
- `true` — cyan border + text
- `false` — pointer-events disabled
- `null` — remove

**Disabled** — `.ahi-disabled` class
- Grayed out, pointer-events disabled, diagonal X overlay

---

### Flash / Lockout System (nUtils)

`flashOutline(el, onDone)`
- Strobes `.flash-outline` (border → white, corners flash)
- Duration: 200ms. Returns `false` if already mid-flash (prevents double-clicks)
- `data-lockout="ms"` — extends lockout; `.n-locked` applied (cursor: wait). Flash tick = 150ms for lockouts ≥400ms

---

### Dialog System

`showDialog(opts)` → `Promise<string|null>`
- `title`, `body`, `buttons` (`{ label, value, style? }`)
- Button styles: `primary`, `warning`, `danger`
- `alarm` — `true` (default) / `false` to suppress looping beep
- Dithered overlay, corner strobe, looping dual-tone beep until dismissed
- Dismiss: click overlay → `null`, click button → `value`

---

### Notification System (nNotify)

`nNotify.show({ icon, title, subtitle, linger? })`
- Overlays top row of grid with dithered grayout + notification box
- Rate-limited: max 1 per 30s (configurable), excess queued
- Dismiss: tap/click, or auto after linger (default: 4000ms)
- Corner strobe: 4 bursts at 50ms, 300ms pause

`nNotify.config({ rateLimit?, linger?, fade? })`

---

### Audio System (nbeep)

`nbeep(text, loop?, pitchMultiplier?)`
- Deterministic: same text + seed = identical sound
- Mono: one sound at a time; new call kills previous
- `loop = false` — single shot
- `loop = true` — repeating
- `loop = "altText"` — dual-text alternating A↔B
- `pitchMultiplier` — 0.5 = octave down, 1.0 = normal, 2.0 = octave up

**Sound Modes** (`config.soundMode`): `standard`, `harmonic`, `ncars`, `ncars2`

**Musical Scales** (`config.scale`): `pentatonic`, `minor_pentatonic`, `chromatic`, `whole_tone`, `lydian`, `dorian`

**Config**: `globalSeed` (default: 2026), `masterVolume` (0–1), `maxDuration` (0–1), `useMusicalScale`, `durationMin`/`durationMax`, `allowedWaveforms`, `fadeDuration`

---

### Grid Layout Engine (nDynamic)

`nDynamic.init(selector, controls, config?)`

**Control Schema**: `{ type, cols, rows, size?, label?, icon?, state?, value?, max?, color?, options?, active?, items?, style?, lockout?, dialog? }`

**Config**: `cols`, `rowHeight`, `gap` (6), `padding` (16), `order` (index array), `pinned` (`{ index: { col, row } }`)

- Viewport-filling, auto cols/rows, bin-packing (`grid-auto-flow: dense`)
- Auto-resize via ResizeObserver
- `nDynamic.update()`, `.rebuild()`, `.destroy()`, `.updateSegBar(el, newValue)`

---

### Interactive Editor (nInteractive) — *Internal*

> nInteractive is an **internal module** consumed by `notumAHI.render()`. Agents should not call it directly. The docs below describe the human-facing editing experience it provides.

- **Normal mode**: controls work normally
- **Hold 5s** → edit mode
- **Edit mode**:
  - Drag → ghost + drop indicator → release to reorder (collision-detected)
  - Hold 2s / right-click → context menu
  - Tap empty / Escape → exit

**Context Menu**: Lock/Unlock position, Mute/Unmute sounds, Soundscape theme (per-control), Sound hash (001–999), Resize (9 sizes: 1×1 through 3×3)

**Config**: `holdEnterMs` (5000), `holdContextMs` (2000), `onEditChange(isEditing)`

---

### Design Tokens

| Token | Value |
|---|---|
| `--bg` | `#0a0a0a` |
| `--surface` | `#141414` |
| `--border` | `rgba(244,244,244, 0.10)` |
| `--text` | `#F4F4F4` |
| `--text-dim` | `rgba(244,244,244, 0.40)` |
| `--accent` | `#00e5ff` (cyan) |
| `--danger` | `#FF3333` (red) |
| `--amber` | `#FFB300` |
| `--radius` | `2px` |

**Fonts**: Rajdhani (400–700) for labels, IBM Plex Mono (400–500) for data. Icons: Phosphor.

**Connection Indicator**: `.conn.on` = solid cyan, `.conn.off` = red blinking

**Responsive**: >768px 3-col, >420px 2-col, <600px 2-col grid, `prefers-reduced-motion` disables all animations

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Quick Start](#quick-start)
3. [File Structure](#file-structure)
4. [Load Order](#load-order)
5. [Tool Call Definitions](#tool-call-definitions)
   - [ahi_render](#ahi_render)
   - [ahi_patch](#ahi_patch)
   - [ahi_insert](#ahi_insert)
   - [ahi_remove](#ahi_remove)
   - [ahi_dialog](#ahi_dialog)
   - [ahi_dismiss](#ahi_dismiss)
   - [ahi_lock](#ahi_lock)
   - [ahi_unlock](#ahi_unlock)
   - [ahi_read](#ahi_read)
   - [ahi_toast](#ahi_toast)
   - [ahi_notify](#ahi_notify)
   - [ahi_flow](#ahi_flow)
   - [ahi_ping](#ahi_ping)
   - [ahi_init](#ahi_init)
   - [ahi_destroy](#ahi_destroy)
6. [Control Schema Reference](#control-schema-reference)
   - [Card](#card)
   - [Slider](#slider)
   - [Toggle](#toggle)
   - [Button](#button)
   - [Stepper](#stepper)
   - [Bar](#bar)
   - [Status](#status)
   - [Gauge](#gauge)
   - [Wave](#wave)
   - [Matrix](#matrix)
   - [Ring](#ring)
   - [Sparkline](#sparkline)
   - [Scope](#scope)
   - [Level Meter](#level-meter)
7. [AHI Extension Fields](#ahi-extension-fields)
8. [Component Properties — Progress, Status & Active States](#component-properties--progress-status--active-states)
9. [Layout Configuration](#layout-configuration)
10. [Dialog Definition](#dialog-definition)
11. [Event Schema](#event-schema)
12. [Flows (Multi-Step Wizards)](#flows-multi-step-wizards)
13. [Transport Modes](#transport-modes)
    - [Embedded (Same Process)](#embedded-same-process)
    - [WebSocket (Remote Agent)](#websocket-remote-agent)
    - [PostMessage (Iframe Sandbox)](#postmessage-iframe-sandbox)
    - [JSON-RPC Protocol](#json-rpc-protocol)
14. [MCP Integration Guide](#mcp-integration-guide)
15. [System Prompt Snippet](#system-prompt-snippet)
16. [Examples](#examples)
17. [Static HTML Usage (Non-AHI)](#static-html-usage-non-ahi)
18. [Icon Reference](#icon-reference)
19. [License](#license)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│  AGENT  (LLM / MCP Server / Coding Agent)           │
│                                                       │
│  Emits tool calls:                                    │
│    ahi_render({ controls: [...] })                     │
│    ahi_dialog({ title: '...', buttons: [...] })        │
│    ahi_patch('my_slider', { value: 15 })               │
│                                                       │
│  Receives events:                                     │
│    { event: 'toggle', id: 'light', value: 'off' }    │
│    { event: 'slider_change', id: 'vol', value: 12 }  │
└──────────────┬──────────────────────────┬─────────────┘
               │  JSON-RPC / Tool Calls   │  Events (JSON)
               ▼                          ▲
┌──────────────────────────────────────────────────────┐
│  NOTUM AHI  (Browser)                                 │
│                                                       │
│  js/ahi/                                              │
│    ahi.js ─── ahi-protocol.js ─── ahi-transport-ws.js │
│      │                                                │
│      └── ahi-flow.js  (wizard engine)                 │
│                                                       │
│  js/nDynamic.js (grid layout engine)                  │
│  js/nNotify.js  (in-grid notifications)               │
│  js/nbeep.js    (procedural audio)                    │
│                                                       │
│  CSS: notum.css + style.css + phosphor.css            │
└──────────────────────────────────────────────────────┘
```

**Key principle:** The agent never touches the DOM. It sends declarative JSON describing *what* should exist. Notum AHI handles rendering, layout, interaction wiring, audio feedback, and event collection. The human sees a polished, responsive dashboard. The agent gets structured events back.

### Rendering Hierarchy

```
notumAHI.render()          ← The ONLY entry point for rendering controls
  └── nInteractive.init()  ← Internal — called automatically by AHI
        └── nDynamic.init() ← Internal — called automatically by nInteractive
```

> **⚠ DO NOT call `nInteractive.init()` or `nDynamic.init()` directly.**
> These are internal modules. Calling them directly bypasses AHI's control
> registry and breaks `patch()`, `lock()`, `read()`, `badge`, and all id-based operations.
> Always go through `notumAHI.render()` → `notumAHI.patch()` → `notumAHI.onEvent()`.

---

## Quick Start

### Minimal HTML Page

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Notum AHI</title>
  <link rel="stylesheet" href="css/phosphor.css">
  <link rel="stylesheet" href="css/style.css">
  <link rel="stylesheet" href="css/notum.css">
</head>
<body>
  <div id="ahi-root"></div>
  <div id="dialog-overlay"><div id="dialog-box"></div></div>

  <script src="js/nRegistry.js"></script>
  <script src="js/nbeep.js"></script>
  <script src="js/nUtils.js"></script>
  <script src="js/nComp.js"></script>
  <script src="js/nCatalog.js"></script>
  <script src="js/nDynamic.js"></script>
  <script src="js/nInteractive.js"></script>
  <script src="js/nNotify.js"></script>
  <script src="js/nStore.js"></script>
  <script src="js/ahi/ahi.js"></script>
  <script src="js/ahi/ahi-flow.js"></script>
  <script src="js/ahi/ahi-protocol.js"></script>
  <script src="js/ahi/ahi-transport-ws.js"></script>

  <script>
    // Initialize AHI
    notumAHI.init('#ahi-root');

    // Option A: Local agent (same process)
    notumAHI.render([
      { id: 'light', type: 'card', cols: 2, rows: 2,
        state: 'on', icon: 'ph-lightbulb', name: 'AMBIENT',
        on: 'ACTIVE', off: 'INACTIVE' }
    ]);
    notumAHI.onEvent(function (evt) { console.log(evt); });

    // Option B: Remote agent via WebSocket
    // notumAHITransportWS.connect('ws://localhost:9100');
  </script>
</body>
</html>
```

### From an Agent (Pseudocode)

```python
# Python agent sending tool calls to AHI via WebSocket
ws.send(json.dumps({
    "jsonrpc": "2.0",
    "method": "render",
    "params": {
        "controls": [
            {"id": "deploy", "type": "button", "cols": 2, "rows": 1,
             "label": "DEPLOY", "style": "danger", "icon": "ph-rocket"},
            {"id": "status", "type": "status", "cols": 2, "rows": 2,
             "label": "PIPELINE", "items": [
                 {"k": "STAGE", "v": "BUILD", "c": "accent"},
                 {"k": "ETA",   "v": "~2m",   "c": "amber"}
             ]}
        ]
    },
    "id": 1
}))

# Agent receives event when human clicks DEPLOY:
# {"jsonrpc":"2.0","method":"event","params":
#   {"event":"button_click","id":"deploy","value":"DEPLOY","timestamp":...}}
```

---

## File Structure

```
notum_design_system/
├── CODING_AGENT.md        ← This file (agent reference)
├── js/
│   ├── ahi/               ← AHI framework modules
│   │   ├── ahi.js              ← Core AHI API (global: notumAHI)
│   │   ├── ahi-protocol.js     ← JSON-RPC message handler
│   │   ├── ahi-transport-ws.js ← WebSocket + PostMessage adapters
│   │   ├── ahi-flow.js         ← Multi-step wizard engine
│   │   └── ahi-schema.json     ← JSON Schema for all types
│   ├── nRegistry.js       ← Module dependency system (must load first)
│   ├── nDynamic.js        ← Grid layout engine (dependency)
│   ├── nbeep.js           ← Procedural audio (optional)
│   ├── nUtils.js          ← Shared utilities (flashOutline, escHtml)
│   ├── nComp.js           ← Component properties (progress, status, active)
│   ├── nCatalog.js        ← Shared control catalog
│   ├── nInteractive.js    ← Human layout editor (internal to AHI — do not call directly)
│   ├── nNotify.js         ← In-grid notification system (optional)
│   ├── nStore.js          ← Reactive state store with auto-rendering
│   ├── notum.js           ← Legacy component demo (not needed for AHI)
│   └── notumDemo.js       ← Demo wiring for components.html
├── css/
│   ├── notum.css          ← Component styles (single source of truth)
│   ├── style.css          ← Base design tokens
│   ├── subpage.css        ← Sub-page viewport layout + .nd-grid overrides
│   └── phosphor.css       ← Icon font
├── fonts/                 ← Self-hosted .woff2 fonts
└── ...
```

> **⚠ Deployment Best Practice — Keep Files Together**
>
> All Notum AHI files (`js/`, `css/`, `fonts/`, and the HTML entry point) are designed to work as a self-contained unit with relative paths between them. **Never cherry-pick individual files into scattered locations.** Instead, copy or move the entire `notum_ahi/` folder as-is into your web server's `/static` directory (or equivalent static-asset root). This preserves all internal references — font paths in CSS, script load order, icon sheets — and avoids broken links or missing assets. The folder is dependency-free and ready to serve from any static file host.

---

## Load Order

```html
<!-- Required (in this order) -->
<script src="js/nRegistry.js"></script>        <!-- Module system (must be first) -->
<script src="js/nbeep.js"></script>            <!-- Audio (optional but recommended) -->
<script src="js/nUtils.js"></script>           <!-- Shared utilities -->
<script src="js/nComp.js"></script>            <!-- Component properties -->
<script src="js/nCatalog.js"></script>         <!-- Control catalog -->
<script src="js/nDynamic.js"></script>         <!-- Layout engine -->
<script src="js/nNotify.js"></script>          <!-- In-grid notifications (optional) -->
<script src="js/nStore.js"></script>           <!-- Reactive state store (optional) -->
<script src="js/ahi/ahi.js"></script>          <!-- Core AHI API -->

<!-- Optional extensions -->
<script src="js/ahi/ahi-flow.js"></script>          <!-- Flow/wizard engine -->
<script src="js/ahi/ahi-protocol.js"></script>      <!-- JSON-RPC layer -->
<script src="js/ahi/ahi-transport-ws.js"></script>   <!-- WebSocket/PostMessage -->
```

---

## Tool Call Definitions

Below are all tool calls an agent can make. Each is documented with its JSON-RPC method name, parameter schema, return value, and usage examples.

### ahi_render

**Replace the entire UI with a new set of controls.**

This is the primary method. Call it to build a dashboard from scratch.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `controls` | `Control[]` | Yes | Array of control definition objects |
| `config` | `LayoutConfig` | No | Layout configuration overrides |

**Returns:** `{ ok: true, count: <number> }`

**Tool definition (for LLM function calling):**

```json
{
  "name": "ahi_render",
  "description": "Display a UI dashboard to the user. Controls automatically fill the viewport in an optimal grid layout. The user can interact with toggles, sliders, buttons, etc. Their actions generate events you receive.",
  "parameters": {
    "type": "object",
    "properties": {
      "controls": {
        "type": "array",
        "description": "Array of UI controls to display. Each control has a type, grid size (cols/rows), and type-specific properties.",
        "items": { "$ref": "ahi-schema.json#/definitions/Control" }
      },
      "config": {
        "type": "object",
        "description": "Optional layout configuration (cols, gap, padding, etc.)",
        "properties": {
          "cols": { "type": "integer", "description": "Force column count (default: auto)" },
          "gap": { "type": "integer", "description": "Grid gap in px (default: 6)" },
          "padding": { "type": "integer", "description": "Container padding in px (default: 16)" }
        }
      }
    },
    "required": ["controls"]
  }
}
```

**Example:**

```json
{
  "jsonrpc": "2.0",
  "method": "render",
  "params": {
    "controls": [
      {
        "id": "main_light",
        "type": "card",
        "cols": 2, "rows": 2,
        "state": "on",
        "icon": "ph-lightbulb",
        "name": "MAIN LIGHT",
        "on": "ACTIVE",
        "off": "INACTIVE"
      },
      {
        "id": "brightness",
        "type": "slider",
        "cols": 4, "rows": 2,
        "label": "BRIGHTNESS",
        "max": 20,
        "value": 14,
        "color": "accent"
      },
      {
        "id": "mode",
        "type": "toggle",
        "cols": 2, "rows": 1,
        "label": "MODE",
        "options": ["AUTO", "MANUAL", "SCHEDULE"],
        "active": 0
      },
      {
        "id": "apply",
        "type": "button",
        "cols": 1, "rows": 1,
        "label": "APPLY",
        "style": "primary",
        "icon": "ph-check"
      },
      {
        "id": "reboot",
        "type": "button",
        "cols": 1, "rows": 1,
        "label": "REBOOT",
        "style": "danger",
        "icon": "ph-arrows-clockwise",
        "confirm": {
          "title": "Confirm Reboot",
          "body": "This will restart the system. Continue?",
          "buttons": [
            { "label": "CANCEL", "value": "no" },
            { "label": "REBOOT", "value": "yes", "style": "danger" }
          ]
        }
      }
    ]
  },
  "id": 1
}
```

---

### ahi_patch

**Update a specific control's properties without re-rendering everything.**

Efficient for live updates (progress bars, value changes, status updates). Bar value patches trigger animated transitions — fade-out on decrease, flash on increase. Progress bar travel/scan animations are direction-aware: they sweep forward on increasing values and reverse on decreasing values.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | `string` | Yes* | Control id |
| `index` | `integer` | Yes* | Control index (alternative to id) |
| `changes` | `object` | Yes | Partial control properties to merge |

*One of `id` or `index` is required.

**Returns:** `{ ok: true }`

**Tool definition:**

```json
{
  "name": "ahi_patch",
  "description": "Update a specific UI control in place without re-rendering the full layout. Use for live value updates, progress changes, enabling/disabling controls, or changing status indicators.",
  "parameters": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "description": "Control id to update" },
      "changes": {
        "type": "object",
        "description": "Properties to update. Only specified fields change; others remain.",
        "properties": {
          "value": { "type": "integer", "description": "New value (sliders, steppers, bars)" },
          "state": { "type": "string", "enum": ["on", "off"], "description": "Card state" },
          "label": { "type": "string" },
          "disabled": { "type": "boolean" },
          "hidden": { "type": "boolean" },
          "progress": { "type": ["number", "null"] },
          "status": { "type": ["string", "null"] },
          "badge": { "type": ["string", "number", "null"] },
          "tooltip": { "type": "string" }
        }
      }
    },
    "required": ["id", "changes"]
  }
}
```

**Examples:**

```json
// Update a slider value
{ "method": "patch", "params": { "id": "brightness", "changes": { "value": 18 } }, "id": 2 }

// Show progress on a button
{ "method": "patch", "params": { "id": "deploy", "changes": { "progress": 45, "status": "busy" } }, "id": 3 }

// Disable a control
{ "method": "patch", "params": { "id": "reboot", "changes": { "disabled": true } }, "id": 4 }

// Add a badge
{ "method": "patch", "params": { "id": "inbox", "changes": { "badge": 3 } }, "id": 5 }

// Clear progress
{ "method": "patch", "params": { "id": "deploy", "changes": { "progress": null, "status": "ok" } }, "id": 6 }
```

---

### ahi_insert

**Add a new control to the layout.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `index` | `integer` | No | Position to insert at (-1 to append) |
| `control` | `Control` | Yes | Control definition object |

**Returns:** `{ ok: true }`

**Tool definition:**

```json
{
  "name": "ahi_insert",
  "description": "Add a new control to the current UI layout at the specified position.",
  "parameters": {
    "type": "object",
    "properties": {
      "index": { "type": "integer", "description": "Position (-1 to append at end)" },
      "control": { "type": "object", "description": "Control definition to add" }
    },
    "required": ["control"]
  }
}
```

---

### ahi_remove

**Remove a control from the layout.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | `string` | Yes* | Control id |
| `index` | `integer` | Yes* | Control index |

**Returns:** `{ ok: true }`

**Tool definition:**

```json
{
  "name": "ahi_remove",
  "description": "Remove a control from the UI layout.",
  "parameters": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "description": "Control id to remove" }
    },
    "required": ["id"]
  }
}
```

---

### ahi_dialog

**Show a modal dialog and wait for the user's response.**

This is a **blocking** tool call — it resolves when the user clicks a button or dismisses the dialog.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | `string` | Yes | Dialog title |
| `body` | `string` | No | Body text |
| `buttons` | `DialogButton[]` | Yes | Action buttons |
| `alarm` | `boolean` | No | Play looping beep alarm (default `true`). Pass `false` to suppress. |

**Returns:** `string` (the `value` of the clicked button) or `null` (if dismissed)

**Tool definition:**

```json
{
  "name": "ahi_dialog",
  "description": "Show a modal dialog to the user and wait for their response. Returns the value of the button they clicked, or null if they dismissed the dialog. Use for confirmations, choices, and alerts.",
  "parameters": {
    "type": "object",
    "properties": {
      "title": { "type": "string", "description": "Dialog title (uppercase recommended)" },
      "body": { "type": "string", "description": "Body/description text" },
      "alarm": { "type": "boolean", "description": "Play looping beep alarm when dialog opens (default true). Pass false to suppress." },
      "buttons": {
        "type": "array",
        "description": "Action buttons. Each has label (display text), value (returned on click), and optional style.",
        "items": {
          "type": "object",
          "properties": {
            "label": { "type": "string" },
            "value": { "type": "string" },
            "style": { "type": "string", "enum": ["", "primary", "warning", "danger"] }
          },
          "required": ["label", "value"]
        }
      }
    },
    "required": ["title", "buttons"]
  }
}
```

**Example:**

```json
{
  "method": "dialog",
  "params": {
    "title": "Deploy to Production",
    "body": "This will push build #847 to all 12 nodes. Rollback requires manual intervention.",
    "buttons": [
      { "label": "CANCEL",  "value": "cancel" },
      { "label": "STAGING", "value": "staging", "style": "warning" },
      { "label": "DEPLOY",  "value": "deploy",  "style": "danger" }
    ]
  },
  "id": 10
}
```

**Response (when user clicks DEPLOY):**

```json
{ "jsonrpc": "2.0", "result": "deploy", "id": 10 }
```

---

### ahi_dismiss

**Close the current dialog programmatically.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `value` | `string` | No | Value to resolve the dialog promise with |

**Returns:** `{ ok: true }`

---

### ahi_lock

**Disable interaction on a control** (grayed out, no events).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | `string` | Yes* | Control id |
| `index` | `integer` | Yes* | Control index |

**Returns:** `{ ok: true }`

**Tool definition:**

```json
{
  "name": "ahi_lock",
  "description": "Disable a UI control so the user cannot interact with it. The control appears grayed out.",
  "parameters": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "description": "Control id to lock" }
    },
    "required": ["id"]
  }
}
```

---

### ahi_unlock

**Re-enable interaction on a previously locked control.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | `string` | Yes* | Control id |
| `index` | `integer` | Yes* | Control index |

**Returns:** `{ ok: true }`

---

### ahi_read

**Read the current state of all controls** (or one specific control).

Useful for the agent to sync its model with the human's current view.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | `string` | No | Read a specific control (omit for all) |
| `index` | `integer` | No | Read by index |

**Returns:** `Control` object (if specific) or `Control[]` (if all)

**Tool definition:**

```json
{
  "name": "ahi_read",
  "description": "Read the current state of all UI controls or a specific one. Returns control definitions with live values from the DOM (slider positions, toggle states, card on/off, etc.).",
  "parameters": {
    "type": "object",
    "properties": {
      "id": { "type": "string", "description": "Optional: read only this control" }
    }
  }
}
```

---

### ahi_toast

**Show an ephemeral notification toast.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | `string` | Yes | Text to display |
| `level` | `string` | No | `'info'` \| `'warn'` \| `'error'` \| `'ok'` (default: `'info'`) |
| `duration` | `integer` | No | Display time in ms (default: 3000) |

**Returns:** `{ ok: true }`

**Tool definition:**

```json
{
  "name": "ahi_toast",
  "description": "Show a brief notification toast to the user. Auto-dismisses after the specified duration. Use for status updates, confirmations, or non-blocking alerts.",
  "parameters": {
    "type": "object",
    "properties": {
      "message": { "type": "string", "description": "Notification text" },
      "level": { "type": "string", "enum": ["info", "warn", "error", "ok"], "description": "Severity level (affects color)" },
      "duration": { "type": "integer", "description": "Display duration in ms (default: 3000)" }
    },
    "required": ["message"]
  }
}
```

---

### ahi_notify

**Display an in-grid notification over the top row of the bound grid container.** The notification is rate-limited (max once every 30 seconds by default) and queued. If nNotify is not initialized, falls back to a toast notification. Triggers audio feedback via nbeep if available.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `icon` | `string` | No | Phosphor icon class (e.g. `'ph-bell'`). Displayed in `--accent` color |
| `title` | `string` | No | Notification title. Rajdhani 700, uppercase, 55% white opacity |
| `subtitle` | `string` | No | Notification subtitle. IBM Plex Mono, `--text-dim` color |
| `linger` | `number` | No | Auto-dismiss time in ms (default: `4000`) |

**Returns:** `{ ok: true }`

**Tool definition:**

```json
{
  "name": "ahi_notify",
  "description": "Display an in-grid notification over the top row of the grid container. Rate-limited and queued. Falls back to toast if nNotify unavailable.",
  "parameters": {
    "type": "object",
    "properties": {
      "icon":     { "type": "string", "description": "Phosphor icon class" },
      "title":    { "type": "string", "description": "Notification title" },
      "subtitle": { "type": "string", "description": "Notification subtitle" },
      "linger":   { "type": "number", "description": "Auto-dismiss time in ms" }
    }
  }
}
```

**Example:**

```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "notify",
  "params": {
    "icon": "ph-check-circle",
    "title": "TASK COMPLETE",
    "subtitle": "All items processed"
  }
}
```

---

### ahi_flow

**Execute a multi-step wizard collecting user input at each stage.**

This is the most powerful tool call — it chains dialogs, renders, toasts, and waits into a sequential flow. The entire interaction sequence is defined in a single call.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `steps` | `FlowStep[]` | Yes | Array of flow step definitions |

**Returns:** `Array` of collected values (one per step)

**Tool definition:**

```json
{
  "name": "ahi_flow",
  "description": "Run a multi-step interaction wizard. Each step can be a dialog (collects user choice), render (shows a new UI), toast (notification), or wait (timed pause). Steps execute sequentially. Templates like {{0}} and {{prev}} interpolate previous step results. Special values: '$back' navigates to the previous step, '$abort' cancels the flow. Returns an array of all collected values.",
  "parameters": {
    "type": "object",
    "properties": {
      "steps": {
        "type": "array",
        "description": "Sequence of interaction steps",
        "items": {
          "type": "object",
          "properties": {
            "type": { "type": "string", "enum": ["dialog", "render", "toast", "notify", "wait", "patch", "lock", "unlock"] },
            "title": { "type": "string" },
            "body": { "type": "string" },
            "buttons": { "type": "array" },
            "controls": { "type": "array" },
            "config": { "type": "object" },
            "message": { "type": "string" },
            "level": { "type": "string" },
            "duration": { "type": "integer" },
            "id": { "type": "string" },
            "changes": { "type": "object" }
          },
          "required": ["type"]
        }
      }
    },
    "required": ["steps"]
  }
}
```

**Example: Deployment wizard**

```json
{
  "method": "flow",
  "params": {
    "steps": [
      {
        "type": "dialog",
        "title": "Select Environment",
        "body": "Choose the deployment target.",
        "buttons": [
          { "label": "STAGING",    "value": "staging" },
          { "label": "PRODUCTION", "value": "production", "style": "danger" }
        ]
      },
      {
        "type": "dialog",
        "title": "Confirm: {{0}}",
        "body": "Deploy build #847 to {{prev}}?",
        "buttons": [
          { "label": "BACK", "value": "$back" },
          { "label": "DEPLOY", "value": "go", "style": "primary" }
        ]
      },
      {
        "type": "notify",
        "icon": "ph-cloud-arrow-up",
        "title": "DEPLOYING",
        "subtitle": "Pushing build #847 to {{0}}\u2026",
        "linger": 2000
      },
      {
        "type": "render",
        "controls": [
          { "id": "progress", "type": "bar", "cols": 4, "rows": 1,
            "label": "DEPLOYMENT", "max": 10, "value": 0, "color": "accent" },
          { "id": "log", "type": "status", "cols": 4, "rows": 2,
            "label": "DEPLOY LOG", "items": [
              { "k": "TARGET", "v": "{{0}}", "c": "accent" },
              { "k": "STATUS", "v": "INITIALIZING", "c": "amber" }
            ]}
        ]
      }
    ]
  },
  "id": 20
}
```

**Response (after all steps complete):**

```json
{ "jsonrpc": "2.0", "result": ["production", "go", "notified", "rendered"], "id": 20 }
```

---

### ahi_ping

**Health check / handshake.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| *(none)* | | | |

**Returns:** `{ pong: true, version: "1.0.0", vendor: "Notum Robotics", url: "https://n-r.hr" }`

---

### ahi_init

**Initialize the AHI framework.** Usually called automatically by `ahi_render`, but can be called explicitly to set the container or config before rendering.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `selector` | `string` | No | CSS selector for the AHI container (default: `.ahi-container` or `body`) |
| `config` | `object` | No | Initial layout configuration |

**Returns:** `{ ok: true, version: "1.0.0" }`

**JSON-RPC:**
```json
{ "method": "init", "params": { "selector": "#ahi-root", "config": { "gap": 8 } }, "id": 1 }
```

---

### ahi_destroy

**Tear down the AHI instance.** Removes all controls, listeners, observers, and toast elements. Use when the agent session ends.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| *(none)* | | | |

**Returns:** `{ ok: true }`

**JSON-RPC:**
```json
{ "method": "destroy", "id": 1 }
```

---

## Control Schema Reference

All controls share these **base fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | **Recommended** | Stable identifier. Used for `patch`, `lock`, `read`. |
| `type` | string | Yes | `'card'` \| `'slider'` \| `'toggle'` \| `'button'` \| `'stepper'` \| `'bar'` \| `'status'` \| `'gauge'` \| `'wave'` \| `'matrix'` \| `'ring'` \| `'spark'` \| `'scope'` \| `'level'` |
| `cols` | integer | Yes | Column span (1–6) |
| `rows` | integer | Yes | Row span (1–4) |

Plus any [AHI Extension Fields](#ahi-extension-fields).

### Card

A toggleable state card with icon, label, and on/off status.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `state` | `'on'` \| `'off'` | — | Initial state |
| `icon` | string | — | Phosphor icon (e.g., `'ph-lightbulb'`) |
| `name` | string | — | Display name |
| `on` | string | `'ON'` | Label when on |
| `off` | string | `'OFF'` | Label when off |
| `size` | string | `'2x2'` | Size variant: `'3x3'`, `'3x2'`, `'2x3'`, `'2x2'`, `'3x1'`, `'2x1'`, `'1x3'`, `'1x2'`, `'1x1'` |

**Events:** `toggle` — `{ value: 'on' | 'off' }`

```json
{ "id": "pump", "type": "card", "cols": 2, "rows": 2,
  "state": "off", "icon": "ph-drop", "name": "WATER PUMP",
  "on": "RUNNING", "off": "STOPPED" }
```

### Slider

Interactive segmented slider with draggable value.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Display label |
| `max` | integer | `10` | Number of segments |
| `value` | integer | `0` | Current value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

**Events:** `slider_change` — `{ value: <int>, max: <int>, percent: <int> }`

```json
{ "id": "temp", "type": "slider", "cols": 4, "rows": 2,
  "label": "TEMPERATURE", "max": 30, "value": 22, "color": "amber" }
```

### Toggle

Mutually exclusive option selector.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Group label |
| `options` | string[] | — | Available choices |
| `active` | integer | `0` | Active option index |

**Events:** `toggle_select` — `{ value: '<option_text>' }`

```json
{ "id": "mode", "type": "toggle", "cols": 2, "rows": 1,
  "label": "MODE", "options": ["AUTO", "MANUAL", "OFF"], "active": 0 }
```

### Button

Clickable action button with optional icon.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Button text |
| `style` | string | `''` | `''` \| `'primary'` \| `'warning'` \| `'danger'` |
| `icon` | string | — | Phosphor icon |
| `dialog` | object | — | Auto-open this dialog on click |

**Events:** `button_click` — `{ value: '<label>' }`

```json
{ "id": "save", "type": "button", "cols": 1, "rows": 1,
  "label": "SAVE", "style": "primary", "icon": "ph-floppy-disk" }
```

### Stepper

Numeric increment/decrement control.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Display label |
| `value` | integer | `0` | Current value |

**Events:** `stepper_change` — `{ value: <int>, direction: 1 | -1 }`

```json
{ "id": "retry", "type": "stepper", "cols": 1, "rows": 2,
  "label": "RETRY COUNT", "value": 3 }
```

### Bar

Read-only segmented progress bar with built-in animations.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Display label |
| `max` | integer | `10` | Segment count |
| `value` | integer | `0` | Filled segments |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

**Animations:** Partial bars show a traveling dim segment (`brightness(0.75)`) whose direction follows the last value change (forward on increase, reverse on decrease). Full bars blink all segments in unison. When value is patched via `ahi_patch`, animated transitions fire: decrease fades out removed segments (150ms opacity transition), increase flashes new segments brighter (`brightness(1.4)`, 120ms). The readout percentage also updates automatically.

**Events:** None (read-only). Update via `ahi_patch`.

```json
{ "id": "signal", "type": "bar", "cols": 2, "rows": 1,
  "label": "SIGNAL", "max": 16, "value": 12, "color": "accent" }
```

### Status

Key-value readout panel.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Panel label |
| `items` | array | — | `[{ k, v, c }]` — key, value, color class |

**Events:** None (read-only). Update via `ahi_patch`.

```json
{ "id": "net", "type": "status", "cols": 2, "rows": 2,
  "label": "NETWORK", "items": [
    { "k": "RSSI",    "v": "-42 dBm", "c": "accent" },
    { "k": "LATENCY", "v": "12ms",    "c": "" },
    { "k": "DROPS",   "v": "0.1%",    "c": "danger" }
  ]}
```

### Gauge

Semicircular arc gauge with needle indicator.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Display label |
| `value` | integer | `0` | Current value |
| `max` | integer | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

**Events:** None (read-only). Update via `ahi_patch`.

Renders as SVG with 11 tick marks, semicircular track arc, filled arc, rotating needle, and center dot. Readout: `[ XX% ]`.

```json
{ "id": "pressure", "type": "gauge", "cols": 2, "rows": 2,
  "label": "PRESSURE", "max": 100, "value": 72, "color": "accent" }
```

### Wave

Animated multi-layer waveform canvas. The wave pattern is **deterministically derived from the widget label** — different names produce visually distinct waveforms.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Display label; also hash seed for unique waveform shape |
| `value` | integer | `0` | Controls amplitude (higher = larger waves) |
| `max` | integer | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

**Events:** None (read-only). Update via `ahi_patch`.

Renders 3 sine wave layers with frequency, speed, and phase derived from `stringHash(label)`. Amplitude scales with `value/max`. Distortion harmonic added above 60%. Canvas uses `requestAnimationFrame`.

```json
{ "id": "vibration", "type": "wave", "cols": 2, "rows": 2,
  "label": "VIBRATION", "max": 100, "value": 35, "color": "accent" }
```

### Matrix

Deterministic symmetric dot-matrix pattern grid. Pattern is **derived from both value and label** — different names with the same value produce different patterns.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Display label; contributes to pattern hash |
| `value` | integer | `0` | Current value (more lit cells at higher values) |
| `max` | integer | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |
| `gridSize` | integer | `8` | NxN grid dimension |

**Events:** None (read-only). Update via `ahi_patch`.

Lit cells are mirrored across both axes for geometric symmetry. Seed = `matrixHash(value * 137 + max * 31 + size) XOR stringHash(label)`. Readout: `[ XX% ]`.

```json
{ "id": "grid_sync", "type": "matrix", "cols": 2, "rows": 2,
  "label": "GRID SYNC", "max": 100, "value": 60, "color": "accent", "gridSize": 8 }
```

### Ring

Concentric arc chart with up to 4 labeled rings. Each ring displays its name and value in a dedicated corner.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `items` | array | `[]` | Array of ring data objects (max 4) |
| `items[].name` | string | `''` | Ring label (displayed in corner) |
| `items[].value` | integer | `0` | Ring value |
| `items[].max` | integer | `100` | Ring max |
| `items[].color` | string | — | Color name. Fallback palette: accent, amber, danger, purple, green |

**Events:** None (read-only). Update via `ahi_patch` with `changes.items`.

Corner layout: TL = ring 0, TR = ring 1, BL = ring 2, BR = ring 3. Each corner shows name + `[ XX% ]`. Unused corners use decorative bracket glyphs. No panel-label header.

```json
{ "id": "subsys", "type": "ring", "cols": 2, "rows": 2, "items": [
    { "name": "NAV",  "value": 88, "max": 100, "color": "accent" },
    { "name": "COM",  "value": 64, "max": 100, "color": "amber" },
    { "name": "LIFE", "value": 45, "max": 100, "color": "danger" }
]}
```

### Sparkline

Rolling time-series sparkline chart.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Display label |
| `value` | integer | `0` | Current value (added to rolling history) |
| `max` | integer | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

**Events:** None (read-only). Update via `ahi_patch`.

Maintains 60-point rolling buffer (~500ms sampling). Draws grid lines, gradient-filled area, stroke line, and glowing current-point dot. Canvas uses `requestAnimationFrame`.

```json
{ "id": "cpu", "type": "spark", "cols": 2, "rows": 1,
  "label": "CPU LOAD", "max": 100, "value": 42, "color": "accent" }
```

### Scope

Oscilloscope-style waveform display with phosphor afterglow.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Display label |
| `value` | integer | `0` | Controls amplitude and frequency |
| `max` | integer | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

**Events:** None (read-only). Update via `ahi_patch`.

Compound sine trace with harmonics, 8×4 division grid, vertical scanning line, and green-tinted phosphor persistence. Canvas uses `requestAnimationFrame`.

```json
{ "id": "waveform", "type": "scope", "cols": 2, "rows": 2,
  "label": "WAVEFORM", "max": 100, "value": 55, "color": "accent" }
```

### Level Meter

Vertical segmented meter with color-coded warning/critical zones.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | — | Display label |
| `value` | integer | `0` | Fill level |
| `max` | integer | `20` | Total segments |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

**Events:** None (read-only). Update via `ahi_patch`.

Filled segments get `.filled`. Segments at ≥70% get `.warn` (amber), at ≥90% get `.crit` (red). Topmost filled segment gets `.peak-hold` highlight. Readout: `[ XX% ]`.

```json
{ "id": "pwr", "type": "level", "cols": 1, "rows": 2,
  "label": "PWR", "max": 20, "value": 14, "color": "accent" }
```

---

## AHI Extension Fields

These fields can be added to **any** control type:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | — | Stable identifier for patch/lock/read/events |
| `disabled` | boolean | `false` | Grayed out, no interaction, no events |
| `hidden` | boolean | `false` | Invisible but occupies grid space |
| `badge` | string \| number \| null | — | Small overlay indicator (top-right corner) |
| `tooltip` | string | — | Hover text |
| `confirm` | DialogDefinition | — | Guard dialog — shown before the control's event fires |
| `onEvent` | string | — | Custom event name (replaces default like `'toggle'` or `'button_click'`) |
| `progress` | number \| null | — | Inline progress bar: 0–100 (determinate), -1 (indeterminate), null (remove) |
| `status` | string \| null | — | Status pip: `'ok'` \| `'warn'` \| `'error'` \| `'busy'` \| null |

**The `confirm` field** is particularly powerful for agents:

```json
{
  "id": "delete_all",
  "type": "button",
  "cols": 2, "rows": 1,
  "label": "DELETE ALL DATA",
  "style": "danger",
  "icon": "ph-trash",
  "confirm": {
    "title": "Irreversible Action",
    "body": "This will permanently erase all stored data.",
    "buttons": [
      { "label": "CANCEL", "value": "cancel" },
      { "label": "DELETE", "value": "delete", "style": "danger" }
    ]
  },
  "onEvent": "data_deletion_confirmed"
}
```

The agent never has to manage dialog state — it just declares "this button needs confirmation" and only receives the event if the human confirms.

---

## Component Properties — Progress, Status & Active States

The AHI extension fields `progress` and `status` are powered by the **nComp** (Component Properties) system, which dynamically injects visual indicators onto any control.

### Progress Bar (`progress` field)

Setting `progress` on a control appends a **10-segment animated bar** at the element's bottom edge (3px height, absolute positioned).

| Value | Mode | Animation |
|-------|------|-----------|
| `0`–`99` | Determinate (partial) | Filled segments glow; a single dimmed segment (brightness 75%) travels across filled segments at 120ms intervals. Direction follows last value change — forward on increase, reverse on decrease |
| `100` | Determinate (full) | All 10 segments filled; all flash brighter in unison — 2 flashes (50ms on/off) then 350ms pause, looping |
| `-1` | Indeterminate (scan) | A single highlighted segment sweeps across all 10 slots at 100ms intervals. Direction inherits from last determinate update (defaults forward). Use when progress percentage is unknown |
| `null` | Remove | Bar is removed, all timers cleared |

**State machine:** The system tracks internal state (`partial` / `full` / `scan`) per element. Animations only restart on state transitions — incremental updates within the same state are absorbed without interrupting the running animation. In partial mode, the travel animation dynamically reads both the filled-segment count and direction, so patching a value from 30 to 50 smoothly extends the bar and the travel sweeps forward, while patching from 50 to 30 reverses the sweep direction — all without restarting the animation.

**Color inheritance:** The bar inherits color from the control's style class — default accent (cyan), `.warning` → amber, `.danger` → red.

**Background fill:** A subtle segmented background gradient (10% opacity) fills behind the control proportional to progress, providing spatial reinforcement beyond the bar itself.

**Agent pattern — long operation feedback:**

```json
// 1. Start: show indeterminate progress + busy status
{ "method": "patch", "params": { "id": "deploy", "changes": { "progress": -1, "status": "busy", "disabled": true } }, "id": 10 }

// 2. Once progress is known, switch to determinate
{ "method": "patch", "params": { "id": "deploy", "changes": { "progress": 35 } }, "id": 11 }

// 3. Complete: show full bar + ok status, then clear
{ "method": "patch", "params": { "id": "deploy", "changes": { "progress": 100, "status": "ok" } }, "id": 12 }

// 4. After a delay, remove indicators and re-enable
{ "method": "patch", "params": { "id": "deploy", "changes": { "progress": null, "status": null, "disabled": false } }, "id": 13 }
```

### Status Pip (`status` field)

Setting `status` on a control injects a **10×10px square indicator** (no border-radius) at the element's top-right corner.

| Value | Color | Animation | Border Effect |
|-------|-------|-----------|---------------|
| `'ok'` | Accent (cyan) | Solid | 25% accent border |
| `'warn'` | Amber | Solid | 25% amber border |
| `'error'` | Red | Hard 1s blink (`steps(1)`, pure on/off) | 25% red border |
| `'busy'` | Accent (cyan) | Hard 1s blink (`steps(1)`, pure on/off) | 20% accent border |
| `null` | — | Removed | Border restored to default |

The pip's blink uses `steps(1)` animation — pure Boolean on/off, no fade, matching the system-wide terminal blink standard.

### Combining Progress + Status

The most effective agent pattern combines both for operations:

```json
// Upload button: progress bar + status pip working together
{ "id": "upload", "type": "button", "cols": 2, "rows": 1,
  "label": "UPLOAD FIRMWARE", "style": "warning", "icon": "ph-cloud-arrow-up",
  "progress": 0, "status": "busy" }
```

As the operation runs, the agent patches `progress` incrementally. When complete, it sets `progress: 100, status: 'ok'`, then clears both after a short delay. This gives the human clear visual feedback at every stage.

---

## Layout Configuration

Passed as the `config` parameter to `ahi_render`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `cols` | integer | auto | Force column count. Auto: 2 (<600px), 4 (600–900px), 6 (>900px) |
| `rowHeight` | integer | auto | Row height in px. Auto: computed to fill viewport (clamped 40–120px) |
| `gap` | integer | `6` | Grid gap in px |
| `padding` | integer | `16` | Container padding in px |
| `order` | number[] | — | Custom render order by control index |
| `pinned` | object | — | Pin controls to grid positions: `{ "0": { "col": 0, "row": 0 } }` |

---

## Dialog Definition

Used in `ahi_dialog`, the `confirm` field, and flow steps.

```json
{
  "title": "DIALOG TITLE",
  "body": "Descriptive text explaining the decision.",
  "alarm": true,
  "buttons": [
    { "label": "CANCEL",  "value": "cancel" },
    { "label": "PROCEED", "value": "proceed", "style": "primary" }
  ]
}
```

- `alarm` (boolean, optional): Whether to play the looping beep alarm. Default `true`. Pass `false` to suppress the alarm sound while keeping all other dialog behavior.
- Button `style` options: `''` (default), `'primary'` (cyan), `'warning'` (amber), `'danger'` (red).

---

## Event Schema

Every user interaction produces a structured JSON event:

```json
{
  "event": "toggle",
  "id": "main_light",
  "index": 0,
  "control": "MAIN LIGHT",
  "value": "off",
  "timestamp": 1740500000000,
  "_ahi": "1.0.0"
}
```

### Event Types

| Event | Trigger | Value |
|-------|---------|-------|
| `toggle` | Card toggled | `'on'` \| `'off'` |
| `button_click` | Button pressed | Button label or confirm dialog value |
| `slider_change` | Slider dragged | `{ value, max, percent }` |
| `toggle_select` | Toggle option selected | Option text string |
| `stepper_change` | Stepper incremented/decremented | `{ value, direction }` |
| `dialog_response` | Dialog button clicked | Button `value` or `null` |
| `toast` | Toast displayed | `{ message, level }` |

All events include: `event`, `id` (if set), `index`, `control` (name/label), `value`, `timestamp`.

If a control has `onEvent: 'my_custom_name'`, the `event` field becomes `'my_custom_name'` instead of the default.

---

## Flows (Multi-Step Wizards)

Flows chain multiple interaction steps into a single logical operation.

### Step Types

| Type | Description | Blocks? | Collected Value |
|------|-------------|---------|-----------------|
| `dialog` | Show dialog, wait for response | Yes | Button value |
| `render` | Replace the screen with controls | No | `'rendered'` |
| `toast` | Show notification, wait for its duration | Semi | `'toasted'` |
| `notify` | Display an in-grid notification over the top row | No | `'notified'` |
| `wait` | Pause for N milliseconds | Yes | `'waited'` |
| `patch` | Update a control in place | No | `'patched'` |
| `lock` | Disable a control | No | `'locked'` |
| `unlock` | Enable a control | No | `'unlocked'` |

### Template Interpolation

| Pattern | Replaced With |
|---------|---------------|
| `{{0}}` | Result of step 0 |
| `{{1}}` | Result of step 1 |
| `{{prev}}` | Result of the previous step |

### Special Values

| Value | Effect |
|-------|--------|
| `$back` | Navigate to previous dialog step (pops the last result) |
| `$abort` | Cancel the entire flow; resolve with partial results |
| `null` | Dialog dismissed without selection; flow terminates |

---

## Transport Modes

### Embedded (Same Process)

For agents running in the browser (extensions, local scripts):

```javascript
notumAHI.init('#my-container');
notumAHI.render(controls, config);
var unsub = notumAHI.onEvent(function (evt) {
    // Handle user interactions
    if (evt.event === 'button_click' && evt.id === 'deploy') {
        startDeployment();
    }
});
```

### WebSocket (Remote Agent)

For agents running on a server (MCP, Python, Node.js):

```javascript
// Browser-side: just connect
notumAHI.init('#ahi-root');
notumAHITransportWS.connect('ws://localhost:9100', {
    reconnect: true,
    reconnectDelay: 2000,
    onStatus: function (s) { console.log(s.status); }
});
```

```python
# Server-side (Python example)
import asyncio, websockets, json

async def agent(websocket):
    # Wait for AHI ready signal
    ready = json.loads(await websocket.recv())
    print(f"AHI connected: {ready}")

    # Render a dashboard
    await websocket.send(json.dumps({
        "jsonrpc": "2.0", "method": "render", "id": 1,
        "params": { "controls": [
            {"id": "status", "type": "card", "cols": 2, "rows": 2,
             "state": "on", "icon": "ph-cpu", "name": "AGENT",
             "on": "ONLINE", "off": "OFFLINE"}
        ]}
    }))

    # Listen for events
    async for message in websocket:
        msg = json.loads(message)
        if msg.get("method") == "event":
            evt = msg["params"]
            print(f"User action: {evt['event']} on {evt.get('id')}")

asyncio.run(websockets.serve(agent, "localhost", 9100))
```

### PostMessage (Iframe Sandbox)

For sandboxed environments where the AHI runs in an iframe:

```javascript
// Inside the iframe (AHI page):
notumAHI.init('#ahi-root');
notumAHITransportPM.listen({ origin: 'https://agent-host.example.com' });

// In the parent window (agent host):
var ahiFrame = document.getElementById('ahi-iframe').contentWindow;
ahiFrame.postMessage(JSON.stringify({
    jsonrpc: '2.0', method: 'render', id: 1,
    params: { controls: [...] }
}), '*');

window.addEventListener('message', function (e) {
    var msg = JSON.parse(e.data);
    if (msg.method === 'event') {
        console.log('User event:', msg.params);
    }
});
```

### JSON-RPC Protocol

All transport modes use the same JSON-RPC 2.0 protocol:

**Request (agent → AHI):**
```json
{ "jsonrpc": "2.0", "method": "render", "params": { "controls": [...] }, "id": 1 }
```

**Response (AHI → agent):**
```json
{ "jsonrpc": "2.0", "result": { "ok": true, "count": 5 }, "id": 1 }
```

**Event notification (AHI → agent):**
```json
{ "jsonrpc": "2.0", "method": "event", "params": { "event": "toggle", "id": "light", "value": "off", "timestamp": 1740500000000 } }
```

**Error:**
```json
{ "jsonrpc": "2.0", "error": { "code": -32601, "message": "Method not found: foo" }, "id": 99 }
```

Method names accept both bare (`render`) and prefixed (`ahi_render`, `ahi.render`) forms.

---

## MCP Integration Guide

To expose Notum AHI as an MCP (Model Context Protocol) tool provider:

1. Run the Notum AHI page in a browser (or headless browser via Puppeteer/Playwright)
2. Connect via WebSocket from your MCP server
3. Register the Notum AHI tools in your MCP tool manifest:

```json
{
  "tools": [
    {
      "name": "ahi_render",
      "description": "Display a UI dashboard to the user",
      "inputSchema": { "$ref": "ahi-schema.json#/definitions/Control" }
    },
    {
      "name": "ahi_dialog",
      "description": "Show a modal dialog and wait for user response",
      "inputSchema": { "$ref": "ahi-schema.json#/definitions/DialogDefinition" }
    },
    {
      "name": "ahi_patch",
      "description": "Update a specific control's properties",
      "inputSchema": {
        "type": "object",
        "properties": {
          "id": { "type": "string" },
          "changes": { "type": "object" }
        }
      }
    },
    {
      "name": "ahi_read",
      "description": "Read current state of all UI controls",
      "inputSchema": { "type": "object", "properties": { "id": { "type": "string" } } }
    },
    {
      "name": "ahi_toast",
      "description": "Show a notification toast",
      "inputSchema": {
        "type": "object",
        "properties": {
          "message": { "type": "string" },
          "level": { "type": "string" }
        }
      }
    },
    {
      "name": "ahi_flow",
      "description": "Run a multi-step interaction wizard",
      "inputSchema": {
        "type": "object",
        "properties": {
          "steps": { "type": "array" }
        }
      }
    }
  ]
}
```

4. When the LLM emits a tool call (e.g., `ahi_render`), forward it as a JSON-RPC message over the WebSocket. The response comes back as a JSON-RPC result.

5. When user events arrive (JSON-RPC notifications with `method: "event"`), feed them back to the LLM as tool results or new context.

---

## System Prompt Snippet

Include this in your agent's system prompt to teach it about Notum AHI:

```
You have access to the Notum AHI (Automatic Human Interface) tools for building interactive UIs.
Powered by Notum AHI (Notum Robotics, n-r.hr).

AVAILABLE TOOLS:
- ahi_render(controls, config?) — Display a UI dashboard. Controls auto-fill the viewport.
- ahi_patch(id, changes) — Update a control in place (value, state, disabled, progress, etc.)
- ahi_insert(index?, control) — Add a new control to the layout.
- ahi_remove(id) — Remove a control from the layout.
- ahi_dialog(title, body, buttons) — Show a modal dialog, returns the user's choice.
- ahi_dismiss(value?) — Close the current dialog programmatically.
- ahi_toast(message, level?, duration?) — Show a brief notification. Levels: info, warn, error, ok.
- ahi_read(id?) — Read current state of controls (syncs live DOM values).
- ahi_lock(id) / ahi_unlock(id) — Disable/enable a control.
- ahi_flow(steps) — Multi-step wizard (chains dialogs, renders, notifications, toasts, waits, patches).
- ahi_ping() — Health check. Returns version and capabilities.
- ahi_init(selector?, config?) — Initialize AHI framework (auto-called by render).
- ahi_destroy() — Tear down all AHI state and listeners.

CONTROL TYPES:
- card: Toggleable state card (on/off) with icon and label. 9 size variants from 1x1 to 3x3.
- slider: Draggable segmented slider. Emits value changes with percent. cols: 2-4, rows: 2.
- toggle: Mutually exclusive option group. cols: 2, rows: 1.
- button: Clickable action. Styles: primary, warning, danger. cols: 1-2, rows: 1.
  Can auto-open a dialog via 'dialog' field.
- stepper: Numeric +/- control. Value clamped ≥ 0. cols: 1, rows: 2.
- bar: Read-only animated progress bar. Partial = traveling dim (direction-aware: forward on increase, reverse on decrease), full = blink. Value patches animate (fade-out on decrease, flash on increase). cols: 2, rows: 1.
- status: Key-value readout panel. Items have k (label), v (value), c (color class). cols: 2, rows: 2.
- gauge: Semicircular arc gauge with needle, SVG ticks, and [ XX% ] readout. Read-only. cols: 2, rows: 2.
- wave: Multi-layer animated waveform. Pattern deterministically derived from label hash. Amplitude scales with value. cols: 2, rows: 2.
- matrix: Deterministic symmetric dot-matrix. Pattern derived from name + value hash. gridSize (default 8). cols: 2, rows: 2.
- ring: Concentric arc chart (1-4 rings). Each ring has name, value, max, color. Names/values shown in widget corners. cols: 2, rows: 2.
- spark: Rolling sparkline chart. 60-point history, gradient fill, glow dot. cols: 2, rows: 1.
- scope: Oscilloscope with phosphor afterglow, grid, scanning line. Amplitude/freq from value. cols: 2, rows: 2.
- level: Vertical segmented meter with warn (>=70%) and crit (>=90%) zones, peak hold. cols: 1, rows: 2.

AHI EXTENSION FIELDS (apply to ANY control):
- id (string): Stable identifier for patch/lock/read/events. Always assign one.
- disabled (bool): Grayed out, no interaction, no events (opacity 35%).
- hidden (bool): Invisible but occupies grid space.
- badge (string|number|null): Small overlay indicator at top-right corner.
- tooltip (string): Hover text explaining the control.
- confirm (DialogDefinition): Guard dialog — shown before event fires. Only fires on confirmation.
- onEvent (string): Custom event name override (replaces default 'toggle', 'button_click', etc.).
- progress (number|null): Inline progress bar. 0-100 = determinate, -1 = indeterminate scan, null = remove.
  Three animation states: partial (traveling dim segment, direction-aware), full (blink), indeterminate (sweep scan, direction inherited).
- status (string|null): Status pip. 'ok' (cyan), 'warn' (amber), 'error' (red blink), 'busy' (cyan blink), null to remove.
  Also shifts the control's border color to match.

LAYOUT CONFIG (optional, passed to ahi_render):
- cols: Force column count (auto: 2/4/6 by width breakpoint)
- rowHeight: Force row height in px (auto: fills viewport, clamped 40-120px)
- gap: Grid gap in px (default: 6)
- padding: Container padding in px (default: 16)
- order: Array of indices defining render sequence
- pinned: Object mapping control index to {col, row} grid positions

FLOW STEPS (for ahi_flow):
- dialog: Show dialog, collect response. Supports $back and $abort.
- render: Replace screen with controls (non-blocking).
- toast: Show notification, auto-advance after duration.
- wait: Pause for N milliseconds then advance.
- patch/lock/unlock: Modify a control mid-flow.
- Templates: {{0}}, {{1}}, {{prev}} interpolate previous step results.

EVENTS YOU RECEIVE:
- toggle: { value: 'on'|'off' } — card toggled
- button_click: { value: label } — button pressed
- slider_change: { value, max, percent } — slider dragged
- toggle_select: { value: option_text } — toggle changed
- stepper_change: { value, direction: 1|-1 } — stepper clicked
- dialog_response: { value } — dialog button clicked or null if dismissed

ICONS: Use Phosphor icons (ph-<name>). Common: ph-lightbulb, ph-gear, ph-warning,
ph-check, ph-x, ph-rocket, ph-lock, ph-shield-check, ph-cpu, ph-thermometer,
ph-fan, ph-cloud-arrow-down, ph-floppy-disk, ph-trash, ph-power, ph-play, ph-pause,
ph-bell, ph-wifi-high, ph-database, ph-export, ph-broadcast, ph-fingerprint.

BEST PRACTICES:
- Give every control a stable 'id' for efficient patching and event identification.
- Use 'confirm' on dangerous buttons so users must explicitly confirm.
- Use 'progress' + 'status' together for live feedback during long operations.
- Use ahi_flow for multi-step interactions instead of multiple sequential ahi_dialog calls.
- Prefer uppercase labels (design convention).
- Combine progress: -1 (indeterminate) with status: 'busy' while waiting, then progress: 100 + status: 'ok' when done.
- Use ahi_patch for incremental updates — avoid re-rendering the entire layout when only one value changes.
- Use badge for notification counts, 'NEW' indicators, or version numbers.
- Use tooltip to explain non-obvious controls.
```

---

## Common Mistakes

### patch() silently does nothing

**Symptom:** Controls render fine, but `ahi_patch('my_id', { value: 5 })` has no visible effect. No error in the browser console.

**Cause:** You rendered controls through `nInteractive.init()` (or `nDynamic.init()`) directly, bypassing AHI's control registry. AHI's `_idMap` is empty, so `patch()` can't find the control.

**Fix:** Always use `notumAHI.render([...])` to render controls. It delegates to nInteractive/nDynamic internally and populates the registry that `patch()`, `lock()`, `read()`, etc. depend on.

### Two _controls arrays, same name, different modules

AHI maintains its own `_controls` array (with `id`, `badge`, `confirm`, etc.) and passes a stripped-down copy to nInteractive/nDynamic, which stores it in *its own* `_controls`. If you call `nInteractive.init()` directly, only nInteractive's copy exists — AHI's registry stays empty. There is no cross-link between them.

### patch() says "control not found" — but it rendered fine

The id you passed to `patch()` doesn't match any `id` field in the controls array you passed to `render()`. Check for typos, case mismatches, or missing `id` fields. The error message now prints all registered ids.

---

## Examples

### Example 1: Simple IoT Dashboard

```json
{
  "method": "render",
  "params": {
    "controls": [
      { "id": "light", "type": "card", "cols": 2, "rows": 2, "state": "on", "icon": "ph-lightbulb", "name": "LIVING ROOM", "on": "ACTIVE", "off": "INACTIVE" },
      { "id": "fan", "type": "card", "cols": 2, "rows": 2, "state": "off", "icon": "ph-fan", "name": "CEILING FAN", "on": "RUNNING", "off": "STOPPED" },
      { "id": "brightness", "type": "slider", "cols": 4, "rows": 2, "label": "BRIGHTNESS", "max": 20, "value": 14, "color": "accent" },
      { "id": "scene", "type": "toggle", "cols": 2, "rows": 1, "label": "SCENE", "options": ["MORNING", "DAY", "NIGHT"], "active": 1 },
      { "id": "zone", "type": "toggle", "cols": 2, "rows": 1, "label": "ZONE", "options": ["A", "B", "C"], "active": 0 },
      { "id": "signal", "type": "bar", "cols": 2, "rows": 1, "label": "SIGNAL", "max": 16, "value": 12, "color": "accent" },
      { "id": "temp", "type": "bar", "cols": 2, "rows": 1, "label": "TEMPERATURE", "max": 16, "value": 9, "color": "amber" },
      { "id": "radio", "type": "status", "cols": 2, "rows": 2, "label": "RADIO", "items": [
        { "k": "RSSI", "v": "-42 dBm", "c": "accent" },
        { "k": "CHANNEL", "v": "11", "c": "" },
        { "k": "TX PWR", "v": "20 dBm", "c": "amber" }
      ]},
      { "id": "save", "type": "button", "cols": 1, "rows": 1, "label": "SAVE", "style": "primary", "icon": "ph-floppy-disk" },
      { "id": "reset", "type": "button", "cols": 1, "rows": 1, "label": "RESET", "style": "danger", "icon": "ph-arrows-clockwise", "confirm": { "title": "Reset All?", "body": "This will restore factory defaults.", "buttons": [{ "label": "CANCEL", "value": "no" }, { "label": "RESET", "value": "yes", "style": "danger" }] } }
    ]
  },
  "id": 1
}
```

### Example 2: CI/CD Pipeline Monitor

```json
{
  "method": "render",
  "params": {
    "controls": [
      { "id": "pipeline", "type": "status", "cols": 4, "rows": 2, "label": "PIPELINE #847", "items": [
        { "k": "BRANCH", "v": "main", "c": "accent" },
        { "k": "COMMIT", "v": "a3f8b21", "c": "" },
        { "k": "STAGE",  "v": "BUILD", "c": "amber" },
        { "k": "ETA",    "v": "~2m 30s", "c": "" }
      ]},
      { "id": "build_progress", "type": "bar", "cols": 4, "rows": 1, "label": "BUILD PROGRESS", "max": 20, "value": 14, "color": "accent" },
      { "id": "test_progress", "type": "bar", "cols": 4, "rows": 1, "label": "TEST SUITE", "max": 20, "value": 0, "color": "amber" },
      { "id": "deploy", "type": "button", "cols": 2, "rows": 1, "label": "DEPLOY", "style": "danger", "icon": "ph-rocket", "disabled": true, "tooltip": "Available after tests pass" },
      { "id": "abort", "type": "button", "cols": 2, "rows": 1, "label": "ABORT", "style": "warning", "icon": "ph-x", "confirm": { "title": "Abort Pipeline?", "body": "Build artifacts will be discarded.", "buttons": [{ "label": "CONTINUE", "value": "no" }, { "label": "ABORT", "value": "abort", "style": "danger" }] } }
    ]
  },
  "id": 1
}
```

Then, as the build progresses, the agent patches values:

```json
{ "method": "patch", "params": { "id": "build_progress", "changes": { "value": 18 } }, "id": 2 }
{ "method": "patch", "params": { "id": "test_progress", "changes": { "value": 7 } }, "id": 3 }
{ "method": "patch", "params": { "id": "deploy", "changes": { "disabled": false, "tooltip": null } }, "id": 4 }
```

### Example 3: Multi-Step Configuration Flow

```json
{
  "method": "flow",
  "params": {
    "steps": [
      {
        "type": "dialog",
        "title": "Select Protocol",
        "body": "Choose communication protocol for the gateway.",
        "buttons": [
          { "label": "MQTT",   "value": "mqtt" },
          { "label": "ZIGBEE", "value": "zigbee", "style": "primary" },
          { "label": "THREAD", "value": "thread" }
        ]
      },
      {
        "type": "dialog",
        "title": "Configure {{prev}}",
        "body": "Set the baud rate for {{0}} connection.",
        "buttons": [
          { "label": "BACK",   "value": "$back" },
          { "label": "9600",   "value": "9600" },
          { "label": "115200", "value": "115200", "style": "primary" }
        ]
      },
      {
        "type": "toast",
        "message": "Configuring {{0}} at {{1}} baud...",
        "level": "info",
        "duration": 2000
      },
      {
        "type": "render",
        "controls": [
          { "id": "conn_status", "type": "card", "cols": 2, "rows": 2, "state": "on", "icon": "ph-wifi-high", "name": "{{0}}", "on": "CONNECTED", "off": "DISCONNECTED" },
          { "id": "conn_info", "type": "status", "cols": 2, "rows": 2, "label": "CONNECTION", "items": [
            { "k": "PROTOCOL", "v": "{{0}}", "c": "accent" },
            { "k": "BAUD",     "v": "{{1}}", "c": "" },
            { "k": "STATUS",   "v": "READY", "c": "accent" }
          ]}
        ]
      }
    ]
  },
  "id": 30
}
```

---

## Static HTML Usage (Non-AHI)

When building **static HTML pages** directly (not using the AHI JSON protocol), AHI provides additional components and features not exposed through the AHI schema. These are available to coding agents generating HTML markup.

### Vertical Segmented Sliders

Interactive vertical slider with the same semantics as horizontal sliders but oriented bottom-to-top. Use class `seg-slider-v` instead of `seg-slider`.

```html
<div class="grid-cell">
  <span class="panel-label">LEVEL</span>
  <div class="seg-slider-v" data-value="6" data-max="10" data-color="accent"></div>
  <span class="slider-readout">[ 60% ]</span>
</div>
```

**Attributes:** Same as horizontal — `data-value`, `data-max`, `data-color` (`accent` | `amber` | `danger`).

**Behavior:** `column-reverse` flexbox — bottom segment = index 0, top = max-1. Click/drag near the top → high value; near the bottom → low value. Pitch-scaled `nbeep('adjust')` sounds fire on drag.

**JS initialization:** Call `buildSegSliderV(element)` or include `notum.js` which auto-initializes all `.seg-slider-v` elements in the DOM.

### Vertical Segmented Bars

Read-only vertical bar with the same animations as horizontal bars. Use class `seg-bar-v` instead of `seg-bar`.

```html
<div class="grid-cell">
  <span class="panel-label">SIGNAL</span>
  <div class="seg-bar-v" data-value="7" data-max="10" data-color="accent"></div>
</div>
```

**Attributes:** Same as horizontal — `data-value`, `data-max`, `data-color`.

**Animations:** Identical to horizontal bars — traveling dim segment for partial fill (direction-aware: bottom-to-top on increase, top-to-bottom on decrease), blink for full. Uses `column-reverse` flexbox for bottom-to-top fill direction.

**Layout helper:** Use the `v-gauge-panel` CSS class for a centered vertical layout:

```html
<div class="v-gauge-panel">
  <span class="panel-label">TEMP</span>
  <div class="seg-bar-v" data-value="8" data-max="12" data-color="amber" style="height: 120px;"></div>
  <span class="slider-readout">[ 67% ]</span>
</div>
```

### Extended Lockout (`data-lockout`)

Any interactive element can declare `data-lockout="<ms>"` to extend the flash-lock cooldown beyond the default 200ms. This prevents re-clicks during long operations.

```html
<button class="action-btn danger" data-flash data-lockout="8000">
  <i class="ph ph-rocket"></i> DEPLOY
</button>
```

**Behavior:**
- The border strobe continues for the full lockout duration (e.g., 8 seconds).
- The element receives the `.n-locked` class for the lockout period, setting `cursor: wait`.
- Re-clicks are blocked for the entire lockout window.
- Colors are NOT muted during lockout — the element remains at full visual fidelity.
- When lockout ≥ 400ms, the strobe tick increases to 150ms (slower blink for longer operations).

**Use case:** Operations that take time (uploads, deployments, diagnostics) where re-triggering would be destructive.

### Icon Toggle Buttons

Standalone toggle buttons that switch between on/off states. Use class `icon-toggle` with `is-on` or `is-off`.

```html
<button class="icon-toggle is-off" data-flash>
  <i class="ph ph-bell"></i>
</button>
```

On click, the button toggles between `is-on` and `is-off` classes. When on, the icon uses accent-colored filled style; when off, it uses the default dimmed style.

### Card Size Variants (All 9 Sizes)

Active state cards support nine `data-size` variants. The CSS dynamically hides or rearranges elements for each size:

| Size | Grid Span | Visible Elements | Use Case |
|------|-----------|------------------|----------|
| `3x3` | 3 cols × 3 rows | Icon (64px), name, state text | Extra-large hero card |
| `3x2` | 3 cols × 2 rows | Icon (56px), name, state text | Wide landscape |
| `2x3` | 2 cols × 3 rows | Icon (56px), name, state (stacked) | Tall portrait |
| `2x2` | 2 cols × 2 rows | Icon, name, state text | Standard (default) |
| `3x1` | 3 cols × 1 row | Icon, name only | Wide strip |
| `2x1` | 2 cols × 1 row | Icon, name only | Compact horizontal |
| `1x3` | 1 col × 3 rows | Icon, name, state (stacked) | Tall pillar |
| `1x2` | 1 col × 2 rows | Icon, name (stacked) | Compact vertical |
| `1x1` | 1 col × 1 row | Icon only | Minimal / toolbar |

**In AHI JSON:** Set `"size": "3x2"` plus matching `"cols": 3, "rows": 2` on a card control.

**In static HTML:** Add `data-size="3x2"` to a `.card` element, and set `grid-column: span 3; grid-row: span 2`.

### Component Properties (nComp) in HTML

The `nComp` API can be called directly from JavaScript on any DOM element — not just AHI controls:

```javascript
// Attach a progress bar to any element
nComp.progress(document.getElementById('my-button'), 45);   // 45%
nComp.progress(document.getElementById('my-button'), -1);   // indeterminate scan
nComp.progress(document.getElementById('my-button'), null); // remove

// Attach a status pip to any element
nComp.status(document.getElementById('my-card'), 'busy');
nComp.status(document.getElementById('my-card'), null);     // remove

// Set active/inactive state
nComp.active(document.getElementById('btn-a'), true);   // accent highlight
nComp.active(document.getElementById('btn-b'), false);  // non-interactive (pointer-events: none)
nComp.active(document.getElementById('btn-b'), null);   // remove — return to default
```

**nComp.active()** is NOT available via the AHI `active` field (not in the schema). Use `disabled: true` in AHI for a similar non-interactive effect, or use nComp directly in embedded mode.

### Sub-menu Items

List-style items with labels, values, status indicators, and chevrons:

```html
<div class="submenu-item" data-flash>
  <div class="submenu-row">
    <span class="submenu-name">GATEWAY</span>
    <span class="submenu-val accent">ONLINE</span>
  </div>
  <i class="ph ph-caret-right submenu-chevron"></i>
</div>
```

Value color classes: `accent`, `amber`, `danger`, or omit for default white.

---

## Icon Reference

AHI uses [Phosphor Icons](https://phosphoricons.com/) (regular weight). Common icons for agents:

| Category | Icons |
|----------|-------|
| **Power/State** | `ph-power`, `ph-lightning`, `ph-battery-charging`, `ph-plug` |
| **Actions** | `ph-play`, `ph-pause`, `ph-stop`, `ph-arrows-clockwise`, `ph-rocket` |
| **Data** | `ph-database`, `ph-cloud`, `ph-cloud-arrow-up`, `ph-cloud-arrow-down`, `ph-download` |
| **Devices** | `ph-cpu`, `ph-desktop`, `ph-device-mobile`, `ph-printer`, `ph-speaker-high` |
| **Environment** | `ph-lightbulb`, `ph-fan`, `ph-thermometer`, `ph-drop`, `ph-sun` |
| **Security** | `ph-lock`, `ph-lock-open`, `ph-shield-check`, `ph-key`, `ph-fingerprint` |
| **Status** | `ph-check-circle`, `ph-warning`, `ph-x-circle`, `ph-info`, `ph-bell` |
| **Navigation** | `ph-gear`, `ph-sliders`, `ph-list`, `ph-terminal`, `ph-code` |
| **Files** | `ph-floppy-disk`, `ph-file`, `ph-folder`, `ph-trash`, `ph-export` |
| **Communication** | `ph-wifi-high`, `ph-bluetooth`, `ph-broadcast`, `ph-chat-text` |

Full set: [phosphoricons.com](https://phosphoricons.com/)

---

## License

MIT License

Copyright (c) 2026 [Notum Robotics](https://n-r.hr)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
