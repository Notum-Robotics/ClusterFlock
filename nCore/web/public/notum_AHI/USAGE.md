# Notum AHI — Usage Guide

> **AI Agents / Tool-Calling LLMs:** Read [`CODING_AGENT.md`](CODING_AGENT.md) instead — it contains every tool call definition, parameter schema, event format, and usage example you need to drive the UI via JSON. This file covers **static HTML markup** and **component APIs** for manual integration.

Copyright © 2026 Notum Robotics. All rights reserved.
Licensed under the MIT License. See [LICENSE](#license) below.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [File Structure](#file-structure)
3. [HTML Boilerplate](#html-boilerplate)
4. [Components](#components)
   - [Buttons](#buttons)
   - [Panels & Corner Anchors](#panels--corner-anchors)
   - [Toggle Groups](#toggle-groups)
   - [Segmented Sliders](#segmented-sliders)
   - [Vertical Segmented Sliders](#vertical-segmented-sliders)
   - [Segmented Bars](#segmented-bars)
   - [Vertical Segmented Bars](#vertical-segmented-bars)
   - [Numeric Steppers](#numeric-steppers)
   - [Active State Cards](#active-state-cards)
   - [Icon Toggle Buttons](#icon-toggle-buttons)
   - [Status Readouts](#status-readouts)
   - [Gauge](#gauge)
   - [Wave](#wave)
   - [Matrix](#matrix)
   - [Ring](#ring)
   - [Sparkline](#sparkline)
   - [Scope](#scope)
   - [Level Meter](#level-meter)
   - [Sub-menu Items](#sub-menu-items)
   - [Dialogs](#dialogs)
5. [Auto-Fit Grid](#auto-fit-grid)
6. [Card Size Variants](#card-size-variants)
7. [Component Properties (nComp)](#component-properties-ncomp)
8. [Module Registry (nRegistry)](#module-registry-nregistry)
9. [Reactive State Store (nStore)](#reactive-state-store-nstore)
10. [Dynamic Layout (nDynamic)](#dynamic-layout-ndynamic)
11. [Interactive Layout (nInteractive)](#interactive-layout-ninteractive)
12. [Procedural Audio (nbeep)](#procedural-audio-nbeep)
13. [In-Grid Notifications (nNotify)](#in-grid-notifications-nnotify)
14. [Interaction Feedback](#interaction-feedback)
15. [Icons](#icons)
16. [Color Reference](#color-reference)
17. [License](#license)

---

## Getting Started

Include the CSS and JS files in your HTML. All assets are self-hosted — no CDNs required.

**Load order matters.** `nRegistry.js` must load first (it provides the module dependency system). Other modules depend on it.

### Minimal Setup (AHI agent-driven)

```html
<!-- CSS -->
<link rel="stylesheet" href="css/phosphor.css">
<link rel="stylesheet" href="css/style.css">
<link rel="stylesheet" href="css/notum.css">
<link rel="stylesheet" href="css/subpage.css">

<!-- JS (load at end of body, in this order) -->
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
```

### Minimal Setup (Static component showcase)

```html
<script src="js/nRegistry.js"></script>
<script src="js/nbeep.js"></script>
<script src="js/nUtils.js"></script>
<script src="js/nComp.js"></script>
<script src="js/nCatalog.js"></script>
<script src="js/nNotify.js"></script>
<script src="js/nStore.js"></script>
<script src="js/notumDemo.js"></script>
```

### Minimal Setup (Dynamic grid only)

```html
<script src="js/nRegistry.js"></script>
<script src="js/nbeep.js"></script>
<script src="js/nUtils.js"></script>
<script src="js/nDynamic.js"></script>
<script src="js/nInteractive.js"></script>  <!-- optional: only if you need edit mode -->
<script src="js/nNotify.js"></script>
<script src="js/nStore.js"></script>
```

---

## File Structure

```
notum_AHI/
├── index.html              # Homepage (AHI dashboard + scenarios)
├── ahi.html                # AHI standalone page
├── components.html         # Component showcase page
├── dynamic.html            # Dynamic grid layout demo
├── interactive.html        # Interactive editor demo
├── CODING_AGENT.md         # AI agent / LLM reference (tool calls, schemas, events)
├── DESIGN_GUDELINES.md     # Full design specification
├── USAGE.md                # This file
├── README.md               # Project overview
├── LICENSE                 # MIT License
├── css/
│   ├── phosphor.css        # Phosphor icon font stylesheet
│   ├── style.css           # Base / reset styles + card size variants
│   ├── notum.css           # All Notum component styles (single source of truth)
│   ├── subpage.css         # Shared sub-page viewport layout + .nd-grid overrides
│   └── demo.css            # Deprecated (empty)
├── fonts/
│   ├── rajdhani-{400,500,600,700}.woff2
│   ├── ibm-plex-mono-{400,500}.woff2
│   └── phosphor-regular.woff2
└── js/
    ├── nRegistry.js        # Module dependency system (Notum.register/require)
    ├── nUtils.js           # Shared utilities (escHtml, flashOutline, etc.)
    ├── nComp.js            # Component properties (progress, status, active)
    ├── nCatalog.js         # Shared control catalog (GRID_CATALOG)
    ├── nbeep.js            # Procedural audio engine (standalone)
    ├── nDynamic.js         # Dynamic viewport-filling grid engine
    ├── nInteractive.js     # Interactive layout editor (wraps nDynamic)
    ├── nNotify.js          # In-grid notification system
    ├── nStore.js           # Reactive state store with auto-rendering
    ├── notum.js            # Legacy component page behaviors (not used by AHI pages)
    ├── notumDemo.js        # Demo wiring for components.html showcase
    └── ahi/
        ├── ahi.js              # Core AHI API (tool calls, rendering)
        ├── ahi-protocol.js     # JSON-RPC 2.0 message handler
        ├── ahi-transport-ws.js # WebSocket + PostMessage transport adapters
        ├── ahi-flow.js         # Multi-step wizard/flow engine
        └── ahi-schema.json     # JSON Schema for AHI control types
```

---

## HTML Boilerplate

Minimal page setup — see `components.html` or `dynamic.html` for full working examples.

The dialog overlay element (`dialog-overlay` containing `dialog-box`) must be present in the DOM for dialogs to work.

---

## Components

### Buttons

Four style variants. Always add `data-flash` for press feedback.

- `action-btn` — Default style
- `action-btn primary` — Primary action
- `action-btn warning` — Warning action
- `action-btn danger` — Destructive action

Add an icon with: `<i class="ph ph-power"></i>`

**With audio:** Add `data-nbeep="your_identifier"` to produce a specific sound.

**With dialog:** Add a `data-dialog` attribute containing a JSON dialog definition object. Clicking the button automatically opens the dialog via `showDialog()`.

---

### Panels & Corner Anchors

Every panel must include all four corner anchor glyphs (`corner-tl`, `corner-tr`, `corner-bl`, `corner-br`) using the Unicode bracket characters (⌜ ⌝ ⌞ ⌟).

---

### Toggle Groups

Mutually exclusive option selector inside a `toggle-group` container. One `tg-option` must have the `active` class. Reading the value: query `.tg-option.active` or listen for clicks on `opt.dataset.val`.

---

### Segmented Sliders

Interactive slider built from discrete segments. Requires `data-value`, `data-max`, and `data-color` attributes on a `seg-slider` element.

Color options: `accent` (cyan), `amber`, `danger` (red).

Reading value changes: Use a `MutationObserver` on the `data-value` attribute.

**Pitch-varying audio:** Sliders produce pitch-scaled `nbeep('adjust')` sounds — lower pitch at the left, higher at the right.

---

### Vertical Segmented Sliders

Interactive vertical slider variant. Same attributes as horizontal sliders but displayed as a bottom-to-top column. Uses `seg-slider-v` class.

```html
<div class="panel v-gauge-panel" style="min-width:70px; height:200px">
    <span class="corner corner-tl">⌜</span>
    <span class="corner corner-tr">⌝</span>
    <span class="corner corner-bl">⌞</span>
    <span class="corner corner-br">⌟</span>
    <div class="panel-label">VOLUME</div>
    <div class="seg-slider-v" id="vslider-volume"
         data-value="6" data-max="10" data-color="accent"
         style="flex:1; width:28px"></div>
    <span class="slider-readout">[ 60% ]</span>
</div>
```

Key differences: CSS `column-reverse` layout, clicking near top → high value, recommended container 28px width.

---

### Segmented Bars

Static (non-interactive) display version of the slider with built-in animations:

- **Partial fill:** Traveling dim highlight sweeps across filled segments every 120ms. Direction reflects last value change.
- **Full bar** (value >= max): Double-blink pattern (50ms on/off × 2 flashes, 350ms pause, loop).
- **Value transitions:** Decrease → removed segments fade out (150ms). Increase → new segments flash brighter (120ms).

---

### Vertical Segmented Bars

Read-only vertical bar variant. Uses `seg-bar-v` class. Same animations as horizontal, displayed bottom-to-top.

```html
<div class="panel v-gauge-panel" style="min-width:70px; height:200px">
    <span class="corner corner-tl">⌜</span>
    <span class="corner corner-tr">⌝</span>
    <span class="corner corner-bl">⌞</span>
    <span class="corner corner-br">⌟</span>
    <div class="panel-label">SIGNAL</div>
    <div class="seg-bar-v" id="vbar-signal"
         data-value="14" data-max="20" data-color="accent"
         style="flex:1; width:14px"></div>
    <span class="slider-readout">[ 70% ]</span>
</div>
```

---

### Numeric Steppers

Increment/decrement control with `stepper-btn` elements carrying `data-dir="-1"` or `data-dir="+1"`. Value is clamped to a minimum of 0. Auto-switches to vertical layout when container < 120px.

---

### Active State Cards

Toggle-able state cards with icon, name, and status indicator. Use class `is-on` or `is-off`. The `data-on` / `data-off` attributes define the label text for each state.

---

### Status Readouts

Key-value data display using `status-row` with `status-label` and `status-val`. Color classes: `accent` (cyan), `amber`, `danger`.

---

### Gauge

Semicircular arc gauge with needle indicator and SVG ticks.

| Property | Type | Default | Description |
|---|---|---|---|
| `label` | string | `''` | Header label |
| `value` | number | `0` | Current value |
| `max` | number | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

The gauge renders as an SVG with 11 tick marks, a semicircular track arc, a filled arc proportional to `value/max`, and a rotating needle. The readout displays `[ XX% ]`.

---

### Wave

Animated multi-layer waveform canvas. The wave pattern is **deterministically derived from the widget label** — different names produce visually distinct waveforms.

| Property | Type | Default | Description |
|---|---|---|---|
| `label` | string | `''` | Header label; also used as hash seed for unique waveform shape |
| `value` | number | `0` | Controls amplitude (higher = larger waves) |
| `max` | number | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

Renders 3 layered sine waves with unique frequency, speed, and phase per label. Amplitude and alpha scale with `value/max`. Distortion harmonic added above 60% value. Uses `requestAnimationFrame`; auto-stops when disconnected.

---

### Matrix

Deterministic symmetric dot-matrix pattern grid. The pattern is **derived from both value and label** — different names with the same value produce different patterns.

| Property | Type | Default | Description |
|---|---|---|---|
| `label` | string | `''` | Header label; contributes to pattern hash |
| `value` | number | `0` | Current value (more lit cells at higher values) |
| `max` | number | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |
| `gridSize` | number | `8` | NxN grid dimension |

Lit cells are mirrored across both axes for symmetry. The readout displays `[ XX% ]`.

---

### Ring

Concentric arc chart with up to 4 rings. Each ring displays its name and value percentage in a dedicated corner of the widget.

| Property | Type | Default | Description |
|---|---|---|---|
| `items` | array | `[]` | Array of ring data objects (max 4) |
| `items[].name` | string | `''` | Ring label (displayed in corner) |
| `items[].value` | number | `0` | Ring value |
| `items[].max` | number | `100` | Ring max |
| `items[].color` | string | — | Color name (`'accent'`, `'amber'`, `'danger'`). Fallback palette cycles through accent, amber, danger, purple, green |

Corner positions: TL = ring 0, TR = ring 1, BL = ring 2, BR = ring 3. Each corner shows the ring's name and `[ XX% ]` value, colored to match its ring. Unused corners use decorative bracket glyphs. No panel-label header is shown.

---

### Sparkline

Rolling time-series sparkline chart rendered on canvas.

| Property | Type | Default | Description |
|---|---|---|---|
| `label` | string | `''` | Header label |
| `value` | number | `0` | Current value (added to rolling history) |
| `max` | number | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

Maintains a rolling buffer of up to 60 data points (sampled every ~500ms). Draws horizontal grid lines, gradient-filled area, stroke line, and a glowing current-point dot. Uses `requestAnimationFrame`.

---

### Scope

Oscilloscope-style waveform display with phosphor afterglow effect.

| Property | Type | Default | Description |
|---|---|---|---|
| `label` | string | `''` | Display label |
| `value` | number | `0` | Controls amplitude and frequency of the waveform |
| `max` | number | `100` | Maximum value |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

Draws a compound sine trace with harmonics, an 8×4 division grid, a vertical scanning line, and a green-tinted phosphor persistence effect. Uses `requestAnimationFrame`.

---

### Level Meter

Vertical segmented meter with color-coded warning and critical zones.

| Property | Type | Default | Description |
|---|---|---|---|
| `label` | string | `''` | Header label |
| `value` | number | `0` | Current fill level |
| `max` | number | `20` | Total number of segments |
| `color` | string | `'accent'` | `'accent'` \| `'amber'` \| `'danger'` |

Filled segments get `.filled` class. Segments at ≥70% of max get `.warn` (amber), at ≥90% get `.crit` (red). The topmost filled segment gets `.peak-hold` highlight. Readout displays `[ XX% ]`.

---

### Icon Toggle Buttons

Compact icon-only toggles that flip between on/off states on click.

```html
<div class="icon-btn is-on icon-toggle" data-flash><i class="ph ph-power"></i></div>
<div class="icon-btn is-off icon-toggle" data-flash><i class="ph ph-bluetooth"></i></div>
```

Required classes: `icon-btn`, `icon-toggle`, and initial state (`is-on` / `is-off`).

---

### Sub-menu Items

List-style navigation items with icon, name, value, and chevron. Support color variants (`warning`, `danger`) on the item and color classes (`accent`, `amber`, `danger`) on the value.

```html
<div class="submenu">
    <div class="submenu-item" data-flash>
        <i class="ph ph-wifi-high"></i>
        <div class="submenu-info">
            <span class="submenu-name">WI-FI</span>
            <span class="submenu-value">[ CONNECTED ]</span>
        </div>
        <span class="submenu-chevron">›</span>
    </div>
</div>
```

---

### Dialogs

Dialogs are invoked programmatically via `showDialog(opts)` which returns a Promise.

| Property | Type | Description |
|---|---|---|
| `title` | string | Dialog title (also used for beep sound) |
| `body` | string | Body text |
| `buttons` | array | `{ label, value, style? }` objects |
| `alarm` | boolean | Play looping beep alarm (default `true`, pass `false` to suppress) |

Button styles: omit for default, or use `'primary'`, `'warning'`, `'danger'`.

Clicking outside the dialog dismisses it (resolves `null`). Call `closeDialog(value)` to close programmatically.

---

## Auto-Fit Grid

Bin-packed auto-fit grid that renders a catalog of components into a responsive layout. Populated programmatically from `GRID_CATALOG` in `nCatalog.js`. Supported types: `card`, `slider`, `toggle`, `button`, `stepper`, `bar`, `status`, `gauge`, `wave`, `matrix`, `ring`, `spark`, `scope`, `level`.

---

## Card Size Variants

Nine `data-size` variants scaling from 1×1 to 3×3:

| Size | Grid Span | Visible Elements |
|---|---|---|
| `3x3` | 3 cols × 3 rows | Icon (64px), name, state text |
| `3x2` | 3 cols × 2 rows | Icon (56px), name, state text |
| `2x3` | 2 cols × 3 rows | Icon (56px), name, state (stacked) |
| `2x2` | 2 cols × 2 rows | Icon, name, state text (default) |
| `3x1` | 3 cols × 1 row | Icon, name only |
| `2x1` | 2 cols × 1 row | Icon, name only |
| `1x3` | 1 col × 3 rows | Icon, name, state (stacked) |
| `1x2` | 1 col × 2 rows | Icon, name (stacked) |
| `1x1` | 1 col × 1 row | Icon only |

---

## Component Properties (nComp)

`nComp` attaches progress bars, status indicators, and active/inactive states to **any** DOM element. Exposed globally as `window.nComp`.

### Progress Bar

```js
nComp.progress(el, 50);    // 50% — 5/10 segments, traveling dim highlight
nComp.progress(el, 100);   // Full — double-blink pattern
nComp.progress(el, -1);    // Indeterminate — single segment scan
nComp.progress(el, null);  // Remove
```

| Value | Animation |
|---|---|
| `0`–`99` | Traveling dim-highlight across filled segs (120ms), direction follows value change |
| `100` | Double-blink on all segs (50ms × 2, 350ms pause) |
| `-1` | Single bright segment scans (100ms/step) |
| `null` | Bar removed |

### Status Pip

```js
nComp.status(el, 'ok');    // Cyan pip (static)
nComp.status(el, 'warn');  // Amber pip (static)
nComp.status(el, 'error'); // Red pip (blinking)
nComp.status(el, 'busy');  // Cyan pip (blinking)
nComp.status(el, null);    // Remove
```

### Active / Inactive State

```js
nComp.active(el, true);    // Cyan border + text
nComp.active(el, false);   // pointer-events disabled
nComp.active(el, null);    // Remove
```

All three can be combined on the same element.

---

## Module Registry (nRegistry)

`nRegistry.js` provides a lightweight module dependency system. It must load **first** — before all other JS files. Exposed globally as `window.Notum`.

### API

| Method | Description |
|---|---|
| `Notum.register(name, deps, factory)` | Register a module. `deps` is an array of dependency names. `factory` receives resolved deps and returns the module's public API. Defers if deps aren't ready yet. |
| `Notum.require(name)` | Retrieve a registered module's API. Auto-adopts legacy `window` globals. |
| `Notum.has(name)` | Returns `true` if the module is registered or exists on `window`. |
| `Notum.list()` | Returns module names in load order. |
| `Notum.pending()` | Returns pending modules with unresolved deps. |
| `Notum.status()` | Logs a health report to console. |

### How It Works

- Modules call `Notum.register('myModule', ['dep1', 'dep2'], function(dep1, dep2) { ... })`.
- If all deps are available, the factory runs immediately and the module is live.
- If deps are missing, the registration is deferred. When a new module resolves, all pending modules are retried automatically.
- Registered modules are also set on `window[name]` for backward compatibility.
- Legacy globals (e.g., `nbeep` on `window`) are auto-adopted when another module declares them as a dependency.

---

## Reactive State Store (nStore)

`nStore.js` provides a reactive state store with path-based get/set, subscriber notifications, batching, and auto-rendering to nDynamic or nInteractive grids. Exposed globally as `window.nStore`.

### Creating a Store

```js
var store = nStore.create({
  controls: [ /* control definitions */ ],
  config: { cols: 4, gap: 6 }
});
```

### API (Store Instance)

| Method | Description |
|---|---|
| `store.get(path?)` | Get value by dot/bracket path, or full state if omitted |
| `store.getAll()` | Deep clone of entire state |
| `store.set(path, value)` | Set nested value; triggers re-render + subscriber notification |
| `store.batch(fn)` | Batch multiple `set()` calls into a single re-render |
| `store.merge(partial)` | Shallow-merge object at top level |
| `store.reset(newState)` | Replace entire state; notifies with path `'*'` |
| `store.subscribe(fn)` | Subscribe to changes. Callback: `fn(path, value, oldValue)`. Returns unsubscribe function |
| `store.bind(selector, engine)` | Bind to a DOM container. Calls nInteractive or nDynamic and auto-renders on change |
| `store.destroy()` | Tear down binding, cancel renders, clear subscribers |
| `store.patchControl(index, partial)` | Patch a single control by array index |
| `store.patchControlByName(name, partial)` | Find control by name/label/id and patch it |

### Example

```js
var store = nStore.create({
  controls: [
    { type: 'card', cols: 2, rows: 2, state: 'on', icon: 'ph-lightbulb', name: 'LIGHT' },
    { type: 'slider', cols: 4, rows: 2, label: 'LEVEL', max: 10, value: 5 }
  ],
  config: { gap: 6 }
});

store.bind('#my-grid', 'nInteractive');

store.subscribe(function(path, val) {
  console.log(path, '→', val);
});

store.patchControlByName('LIGHT', { state: 'off' });
```

---

## Dynamic Layout (nDynamic)

Viewport-filling grid layout engine. Controls are defined declaratively — the engine handles placement, sizing, and responsive adaptation.

### Quick Start

```js
nDynamic.init('#my-grid', controlsArray, configObject);
```

### Control Schema

| Property | Type | Required | Description |
|---|---|---|---|
| `type` | string | Yes | `'card'`, `'slider'`, `'toggle'`, `'button'`, `'stepper'`, `'bar'`, `'status'`, `'gauge'`, `'wave'`, `'matrix'`, `'ring'`, `'spark'`, `'scope'`, `'level'` |
| `cols` | number | Yes | Column span (1–6) |
| `rows` | number | Yes | Row span (1–4) |
| `size` | string | — | Card size: `'1x1'`–`'3x3'` |
| `state` | string | — | Card state: `'on'` / `'off'` |
| `icon` | string | — | Phosphor icon class |
| `name` | string | — | Card display name |
| `on`/`off` | string | — | Card state labels |
| `label` | string | — | Label for sliders, toggles, steppers, bars, buttons |
| `max` | number | — | Max segments for sliders/bars (default: 10) |
| `value` | number | — | Current value |
| `color` | string | — | `'accent'`, `'amber'`, `'danger'` |
| `style` | string | — | Button style: `''`, `'primary'`, `'warning'`, `'danger'` |
| `options` | array | — | Toggle group options (string array) |
| `active` | number | — | Toggle group active index |
| `items` | array | — | Status rows: `[{ k, v, c }]`. Ring items: `[{ name, value, max, color }]` |
| `dialog` | object | — | Button dialog definition |
| `gridSize` | number | — | Matrix grid dimension (default 8) |

### Configuration

| Config | Default | Description |
|---|---|---|
| `cols` | auto | Force column count. Auto: 2/4/6 by width breakpoint |
| `rowHeight` | auto | Row height px. Auto fills viewport (clamped 40–120px) |
| `gap` | `6` | Grid gap px |
| `padding` | `16` | Container padding px |
| `order` | — | Render order by control index |
| `pinned` | — | Pin controls: `{ index: { col, row } }` |

### API

| Method | Description |
|---|---|
| `nDynamic.init(sel, controls, config)` | Initialize |
| `nDynamic.rebuild()` | Force rebuild |
| `nDynamic.update(controls, config)` | Update and rebuild (pass `null` to keep current) |
| `nDynamic.destroy()` | Tear down |
| `nDynamic.updateSegBar(el, value)` | Animate a bar to a new value |
| `nDynamic.showDialog(opts)` | Open a dialog (returns Promise) |
| `nDynamic.closeDialog(value)` | Close dialog |

---

## Interactive Layout (nInteractive)

> **Internal module.** For AHI agent dashboards, use `notumAHI.render()` instead. Calling `nInteractive.init()` directly bypasses AHI's control registry.

Wraps nDynamic with a hold-to-edit interaction layer: drag-and-drop reordering, context menus, per-control lock/mute/resize.

### Interaction Model

| Mode | Action | Effect |
|---|---|---|
| Normal | Tap/click | Control's native action |
| Normal | Hold 5s | Enter edit mode |
| Edit | Drag | Ghost + drop indicator → reorder |
| Edit | Hold 2s | Context menu |
| Edit | Tap empty / Escape | Exit edit mode |

### Context Menu Options

Lock/Unlock position, Mute/Unmute sounds, Resize (9 sizes: 1×1 through 3×3), Close.

### Configuration

All nDynamic config options plus:

| Key | Default | Description |
|---|---|---|
| `holdEnterMs` | `5000` | Hold to enter edit mode |
| `holdContextMs` | `2000` | Hold for context menu |
| `onEditChange` | — | Callback `fn(isEditing)` |

### API

| Method | Description |
|---|---|
| `nInteractive.init(sel, controls, config?)` | Initialize (calls nDynamic internally) |
| `nInteractive.destroy()` | Tear down all |
| `nInteractive.isEditing()` | Current edit state |
| `nInteractive.enterEdit()` / `exitEdit()` | Programmatic mode change |
| `nInteractive.rebuild()` | Sanitize pins and rebuild |
| `nInteractive.update(controls?, config?)` | Merge and rebuild |

---

## Procedural Audio (nbeep)

Standalone procedural audio engine. Every sound is deterministically generated from a string seed.

### Basic Usage

```js
nbeep('my_button_click');           // Single beep
nbeep('adjust', false, 1.5);       // Pitch multiplied
nbeep('alarm_active', true);       // Looping
nDesignAudio.killActive();          // Stop all
```

### Soundscapes

| Mode | Key | Description |
|---|---|---|
| Retro | `'standard'` | Single-oscillator chirps |
| Harmonic | `'harmonic'` | Multi-voice chords |
| nCARS | `'ncars'` | LCARS-style computer beeps |
| nCARS 2 | `'ncars2'` | High-register LCARS variant **(default)** |

### Configuration

| Property | Default | Description |
|---|---|---|
| `masterVolume` | `0.30` | Volume (0–1) |
| `maxDuration` | `0.10` | Duration scaling (0–1) |
| `soundMode` | `'ncars2'` | Soundscape |
| `scale` | `'pentatonic'` | Musical scale |
| `globalSeed` | `2026` | Deterministic seed |

### Automatic Audio Integration

A delegated click listener auto-derives beep strings from element context:

| Element | Beep String |
|---|---|
| `[data-nbeep="X"]` | `X` (explicit) |
| `.tg-option` | `toggle_` + value |
| `.stepper-btn` | `stepper_dec` / `stepper_inc` |
| `.demo-card` | `card_` + name |
| `.dialog-btn` | `dialog_` + value |
| `.submenu-item` | `menu_` + name |
| Any `[data-flash]` | Trimmed text content |

---

## In-Grid Notifications (nNotify)

Non-dialog notifications displayed over the top row of any CSS Grid container. Rate-limited, queued, and auto-dismissed.

```js
nNotify.init('#my-grid');
nNotify.show({ icon: 'ph-bell', title: 'SYSTEM READY', subtitle: 'All modules loaded', linger: 4000 });
nNotify.dismiss();
nNotify.destroy();
```

| Method | Description |
|---|---|
| `nNotify.init(sel)` | Bind to a grid container |
| `nNotify.show(opts)` | Queue and display (icon, title, subtitle, linger) |
| `nNotify.dismiss()` | Programmatic dismiss |
| `nNotify.destroy()` | Tear down |
| `nNotify.config(opts)` | Override: `rateLimit` (30000), `linger` (4000), `fade` (250) |
| `nNotify.isActive()` | Returns `true` if notification visible |
| `nNotify.queueLength()` | Queued count |

---

## Interaction Feedback

### Flash Feedback (flashOutline)

All interactive elements should include `data-flash` for unified press feedback.

```js
var accepted = flashOutline(myElement);
flashOutline(myElement, function() { console.log('done'); });
```

- `.flash-outline` toggles every 10ms for 200ms
- Re-clicks blocked during strobe (WeakMap lock)
- Audio fires independently

### Extended Lockout

```html
<div class="action-btn primary" data-flash data-lockout="2000">DEPLOY</div>
```

- `data-lockout="<ms>"` — lockout duration
- Element gets `.n-locked` class during lockout (cursor: wait)
- When lockout ≥ 400ms, strobe tick slows to 150ms

---

## Icons

[Phosphor Icons](https://phosphoricons.com/) (regular weight), self-hosted via `css/phosphor.css`.

```html
<i class="ph ph-lightbulb"></i>
<i class="ph ph-warning"></i>
```

---

## Color Reference

| Token | Value | Usage |
|---|---|---|
| `--bg` | `#0A0A0A` | App background |
| `--surface` | `#141414` | Cards, panels, dialogs |
| `--text` | `#F4F4F4` | Primary text |
| `--text-dim` | `#F4F4F4` @ 40% | Secondary labels |
| `--accent` | `#00E5FF` | Active states, selections |
| `--amber` | `#FFB300` | Warnings |
| `--danger` | `#FF3333` | Errors, off states |
| `--radius` | `2px` | Border radius |

---

## License

MIT License

Copyright (c) 2026 Notum Robotics

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