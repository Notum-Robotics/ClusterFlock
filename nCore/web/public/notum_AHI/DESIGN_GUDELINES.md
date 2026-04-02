# Notum AHI — Design Guidelines

Copyright © 2026 Notum Robotics. Licensed under the MIT License.

---

## Core Concept: Functional Realism

The overarching philosophy of this design system is Utilitarian Necessity. This interface is a tool, not a toy. It implies an environment where data is mission-critical, and user friction could have catastrophic results. The aesthetic is born from extreme engineering: stripping away decorative elements to leave only raw data, structural grids, and purposeful interaction. Do not use decorative "greebles," arbitrary glowing lights, or meaningless animations. If a pixel is lit, it must convey information.

---

## 1. Layout Architecture & Grid Systems

### Rigid Modularity

**The Grid:** The entire interface must be built on a strict, mathematically precise grid (e.g., 8px or 12px baseline). Screen real estate is divided into modular, non-overlapping rectangular panes.

**Containment:** Group related data into distinct, bordered bounding boxes. Avoid floating elements. Every chart, list, or control set should live within a defined structural container.

**Borders & Dividers:** Use hairline borders (1px solid). Do not use drop shadows to separate elements; use negative space or thin geometric dividing lines.

**Whitespace Layout:** "Dense but breathable." Pack a high volume of data into the modules, but leave strict, uniform padding between the modules themselves to prevent cognitive overload.

---

## 2. Shape Language & Geometry

### Sharp, Synthetic, and Framed

**Corners:** Eliminate organic curves. Border radii should be 0px (perfectly sharp) or a maximum of 2px to slightly soften high-contrast bounding boxes.

**Framing Data:** Use typographic and geometric framing to isolate critical readouts. Enclose changing values within brackets `[ ]`, angle brackets `< >`, or vertical pipes `|`.

Example: `SYSTEM_LOAD: [ 84.2% ]`

**Corner Anchors (All Four Corners):** Every major container (cards, dialogs, panels) must display right-angle bracket glyphs (⌜ ⌝ ⌞ ⌟) at ALL four corners. Position them precisely using flexbox-stretched pseudo-elements (`::before` for top-left + top-right, `::after` for bottom-left + bottom-right) with `justify-content: space-between`. Use symmetric inset (e.g., 5px from edges on cards, 6px on dialogs). Opacity: 10–20% white. Font: monospace data font at ~0.65rem.

**Line Weights:** Rely entirely on 1px or 2px stroke weights. Do not use filled, bulky shapes unless they represent a critical alert state or a solid progress bar.

---

## 3. Color Palette & Lighting

### High-Contrast, Low-Fatigue

This system relies on dark environments to make thin-line data highly visible without blinding the user.

**Backgrounds (The Canvas):**
- App background: Near-black (`#0A0A0A`).
- Surface/card background: Deep charcoal (`#141414`). Avoid pure black to reduce eye strain over long periods.
- High-Glare/Mobile Environments: Pure black (`#000000`) for absolute maximum contrast.

**Primary Data (The Ink):** Crisp White (`#F4F4F4`) for static text. Dimmed to 40% opacity for secondary labels.

**Accent & Action (The Highlight):** Electric Cyan (`#00E5FF`) as the single accent color for active/on states, selected elements, and real-time data. When a control is ON, its icon container should be solid-filled with the accent color and the icon rendered in dark (`#0A0A0A`) for maximum contrast.

**Alerts & Status:**
- Amber (`#FFB300`) for warnings.
- Stark Red (`#FF3333`) for off states, danger actions, and critical alerts.

**Borders:** Default border color is 10% white (`rgba(244,244,244, 0.10)`). Active/on state borders: 30% accent. Off/danger borders: 20–25% red.

**Lighting/Glow:** Keep it extremely subtle. Apply a 1px or 2px text-shadow behind active state text (e.g., 35% accent blur on "on" text, 25% red blur on "off" text) to simulate slight light-bleed of hardware monitors. Avoid a heavy "neon" aesthetic.

---

## 4. Typography Rules

### Hierarchy through Weight, not Size

