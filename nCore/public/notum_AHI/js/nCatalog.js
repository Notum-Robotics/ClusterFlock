/* ──────────────────────────────────────
   Notum AHI — Component Catalog
   Copyright © 2026 Notum Robotics. Licensed under the MIT License.

   Canonical showcase of every control type and size variant.
   Used by the homepage auto-grid and the AHI "Home" scenario.

   GLOBAL: window.GRID_CATALOG  (Array)
   ────────────────────────────────────── */

window.GRID_CATALOG = [
    // ── Active-state cards ──
    { type: 'card', cols: 2, rows: 2, state: 'on',  icon: 'ph-lightbulb',   name: 'AMBIENT',     on: 'ACTIVE',   off: 'INACTIVE' },
    { type: 'card', cols: 2, rows: 2, state: 'off', icon: 'ph-fan',         name: 'EXHAUST FAN', on: 'ENABLED',  off: 'DISABLED' },

    // ── Full-width slider ──
    { type: 'slider', cols: 4, rows: 2, label: 'ROOM BRIGHTNESS',    max: 20, value: 14, color: 'accent' },

    // ── Half sliders ──
    { type: 'slider', cols: 2, rows: 2, label: 'VOLUME',            max: 15, value: 8,  color: 'accent' },
    { type: 'slider', cols: 2, rows: 2, label: 'COLOR WARMTH',      max: 12, value: 9,  color: 'amber' },

    // ── Toggle groups ──
    { type: 'toggle', cols: 2, rows: 1, label: 'SCENE',  options: ['MORNING', 'DAY', 'NIGHT'], active: 1 },
    { type: 'toggle', cols: 2, rows: 1, label: 'ZONE',   options: ['A', 'B', 'C', 'D'],       active: 2 },

    // ── Buttons ──
    { type: 'button', cols: 1, rows: 1, label: 'REBOOT', style: 'danger',  icon: 'ph-arrows-clockwise' },
    { type: 'button', cols: 1, rows: 1, label: 'ARM',    style: 'primary', icon: 'ph-shield-check' },
    { type: 'button', cols: 1, rows: 1, label: 'LOCK',   style: '',        icon: 'ph-lock-key' },
    { type: 'button', cols: 1, rows: 1, label: 'ALERT',  style: 'warning', icon: 'ph-bell-ringing' },

    // ── Steppers ──
    { type: 'stepper', cols: 1, rows: 2, label: 'DELAY (S)', value: 30 },
    { type: 'stepper', cols: 1, rows: 2, label: 'RETRY #',   value: 3 },

    // ── Progress bars ──
    { type: 'bar', cols: 2, rows: 1, label: 'SIGNAL',  max: 16, value: 12, color: 'accent' },
    { type: 'bar', cols: 2, rows: 1, label: 'LATENCY', max: 16, value: 14, color: 'danger' },

    // ── More cards ──
    { type: 'card', cols: 2, rows: 2, state: 'on',  icon: 'ph-lock',        name: 'DOOR LOCK', on: 'LOCKED',  off: 'UNLOCKED' },
    { type: 'card', cols: 2, rows: 2, state: 'off', icon: 'ph-thermometer', name: 'HEATING',   on: 'ENABLED', off: 'DISABLED' },

    // ── Card size variants ──
    { type: 'card', cols: 2, rows: 1, size: '2x1', state: 'on',  icon: 'ph-lock',      name: 'DOOR LOCK', on: 'LOCKED', off: 'UNLOCKED' },
    { type: 'card', cols: 1, rows: 2, size: '1x2', state: 'off', icon: 'ph-lock-open', name: 'DOOR LOCK', on: 'LOCKED', off: 'UNLOCKED' },
    { type: 'card', cols: 1, rows: 1, size: '1x1', state: 'on',  icon: 'ph-lock',      name: 'DOOR LOCK', on: 'LOCKED', off: 'UNLOCKED' },
    { type: 'card', cols: 1, rows: 1, size: '1x1', state: 'off', icon: 'ph-lock-open', name: 'DOOR LOCK', on: 'LOCKED', off: 'UNLOCKED' },
    { type: 'card', cols: 2, rows: 1, size: '2x1', state: 'off', icon: 'ph-lock-open', name: 'DOOR LOCK', on: 'LOCKED', off: 'UNLOCKED' },

    // ── More buttons ──
    { type: 'button', cols: 1, rows: 1, label: 'PAIR',   style: 'primary', icon: 'ph-bluetooth' },
    { type: 'button', cols: 1, rows: 1, label: 'SCAN',   style: '',        icon: 'ph-magnifying-glass' },
    { type: 'button', cols: 1, rows: 1, label: 'LOG',    style: '',        icon: 'ph-terminal' },
    { type: 'button', cols: 1, rows: 1, label: 'OTA',    style: 'warning', icon: 'ph-cloud-arrow-down' },

    // ── Full-width slider ──
    { type: 'slider', cols: 4, rows: 2, label: 'MOTION SENSITIVITY', max: 25, value: 18, color: 'accent' },

    // ── Status groups ──
    { type: 'status', cols: 2, rows: 2, label: 'RADIO', items: [
        { k: 'RSSI',    v: '-42 dBm', c: 'accent' },
        { k: 'CHANNEL', v: '11',       c: '' },
        { k: 'TX PWR',  v: '20 dBm',  c: 'amber' }
    ]},
    { type: 'status', cols: 2, rows: 2, label: 'MESH', items: [
        { k: 'NODES',   v: '7',     c: 'accent' },
        { k: 'HOPS',    v: '2',     c: '' },
        { k: 'DROPPED', v: '0.1%',  c: 'accent' }
    ]},

    // ── More toggles ──
    { type: 'toggle', cols: 2, rows: 1, label: 'POWER',  options: ['ECO', 'STD', 'PERF'],    active: 1 },
    { type: 'toggle', cols: 2, rows: 1, label: 'SOURCE', options: ['HDMI1', 'HDMI2', 'USB'], active: 0 },

    // ── More bars ──
    { type: 'bar', cols: 2, rows: 1, label: 'CACHE', max: 20, value: 7,  color: 'accent' },
    { type: 'bar', cols: 2, rows: 1, label: 'QUEUE', max: 20, value: 3,  color: 'amber' },

    // ── Fill buttons ──
    { type: 'button', cols: 1, rows: 1, label: 'RESET',  style: 'danger',  icon: 'ph-prohibit' },
    { type: 'button', cols: 1, rows: 1, label: 'SAVE',   style: 'primary', icon: 'ph-floppy-disk' },
    { type: 'button', cols: 1, rows: 1, label: 'EXPORT', style: '',        icon: 'ph-export' },
    { type: 'button', cols: 1, rows: 1, label: 'CONFIG', style: 'warning', icon: 'ph-gear' },

    // ── Gauge widgets ──
    { type: 'gauge', cols: 2, rows: 2, label: 'THRUST',     max: 100, value: 72, color: 'accent' },
    { type: 'gauge', cols: 2, rows: 2, label: 'CORE TEMP',  max: 200, value: 145, color: 'amber' },

    // ── Wave displays ──
    { type: 'wave', cols: 2, rows: 2, label: 'VIBRATION',   max: 100, value: 35, color: 'accent' },
    { type: 'wave', cols: 2, rows: 2, label: 'FIELD FLUX',  max: 100, value: 78, color: 'danger' },

    // ── Matrix patterns ──
    { type: 'matrix', cols: 2, rows: 2, label: 'GRID SYNC',  max: 100, value: 60, color: 'accent', gridSize: 8 },
    { type: 'matrix', cols: 2, rows: 2, label: 'MESH MAP',   max: 100, value: 25, color: 'amber',  gridSize: 8 },

    // ── Ring charts ──
    { type: 'ring', cols: 2, rows: 2, label: 'SUBSYSTEMS', items: [
        { name: 'NAV',  value: 88, max: 100, color: 'accent' },
        { name: 'COM',  value: 64, max: 100, color: 'amber' },
        { name: 'LIFE', value: 45, max: 100, color: 'danger' }
    ]},

    // ── Sparklines ──
    { type: 'spark', cols: 2, rows: 1, label: 'CPU LOAD',  max: 100, value: 42, color: 'accent' },
    { type: 'spark', cols: 2, rows: 1, label: 'NET I/O',   max: 100, value: 67, color: 'amber' },

    // ── Scope displays ──
    { type: 'scope', cols: 2, rows: 2, label: 'WAVEFORM',   max: 100, value: 55, color: 'accent' },

    // ── Level meters ──
    { type: 'level', cols: 1, rows: 2, label: 'PWR',     max: 20, value: 14, color: 'accent' },
    { type: 'level', cols: 1, rows: 2, label: 'SIGNAL',  max: 20, value: 18, color: 'accent' },
];