**Font Stack (Self-Hosted, Zero CDNs):**
- Labels/Headers: Rajdhani (geometric sans-serif, Bourgeois-similar). Weights: 400, 500, 600, 700. All labels UPPERCASE.
- Dynamic Data/Numbers: IBM Plex Mono. Weights: 400, 500. Ensures rapidly changing numbers do not cause layout jitter.
- All fonts must be self-hosted as `.woff2` files. No external CDN references anywhere.

**Sizing:** Keep font sizes relatively uniform. Establish hierarchy by making headers bold and low-opacity (e.g., 45–50% white), while making the actual data readouts regular weight but high-opacity (100% white).

**State Labels:** Dynamic state values are bracket-framed in monospace: `[ ACTIVE ]`, `[ DISABLED ]`. Always uppercase.

---

## 5. Data Visualization Guidelines

**Line Graphs:** Raw and jagged. Do not use bezier curves or smoothed line interpolation. Lines should travel from data point to data point with sharp angles. Fill the area under the line with a 10% opacity of the stroke color.

**Bar Charts / Progress Bars:** Do not use solid filled blocks. Use "segmented" bars — arrays of small horizontal or vertical rectangles with 1px gaps between them that light up sequentially.
- **Partially filled (loading):** A single subtly darker segment (`filter: brightness(0.75)`) snaps from position to position across the filled segments at 120ms intervals. No gradients, no smooth transitions — pure `steps(1)` snap. JS-driven via `.seg-dim` class toggle.
- **Fully filled:** All filled segments flash brighter in unison: 2 flashes (50ms on, 50ms off) then 350ms pause, looping continuously. JS-driven via `.seg-bright` class toggle (`filter: brightness(1.35)` in on state).
- **Value transitions (bars only):** When a bar's value is patched via `updateSegBar()`, animated transitions reinforce the change. **Decrease:** removed segments fade out (opacity → 0 over 150ms CSS transition) before snapping their filled class off. **Increase:** new segments appear immediately with a brief bright flash (`filter: brightness(1.4)` via `.seg-flash` class for 120ms). After either transition, the travel/blink animation restarts automatically.

**Circular Gauges:** Avoid traditional analog dials. Use broken/segmented rings with small gaps (e.g., every 45 degrees). Place the precise numeric readout in the exact center.

**3D Elements:** Rendered as glowing wireframes, point-clouds, or topographic contour lines. No shaded polygons or realistic textures.

---

## 6. Motion Design, Animation & UX

### Instantaneous & Mechanical

The animation style is the antithesis of modern, bouncy consumer apps. It should feel like it is running on a high-performance, bare-metal operating system.

**Transitions:** No easing, no "swooshing," no motion blur. UI panes should snap open instantaneously.

**Data Decryption / Scramble Effect:** When loading new cards or text, animate by rapidly cycling through randomized alphanumeric characters (charset: `A-Z, 0-9, @#$%&`) at 25ms intervals for 180ms total, progressively locking characters from left to right before resolving to the final text.

**Blinking:** Connection indicator and critical alerts blink with harsh terminal-style Boolean rhythm using `steps(1)` — pure on/off, no fade. Period: 1 second.

---

## 7. Interaction Feedback Standard

**Press Feedback (flashOutline):** ALL interactive elements (cards, buttons, dialog buttons) must use the same unified feedback system:
- On click/tap: rapidly strobe a white border (toggle `.flash-outline` class) every 10ms for 200ms total.
- During the 200ms feedback window, re-clicks are blocked (WeakMap-based lock per element).
- The flash lock prevents duplicate animations but MUST NOT block audio — the delegated `nbeep()` listener fires independently of the flash state.
- No CSS `:active` overrides — all press feedback is driven by JavaScript for consistency.
- The `flashOutline(el, onDone)` function is exposed globally as `window.flashOutline` for reuse.

**Extended Lockout (`data-lockout`):** Any interactive element may declare a `data-lockout="<ms>"` attribute to extend its flash-lock duration beyond the default 200ms. When present:
- The border strobe continues for the full lockout duration (e.g., `data-lockout="8000"` strobes for 8 seconds).
- The element receives the `.n-locked` class for the lockout period, setting `cursor: wait`.
- Re-clicks remain blocked for the entire lockout window via the same WeakMap lock.
- Colors are NOT muted during lockout — the element remains at full visual fidelity.
- Use lockout for operations that take time (uploads, deployments, diagnostics) where re-triggering would be destructive.

---

## 8. Dialog / Modal System

**Dithered Overlay:** When a dialog is summoned, the entire page is covered by a fixed overlay with a dithered pixel effect (4×4px checkerboard SVG pattern at 45% black opacity over a 55% black base). No fade transitions — instant snap via `.open` class toggling opacity between 0 and 1.

**Dialog Box:** Centered within the overlay. Dark surface background (`#141414`), 1px border at 15% white, 2px border-radius. All four corner anchors (⌜ ⌝ ⌞ ⌟). Max-width 360px.

**Corner Strobe Loop:** When a dialog opens, the four corner anchors continuously strobe in a looping pattern:
- 5 rapid flashes (50ms on, 50ms off per flash).
- 200ms pause.
- Loop repeats for the entire duration the dialog is open.
- On close, the strobe is immediately cancelled and all corners are reset.

**Modular API:** `showDialog({ title, body, buttons, alarm? })` returns a Promise resolving with the clicked button's value. Buttons support style variants: `'primary'` (accent-colored), `'warning'` (amber-colored), and `'danger'` (red-colored). The `alarm` option (default `true`) controls whether the looping beep sound plays when the dialog opens — pass `false` to suppress the alarm while keeping all other dialog behavior (overlay, corner strobe, etc.). Clicking outside the dialog dismisses it (resolves null).

**Dialog buttons** use the same flashOutline feedback as all other buttons. The dialog closes after the flash completes.

---

## 9. Asset Management

**Self-Hosting:** All assets must be self-hosted. Zero CDN dependencies. This includes:
- Font files (`.woff2`): Rajdhani (4 weights), IBM Plex Mono (2 weights), Phosphor Icons (regular).
- JavaScript: Application JS.
- CSS: Phosphor icon stylesheet (with local font path), application stylesheets.

**Cache Busting:** All static asset URLs include a version query parameter (`?v=<version>`) for cache invalidation.

---

## 10. Connection & Sync

**WebSocket Primary:** Socket.IO with WebSocket transport preferred, polling fallback.

**Long-Polling Fallback:** When WebSocket disconnects, automatic fallback to `/api/poll` endpoint with 25-second timeout and hash-based change detection.

**Connection Indicator:** 8×8px square (no border-radius) in top-right corner. Accent (`#00E5FF`) when connected, Red (`#FF3333`) with hard blink when disconnected. Title attributes: `LINK_OK` / `LINK_DOWN`.

---

## 11. Procedural Audio Subsystem (nbeep)

### Architectural Overview

The nbeep module is a zero-latency, zero-asset, dependency-free procedural audio engine for the Notum framework. It translates UI interaction identifiers (arbitrary strings up to 512 characters) into deterministic, acoustically pleasant, thematically consistent auditory feedback using the native Web Audio API.

**Pipeline:** String Input → Cyrb53 Hash → Mulberry32 Seeded PRNG → Soundscape Engine → Web Audio Graph → Output.

**Singleton AudioContext:** The module lazily instantiates a single AudioContext. Multiple instantiations are forbidden (memory leaks, concurrency limit violations). The context is resumed on first call to handle browser autoplay policies.

### Deterministic Hashing & RNG

**Hash Function:** Cyrb53 — a fast, non-cryptographic 53-bit string hash. Accepts `(string, seed)` and returns a deterministic integer. Chosen for speed and sufficient bit-width to seed the PRNG without collision issues.

**PRNG:** Mulberry32 — a seedable 32-bit PRNG that takes the Cyrb53 output and produces a predictable stream of floats `[0.0, 1.0)`. JavaScript's native `Math.random()` is NOT seedable and MUST NOT be used.

**Idempotency Requirement:** `nbeep("submit_btn")` called 10,000 times MUST yield the exact same bit-accurate waveform every time, provided the global seed has not changed. This is testable and non-negotiable.

### Musical Scale Quantization

All frequencies are quantized to predefined musical scales when `useMusicalScale` is enabled (always true for all soundscapes except historical compatibility).

**Available Scales** (configurable at runtime):
- C Major Pentatonic (default): C4 D4 E4 G4 A4 C5 D5 E5 G5 A5 C6
- C Minor Pentatonic: C4 Eb4 F4 G4 Bb4 C5 Eb5 F5 G5 Bb5 C6
- C Lydian: C4 D4 E4 F#4 G4 A4 B4 ... C6
- C Dorian: C4 D4 Eb4 F4 G4 A4 Bb4 ... C6
- Whole Tone: C4 D4 E4 F#4 G#4 Bb4 ... C6
- Chromatic: All semitones C4–C6

### Soundscapes

Four distinct soundscape modes are available, switchable at runtime via the SOUNDSCAPE toggle group:

#### Retro (standard)
Single-oscillator tones quantized to the selected musical scale. Each tone applies a frequency slide (`exponentialRampToValueAtTime`) to an adjacent scale degree, producing a characteristic "chirp." Waveforms: sine or triangle. Duration: 20–300ms (scaled by `maxDuration`).

#### Harmonic
Rich multi-voice chords using 3–5 layered oscillators. Features:
- 10 chord patterns (major/minor triads, 7ths, sus2/sus4, spread voicings).
- Arpeggio stagger (0–35ms per voice) for rhythmic spread.
- Per-voice vibrato via LFO modulation (4–10 Hz, 0–4 Hz deviation).
- Shaped envelopes: fast attack, brief hold, exponential decay.
- Optional sub-octave ghost (20% chance) and high shimmer (25% chance).
- All voices scale-snapped when within 5% of a scale degree.

#### nCARS
LCARS-inspired computer beep sequences. Pure sine waves at exact musical pitches in the bright C5–A6 register. Features:
- 11-note frequency palette: C5 D5 E5 G5 A5 B5 C6 D6 E6 G6 A6.
- 12 deterministic sequence patterns: single chirps, two-tone rising/falling, ascending/descending triples, double taps, four-note acknowledgements.
- Note duration: 40–100ms per note (scaled by `maxDuration`, minimum 20ms).
- Note gap: 10–40ms (also scaled by `maxDuration`).
- Subtle pitch bend ±0.3% for organic feel.
- Pure sine-only: no harmonics, matching the clean electronic character.

#### nCARS (default)
High-register variant spanning C6–C8 with extended variety. Features:
- 15-note frequency palette from C6 to C8.
- 20 sequence patterns (vs 12 in nCARS): five-note acknowledgements, stutter rises, oscillating bounces, paired doubles, octave jumps, ping-pong, chromatic descents.
- 5 dynamics profiles: standard, swell, accent-decay, flat, punch — randomly assigned per beep.
- Micro-rests: occasional 30–60ms pauses inserted mid-sequence for rhythmic variety.
- Per-note timing jitter ±15% so sequences feel less mechanical.
- Dual-layer ghost notes: 20% chance of a quiet octave-up shimmer layered on individual notes.
- Note duration: 25–80ms (scaled by `maxDuration`, minimum 20ms).

### Duration Control

**Max Duration** (`config.maxDuration`): A global scaling factor (0.0–1.0, default 0.80) that proportionally compresses or expands all computed note durations and inter-note gaps across every soundscape. The raw duration range is 20–300ms. This affects:
- Retro mode: overall tone length.
- Harmonic mode: base duration of all chord voices.
- nCARS / nCARS 2: per-note duration and gap timing.

All durations enforce an absolute floor of 20ms to prevent inaudible or clipped sounds. Controlled via a segmented slider in the UI.

### Acoustic Constraints & Envelope

**Waveforms:** Restricted to sine and triangle oscillators (Retro/Harmonic). nCARS modes use sine only. Square and sawtooth are excluded for softer UI feel.

**Micro-Envelope (anti-pop):** Every tone routes OscillatorNode → GainNode → destination.
- Attack: 10ms `linearRampToValueAtTime` from 0 → target volume.
- Sustain: Constant gain for (duration − 20ms).
- Release: 10ms `linearRampToValueAtTime` from target volume → 0.
- This eliminates zero-crossing artifacts (audible pops) at tone boundaries.

**Harmonic Mode Envelope:** Per-voice shaped envelope with fast attack, brief hold at peak, then `exponentialRampToValueAtTime` decay to 0.001, followed by linear ramp to zero.

**Volume:** Master volume is user-configurable (0–100%, default 20%) via a segmented slider control. The gain value is `masterVolume` (0.0–1.0). In harmonic and nCARS modes, per-voice volume scaling is applied on top of master volume.

### Configuration & Theming

| Property | Type | Default | Description |
|---|---|---|---|
| `globalSeed` | integer | 2026 | Seed fed to Cyrb53. Changes entire soundscape deterministically. |
| `masterVolume` | float | 0.30 | 0.0–1.0. Bound to MASTER VOLUME slider. |
| `maxDuration` | float | 0.10 | 0.0–1.0. Scales all durations. Bound to MAX DURATION slider. |
| `soundMode` | string | `'ncars2'` | `'standard'` / `'harmonic'` / `'ncars'` / `'ncars2'` |
| `useMusicalScale` | boolean | true | Enables scale quantization for Retro/Harmonic modes. |
| `scale` | string | `'pentatonic'` | Key into SCALES library. Affects Retro and Harmonic modes. |
| `durationMin` | float | 0.02 | Minimum duration in seconds (Retro mode). |
| `durationMax` | float | 0.30 | Maximum duration in seconds (Retro mode). |
| `allowedWaveforms` | array | `['sine','triangle']` | Oscillator types for Retro/Harmonic. |
| `fadeDuration` | float | 0.01 | Anti-pop envelope fade time in seconds. |

### API Reference

**Execution Function:** `nbeep(text, loop)` — globally exposed as `window.nbeep`.
- `text` (string): Identifier up to 512 characters. Same text + same seed = identical waveform.
- `loop` (boolean | string, default false):
  - `true`: Repeats the tone from `text` indefinitely until the next `nbeep()` call cancels it. Loop gap varies by mode (80–150ms).
  - string: Enables **dual-text alternating loop** — the engine hashes `text` and `loop` separately, producing two distinct tones (A and B), then plays them in an infinite A → B → A → B cycle. The interval is derived from the longer of the two tones. Useful for dialog audio where title and body should produce distinguishable, rhythmically paired sounds.

**Mono-Voice Rule:** Only one sound may play at any time. Calling `nbeep()` ALWAYS kills the previous sound immediately — no stacking, no overlap, no polyphony. This applies to both single and looping tones. A new `nbeep()` call is the only way to cancel a looping tone.

**Dialog Audio Pattern:** When a dialog opens, `nbeep(titleText, bodyText)` fires a dual-text alternating loop — the title and body are hashed independently, producing two distinct tones that cycle A → B → A → B for the duration the dialog is open. This gives each dialog a unique two-tone audio signature rather than a single monotone. When any dialog button is pressed, its own `nbeep('dialog_btn_' + value)` fires, which inherently cancels the loop. Clicking outside the dialog to dismiss also fires `nbeep('dialog_dismiss')`, cancelling the loop.

**Module Object:** `nDesignAudio` — globally exposed as `window.nDesignAudio`. Exposes:
- `nDesignAudio.nbeep(text, loop)` — the execution function.
- `nDesignAudio.killActive()` — immediately stop all sound and cancel loops.
- `nDesignAudio.config` — mutable configuration object (see table above).
- `nDesignAudio.SCALES` — read-only scale library.
- `nDesignAudio.cyrb53(str, seed)` — exposed hash function for external use (e.g., test panel hash display).

**Integration Pattern — Semantic Action Sounds:** Every interactive element in the Notum framework fires `nbeep()` on click via a single delegated event listener on `document`. This ensures 100% audio coverage with zero per-component wiring. The delegated listener does NOT check for flash locks — audio fires on every qualifying click regardless of animation state.

Beep strings are derived from **semantic action type**, not element names or content. The same action produces the same deterministic tone everywhere in the application:

| Action | Beep String | Triggered By |
|---|---|---|
| Toggle / menu selection | `"select"` | `.tg-option`, `.submenu-item` |
| Card → ON state | `"on"` | `.demo-card` entering on-state |
| Card → OFF state | `"off"` | `.demo-card` entering off-state |
| Stepper increment | `"increment"` | `.stepper-btn` (dir = 1) |
| Stepper decrement | `"decrement"` | `.stepper-btn` (dir = -1) |
| Standard button press | `"action"` | `[data-flash]`, `.primary` buttons |
| Danger button press | `"danger"` | `.danger` variant buttons |
| Warning button press | `"warn"` | `.warning` variant buttons |
| Dialog confirm | `"confirm"` | Dialog buttons with value yes/confirm or `.primary` style |
| Dialog cancel / dismiss | `"dismiss"` | All other dialog buttons |
| Slider adjustment | `"adjust"` | `.seg-slider` |

Explicit `data-nbeep="custom_string"` attributes on any element override the semantic mapping and pass the custom string directly to `nbeep()`.

**Browser Autoplay:** The AudioContext is created lazily and resumed on first `nbeep()` call. No user action is required beyond the first click/tap on any element.

**Node Cleanup:** Every OscillatorNode and GainNode is disconnected in the `osc.onended` callback to ensure garbage collection. No audio nodes persist beyond their scheduled duration.

---

## 12. Vertical Component Variants

### Vertical Segmented Bars (`seg-bar-v`)

Read-only vertical gauge displayed bottom-to-top using CSS `flex-direction: column-reverse`. Same animation behaviors as horizontal bars (travel highlight at 120ms for partial fill; double-blink for full fill). Typical width: 14px. Container should have a fixed or flex height (recommended ≥ 150px).

**Design rules:**
- Segments stack vertically with 1px gaps, bottom = index 0 (low), top = max (high).
- Color follows `data-color` attribute: `accent`, `amber`, `danger`.
- Place inside a `.panel.v-gauge-panel` with all four corner anchors and a `.panel-label` header.
- Include a `.slider-readout` below with bracketed percentage: `[ 70% ]`.

### Vertical Segmented Sliders (`seg-slider-v`)

Interactive vertical slider variant. Same drag behavior as horizontal sliders but Y-axis mapped: top = high value, bottom = low value. Typical width: 28px. Pitch-scaling audio is identical: `2^((pct - 0.5) * 2)`.

**Design rules:**
- Same visual treatment as vertical bars but wider (28px vs 14px) to indicate interactivity.
- Cursor: pointer during drag.
- Wrap in the same `.panel.v-gauge-panel` container structure as vertical bars.

### Icon Toggle Buttons

Compact icon-only toggles used for dense toolbar/control layouts. Classes: `.icon-btn.icon-toggle` with initial state `.is-on` or `.is-off`.

**Design rules:**
- Same border-radius (2px) and flash feedback as standard buttons.
- `is-on`: icon at full accent color, subtle accent border glow.
- `is-off`: icon dimmed to ~40% opacity.
- Use `data-flash` for press strobe. Click toggles between `is-on` ↔ `is-off`.
- Arrange in grid rows inside panels for toolbar-style layouts.

### Sub-menu Items

List-style navigation rows with icon, name, value text, and chevron glyph. Used inside `.submenu` containers within panels.

**Design rules:**
- Each `.submenu-item` is a horizontal flex row: `<icon> <info block> <chevron>`.
- The info block contains `.submenu-name` (bold label) and `.submenu-value` (bracketed status text in monospace).
- Color variants via class on the item container: `warning` (amber left border), `danger` (red left border).
- Value text color classes: `accent`, `amber`, `danger`.
- Chevron: right-aligned `›` glyph.
- `data-flash` enables standard strobe feedback.
- Audio: `nbeep('menu_' + name)` fires on click.

---

## 13. Toast Notifications

### Design Specification

Toast notifications are ephemeral, non-blocking messages that appear in a fixed stack at the bottom-right of the viewport. They are used by the AHI system (`ahi_toast` tool call) and the internal `notumAHI.toast()` API.

**Container:** `#ahi-toast-container` — fixed position, bottom-right, 16px inset, stacks upward with 8px gap. `pointer-events: none` on container, `pointer-events: auto` on individual toasts.

**Toast element:** Dark surface background (`#141414`), 1px border colored by severity level, plus a 3px left accent border. Rajdhani font, 0.85rem, 500 weight, uppercase, 0.04em letter-spacing. Max-width 360px, word-break enabled.

**Severity levels and border colors:**

| Level | Border Color | Audio |
|---|---|---|
| `info` | Accent (`#00E5FF`) | `nbeep('toast_info')` |
| `ok` | Accent (`#00E5FF`) | `nbeep('toast_ok')` |
| `warn` | Amber (`#FFB300`) | `nbeep('toast_warn')` |
| `error` | Danger (`#FF3333`) | `nbeep('toast_error')` |

**Lifecycle:**
1. Created and appended to container with `opacity: 0`.
2. Fades in via CSS transition (300ms).
3. Remains visible for configurable duration (default 3000ms).
4. Fades out (300ms transition), then removed from DOM.
5. Multiple toasts stack vertically; dismissed toasts collapse naturally.

**Design rules:**
- No border-radius beyond the system 2px maximum.
- No drop shadows — rely on the colored left border for visual weight.
- Text is always uppercase, consistent with system typography.
- Toasts should never block user interaction (pointer-events pass through the container).

---

## 14. In-Grid Notifications

In-grid notifications are non-interactive, non-dialog alerts that display contextually over the top row of a grid layout. They are distinct from toast notifications — they appear *within* the grid rather than as a floating overlay.

### Overlay Treatment

- The overlay (`.nn-overlay`) is absolute-positioned to cover all grid children whose top edge aligns with the container's first row.
- A `.nn-grayout` element fills the overlay with the same dithered SVG checkerboard pattern used by dialogs (4×4px at 50% black opacity over 60% black base).
- The grayout communicates that the underlying content is temporarily obscured, not disabled.

### Notification Box

- Background: `--surface` (`#141414`).
- Border: 1px solid at 15% white opacity.
- Border radius: `--radius` (2px).
- Centered horizontally and vertically within the overlay.
- Compact padding (`0.75rem 1rem`) — the box should not dominate the row.
- Max-width 90%, min-width 160px.

### Corner Anchors

- All four corners display anchor characters: ⌜ (top-left), ⌝ (top-right), ⌞ (bottom-left), ⌟ (bottom-right).
- Strobe animation: 4 bursts of 50ms on / 50ms off, followed by a 300ms pause, then repeat.
- Anchors reinforce the bounded, contained nature of the notification.

### Typography

- **Icon:** Phosphor icon class, `--accent` color, 1.25rem size, flex-shrink 0.
- **Title:** Rajdhani, 700 weight, 0.85rem, uppercase, letter-spacing 0.08em, 55% white opacity.
- **Subtitle:** IBM Plex Mono, 0.7rem, `--text-dim` color, letter-spacing 0.03em.

### Timing

- **Linger:** Default 4000ms before auto-dismiss.
- **Rate limit:** Maximum one notification per 30 seconds (configurable).
- **Fade:** 250ms ease transition for both fade-in and fade-out.
- **Queue:** Notifications arriving during cooldown are queued (FIFO) and drained automatically when cooldown expires.

### Dismissal

- **Tap/click:** Anywhere on the overlay instantly dismisses the notification.
- **Auto-dismiss:** After the linger duration expires.
- **Programmatic:** `nNotify.dismiss()` removes the active notification immediately.

### Audio

- A beep is triggered via nbeep (if available) when the notification appears.
- No audio on dismiss.

### Relationship to Toasts

- Toasts are global, floating, and appear outside the grid context.
- In-grid notifications are contextual, anchored to the grid's top row, and use the same grayout language as dialogs.
- When nNotify is unavailable, the AHI system falls back to toast notifications.

---

## 15. Component Properties System (nComp)

### Overview

The Component Properties System provides a uniform API to attach **progress**, **status**, and **active/inactive** states to any UI element. All properties are injected via JavaScript and driven by data attributes + dynamically created child elements. No manual HTML markup is required — call the API and the DOM updates automatically.

**Global API:** `window.nComp` exposes three methods: `.progress()`, `.status()`, `.active()`.

### Progress

`nComp.progress(element, value)` — Attach a segmented progress bar to any element.

**Parameters:**
- `value` (number): `0`–`100` for determinate progress, `-1` for indeterminate scan, `null` to remove.

**Behavior:**
- A 10-segment bar (`.n-progress`) is appended at the element's bottom edge (absolute positioned, 3px height, 1px gap between segments).
- The element receives `data-progress="<value>"` (or `"indeterminate"`).
- If the element has `position: static`, it is promoted to `position: relative` automatically.
- Segment colors inherit from the parent element's variant: accent (default), amber (`.warning`), red (`.danger`).

**Animation States (JS-driven, no CSS animations):**

| State | Trigger | Animation |
|---|---|---|
| **Partial** (0–99%) | 2+ filled segments | Travel: a single dimmed segment (`filter: brightness(0.75)`) snaps position-to-position across filled segments at 120ms intervals. Direction follows last value change — forward (L→R / bottom-to-top) on increase, reverse on decrease. |
| **Full** (100%) | All 10 segments filled | Blink: all segments flash brighter in unison — 2 flashes (50ms on, 50ms off) then 350ms pause, looping. |
| **Indeterminate** (-1) | Scan mode | Scan: a single highlighted segment sweeps across all 10 slots at 100ms intervals. Direction inherits from the last determinate update (defaults to forward). |

**State Machine:** The progress system tracks internal state (`partial` / `full` / `scan`) per element. Animations only restart on state transitions — incremental progress updates within the same state are absorbed without interrupting the running animation. The travel animation reads the filled-segment array and direction dynamically, so newly filled segments and direction changes are picked up automatically on the next tick.

**Direction-aware animation:** The system tracks the previous value and computes animation direction from the delta. When value increases, travel/scan animations move forward (left-to-right for horizontal, bottom-to-top for vertical bars). When value decreases, they reverse. Same-value updates preserve the current direction. Default direction is forward. Direction is stored per-element and persists across state transitions — an indeterminate scan following a decrease will sweep in reverse.

**Cleanup:** `nComp.progress(el, null)` removes the bar, clears all timers, and removes `data-progress`.

### Status Pip

`nComp.status(element, status)` — Attach a status indicator to any element.

**Parameters:**
- `status` (string): `'ok'` | `'warn'` | `'error'` | `'busy'` | `null` to remove.

**Behavior:**
- A 10×10px square pip (`.n-status-pip`) is injected at the element's top-right corner (`top: 4px; right: 4px`), inside the element's border.
- The pip has `border-radius: 0` (sharp square, consistent with design language) and a subtle 1px border at 6% white.
- The element receives `data-status="<status>"` and its border color shifts to match:
  - `ok` → 25% accent border, accent pip.
  - `warn` → 25% amber border, amber pip.
  - `error` → 25% red border, red pip with hard 1s blink.
  - `busy` → 20% accent border, accent pip with hard 1s blink.
- Blinking uses `steps(1)` animation (pure on/off, no fade), matching the system-wide blink standard.

**Cleanup:** `nComp.status(el, null)` removes the pip and `data-status`.

### Active / Inactive

`nComp.active(element, active)` — Set an element's active state.

**Parameters:**
- `active` (boolean): `true` for active, `false` for inactive, `null` to remove.

**Behavior:**
- Sets `data-active="true"` or `data-active="false"` on the element.
- **Active (`true`):** Text color shifts to accent, border glows at 30% accent.
- **Inactive (`false`):** `pointer-events: none; cursor: default` — the element becomes non-interactive. Colors are NOT muted; the element retains full visual fidelity. This ensures data readability is never compromised by state.
- `nComp.active(el, null)` removes the attribute entirely, returning the element to default behavior.

### CSS Architecture

All Component Properties styles live in the core design system stylesheet (`style.css`), not in demo or page-specific CSS. The system uses:
- Attribute selectors (`[data-status]`, `[data-active]`, `[data-progress]`) for state-driven styling.
- `:is()` pseudo-class for multi-selector efficiency (e.g., `:is(.action-btn, .card, .submenu-item, .panel)[data-status="ok"]`).
- No opacity dimming on any state — colors stay at full intensity to preserve data legibility in mission-critical environments.
- `prefers-reduced-motion` media query: all animation durations collapse to `0s`.