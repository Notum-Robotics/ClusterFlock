/* ──────────────────────────────────────────────────────────────
   nDynamic — Intelligent Viewport-Filling Grid Layout Engine
   Copyright © 2026 Notum Robotics. Licensed under the MIT License.
   
   Automatically arranges Notum AHI controls into an
   optimal grid that fills the viewport without scrolling.
   
   USAGE:
     nDynamic.init('#my-container', controlArray, optionalConfig);
   
   CONTROL SCHEMA:
     Each control object in the array follows the same schema
     as GRID_CATALOG entries in notum.js:
       { type, cols, rows, size?, label?, icon?, state?, ... }
   
   CONFIG (all optional):
     {
       cols:      Number   — force column count (default: auto)
       rowHeight: Number   — base row height in px (default: auto-fit)
       gap:       Number   — grid gap in px (default: 6)
       padding:   Number   — container padding in px (default: 16)
       order:     [Number] — array of indices defining render order
       pinned:    { index: {col, row} } — pin specific items to positions
     }
   
   DEPENDENCY: nUtils.js must be loaded first.

   The engine:
   1. Measures viewport minus header/chrome
   2. Calculates optimal column count and row height
   3. Bin-packs controls using dense grid-auto-flow
   4. Resizes on viewport change via ResizeObserver
   5. Exposes nDynamic.rebuild() for manual refresh
   ────────────────────────────────────────────────────────────── */

var nDynamic = (function () {
    'use strict';

    /* ═══════════════════════════════════
       Internal State
       ═══════════════════════════════════ */

    var _container  = null;
    var _controls   = [];
    var _config     = {};
    var _observer   = null;
    var _debounceId = null;
    var _lastWidth  = 0;
    var _lastHeight = 0;
    var _resizeHandler = null;          // stored ref for cleanup
    var _docSliderListeners = null;     // single set of document-level slider listeners

    /* ═══════════════════════════════════
       Helpers (delegated to nUtils)
       ═══════════════════════════════════ */

    var escHtml     = nUtils.escHtml;
    var gridCorners = nUtils.gridCorners;

    /* ═══════════════════════════════════
       Creative Widget Helpers
       ═══════════════════════════════════ */

    /**
     * Deterministic hash for matrix patterns (FNV-1a variant).
     */
    function matrixHash(val) {
        var h = 2166136261;
        h = Math.imul(h ^ (val & 0xff), 16777619);
        h = Math.imul(h ^ ((val >> 8) & 0xff), 16777619);
        h = Math.imul(h ^ ((val >> 16) & 0xff), 16777619);
        return h >>> 0;
    }

    /**
     * FNV-1a hash for strings — returns a 32-bit unsigned integer.
     * Used to derive deterministic wave parameters from widget names.
     */
    function stringHash(str) {
        var h = 2166136261;
        for (var i = 0; i < str.length; i++) {
            h ^= str.charCodeAt(i);
            h = Math.imul(h, 16777619);
        }
        return h >>> 0;
    }

    /**
     * Build lit-cell boolean array for the matrix widget.
     * Creates unique geometric patterns per value level.
     */
    function buildMatrixPattern(value, max, size, label) {
        var total = size * size;
        var cells = new Array(total);
        for (var i = 0; i < total; i++) cells[i] = false;
        if (value <= 0) return cells;

        var pct = Math.min(1, value / max);
        var litCount = Math.max(1, Math.round(pct * total));
        var nameSeed = label ? stringHash(label) : 0;
        var seed = matrixHash((value * 137 + max * 31 + size) ^ nameSeed);

        /* Generate symmetric patterns: mirror across both axes */
        var half = Math.ceil(size / 2);
        var candidates = [];
        for (var row = 0; row < half; row++) {
            for (var col = 0; col < half; col++) {
                candidates.push([row, col]);
            }
        }

        /* Shuffle candidates deterministically */
        for (var si = candidates.length - 1; si > 0; si--) {
            seed = (seed * 1103515245 + 12345) >>> 0;
            var j = seed % (si + 1);
            var tmp = candidates[si]; candidates[si] = candidates[j]; candidates[j] = tmp;
        }

        /* Light up quads symmetrically until we reach litCount */
        var lit = 0;
        for (var ci = 0; ci < candidates.length && lit < litCount; ci++) {
            var r = candidates[ci][0];
            var c = candidates[ci][1];
            /* Mirror positions */
            var mirrors = [
                [r, c],
                [r, size - 1 - c],
                [size - 1 - r, c],
                [size - 1 - r, size - 1 - c]
            ];
            /* Deduplicate for center row/col */
            var seen = {};
            for (var mi = 0; mi < mirrors.length; mi++) {
                var key = mirrors[mi][0] * size + mirrors[mi][1];
                if (!seen[key] && lit < litCount) {
                    seen[key] = true;
                    cells[key] = true;
                    lit++;
                }
            }
        }
        return cells;
    }

    /**
     * Resolve color string to CSS hex for canvas drawing.
     */
    function resolveCanvasColor(colorName) {
        switch (colorName) {
            case 'amber':  return '#FFB300';
            case 'danger': return '#FF3333';
            default:       return '#00e5ff';
        }
    }

    /**
     * Start the wave canvas animation loop.
     * Wave shape is deterministically derived from the widget label.
     */
    function animateWave(canvas) {
        var ctx = canvas.getContext('2d');
        var t = 0;

        /* Derive stable wave parameters from the widget label */
        var label = canvas.dataset.label || 'WAVE';
        var seed = stringHash(label);

        /* Extract pseudo-random floats from different bit ranges of the hash */
        function frac(s) { return ((s & 0xffff) / 0xffff); }
        var s1 = seed;
        var s2 = stringHash(label + '_2');
        var s3 = stringHash(label + '_3');

        /* Each widget gets a unique combination of 3 wave layers */
        var baseLayers = [
            { freq: 1.5 + frac(s1) * 3.0,  speed:  0.6 + frac(s1 >> 8) * 1.2,  phase: frac(s1 >> 4) * Math.PI * 2 },
            { freq: 2.0 + frac(s2) * 4.0,  speed: -(0.4 + frac(s2 >> 8) * 1.0), phase: frac(s2 >> 4) * Math.PI * 2 },
            { freq: 1.0 + frac(s3) * 2.5,  speed:  0.8 + frac(s3 >> 8) * 1.6,  phase: frac(s3 >> 4) * Math.PI * 2 }
        ];

        function draw() {
            if (!canvas.isConnected) return;
            var rect = canvas.getBoundingClientRect();
            var w = Math.round(rect.width * (window.devicePixelRatio || 1));
            var h = Math.round(rect.height * (window.devicePixelRatio || 1));
            if (w < 2 || h < 2) { requestAnimationFrame(draw); return; }
            if (canvas.width !== w || canvas.height !== h) {
                canvas.width = w; canvas.height = h;
            }
            var val = parseInt(canvas.dataset.value) || 0;
            var max = parseInt(canvas.dataset.max) || 100;
            var pct = Math.min(1, val / max);
            var color = resolveCanvasColor(canvas.dataset.color);
            var r2 = parseInt(color.slice(1, 3), 16);
            var g2 = parseInt(color.slice(3, 5), 16);
            var b2 = parseInt(color.slice(5, 7), 16);

            ctx.clearRect(0, 0, w, h);
            t += 0.03;

            /* Amplitude & alpha scale with the value */
            var amps   = [0.08 + pct * 0.35, 0.05 + pct * 0.25, 0.03 + pct * 0.45];
            var alphas = [0.15 + pct * 0.15, 0.25 + pct * 0.2,  0.4  + pct * 0.4];

            for (var li = 0; li < 3; li++) {
                var B = baseLayers[li];
                var amp   = amps[li];
                var alpha = alphas[li];

                /* — filled area — */
                ctx.beginPath();
                ctx.moveTo(0, h / 2);
                for (var x = 0; x <= w; x += 2) {
                    var nx = x / w;
                    var env = Math.sin(nx * Math.PI);
                    var y = h / 2 + Math.sin(nx * B.freq * Math.PI * 2 + t * B.speed * 3 + B.phase) * (h * amp * env);
                    if (pct > 0.6) {
                        var distort = (pct - 0.6) * 2.5;
                        y += Math.sin(nx * 7.3 * Math.PI + t * 2.1) * (h * 0.05 * distort * env);
                    }
                    ctx.lineTo(x, y);
                }
                ctx.lineTo(w, h);
                ctx.lineTo(0, h);
                ctx.closePath();
                ctx.fillStyle = 'rgba(' + r2 + ',' + g2 + ',' + b2 + ',' + alpha + ')';
                ctx.fill();

                /* — stroke top edge — */
                ctx.beginPath();
                for (var x2 = 0; x2 <= w; x2 += 2) {
                    var nx2 = x2 / w;
                    var env2 = Math.sin(nx2 * Math.PI);
                    var y2 = h / 2 + Math.sin(nx2 * B.freq * Math.PI * 2 + t * B.speed * 3 + B.phase) * (h * amp * env2);
                    if (pct > 0.6) {
                        var distort2 = (pct - 0.6) * 2.5;
                        y2 += Math.sin(nx2 * 7.3 * Math.PI + t * 2.1) * (h * 0.05 * distort2 * env2);
                    }
                    if (x2 === 0) ctx.moveTo(x2, y2);
                    else ctx.lineTo(x2, y2);
                }
                ctx.strokeStyle = 'rgba(' + r2 + ',' + g2 + ',' + b2 + ',' + Math.min(1, alpha + 0.3) + ')';
                ctx.lineWidth = 1.5;
                ctx.stroke();
            }

            requestAnimationFrame(draw);
        }
        requestAnimationFrame(draw);
    }

    /**
     * Start the spark canvas animation loop.
     * Maintains history on the canvas element.
     */
    function animateSpark(canvas) {
        var ctx = canvas.getContext('2d');
        if (!canvas._sparkHistory) canvas._sparkHistory = [];
        var maxPts = 60;

        function draw() {
            if (!canvas.isConnected) return;
            var rect = canvas.getBoundingClientRect();
            var w = Math.round(rect.width * (window.devicePixelRatio || 1));
            var h = Math.round(rect.height * (window.devicePixelRatio || 1));
            if (w < 2 || h < 2) { requestAnimationFrame(draw); return; }
            if (canvas.width !== w || canvas.height !== h) {
                canvas.width = w; canvas.height = h;
            }
            var val = parseInt(canvas.dataset.value) || 0;
            var max = parseInt(canvas.dataset.max) || 100;
            var color = resolveCanvasColor(canvas.dataset.color);
            var r2 = parseInt(color.slice(1, 3), 16);
            var g2 = parseInt(color.slice(3, 5), 16);
            var b2 = parseInt(color.slice(5, 7), 16);

            var hist = canvas._sparkHistory;
            /* Push value every ~500ms (30 frames at 60fps) */
            if (!canvas._sparkFrame) canvas._sparkFrame = 0;
            canvas._sparkFrame++;
            if (canvas._sparkFrame % 30 === 0 || hist.length === 0) {
                hist.push(val);
                if (hist.length > maxPts) hist.shift();
            }

            ctx.clearRect(0, 0, w, h);

            /* Grid lines */
            ctx.strokeStyle = 'rgba(244,244,244,0.04)';
            ctx.lineWidth = 1;
            for (var gl = 1; gl < 4; gl++) {
                var gy = Math.round(h * gl / 4) + 0.5;
                ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke();
            }

            if (hist.length < 2) { requestAnimationFrame(draw); return; }

            /* Gradient fill */
            var grad = ctx.createLinearGradient(0, 0, 0, h);
            grad.addColorStop(0, 'rgba(' + r2 + ',' + g2 + ',' + b2 + ',0.25)');
            grad.addColorStop(1, 'rgba(' + r2 + ',' + g2 + ',' + b2 + ',0.02)');

            ctx.beginPath();
            var stepX = w / (maxPts - 1);
            var startIdx = maxPts - hist.length;
            for (var pi = 0; pi < hist.length; pi++) {
                var px = (startIdx + pi) * stepX;
                var py = h - (hist[pi] / max) * h * 0.9 - h * 0.05;
                if (pi === 0) ctx.moveTo(px, py);
                else ctx.lineTo(px, py);
            }
            /* Fill area */
            var lastX = (startIdx + hist.length - 1) * stepX;
            ctx.lineTo(lastX, h);
            ctx.lineTo(startIdx * stepX, h);
            ctx.closePath();
            ctx.fillStyle = grad;
            ctx.fill();

            /* Line */
            ctx.beginPath();
            for (var pi2 = 0; pi2 < hist.length; pi2++) {
                var px2 = (startIdx + pi2) * stepX;
                var py2 = h - (hist[pi2] / max) * h * 0.9 - h * 0.05;
                if (pi2 === 0) ctx.moveTo(px2, py2);
                else ctx.lineTo(px2, py2);
            }
            ctx.strokeStyle = color;
            ctx.lineWidth = 2;
            ctx.stroke();

            /* Current point glow */
            var cpx = lastX;
            var cpy = h - (hist[hist.length - 1] / max) * h * 0.9 - h * 0.05;
            ctx.beginPath();
            ctx.arc(cpx, cpy, 4, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.fill();
            ctx.beginPath();
            ctx.arc(cpx, cpy, 8, 0, Math.PI * 2);
            ctx.fillStyle = 'rgba(' + r2 + ',' + g2 + ',' + b2 + ',0.2)';
            ctx.fill();

            requestAnimationFrame(draw);
        }
        requestAnimationFrame(draw);
    }

    /**
     * Start the scope (oscilloscope) canvas animation loop.
     */
    function animateScope(canvas) {
        var ctx = canvas.getContext('2d');
        var phase = 0;
        /* phosphor buffer for afterglow */
        var imgData = null;

        function draw() {
            if (!canvas.isConnected) return;
            var rect = canvas.getBoundingClientRect();
            var w = Math.round(rect.width * (window.devicePixelRatio || 1));
            var h = Math.round(rect.height * (window.devicePixelRatio || 1));
            if (w < 2 || h < 2) { requestAnimationFrame(draw); return; }
            if (canvas.width !== w || canvas.height !== h) {
                canvas.width = w; canvas.height = h;
                imgData = null;
            }
            var val = parseInt(canvas.dataset.value) || 0;
            var max = parseInt(canvas.dataset.max) || 100;
            var pct = Math.min(1, val / max);
            var color = resolveCanvasColor(canvas.dataset.color);
            var r2 = parseInt(color.slice(1, 3), 16);
            var g2 = parseInt(color.slice(3, 5), 16);
            var b2 = parseInt(color.slice(5, 7), 16);

            phase += 0.04;

            /* Afterglow: fade existing content */
            if (imgData) {
                ctx.putImageData(imgData, 0, 0);
            }
            ctx.fillStyle = 'rgba(0, 6, 4, 0.15)';
            ctx.fillRect(0, 0, w, h);

            /* Grid */
            ctx.strokeStyle = 'rgba(' + r2 + ',' + g2 + ',' + b2 + ',0.06)';
            ctx.lineWidth = 1;
            for (var gx = 1; gx < 8; gx++) {
                var lx = Math.round(w * gx / 8) + 0.5;
                ctx.beginPath(); ctx.moveTo(lx, 0); ctx.lineTo(lx, h); ctx.stroke();
            }
            for (var gy2 = 1; gy2 < 4; gy2++) {
                var ly = Math.round(h * gy2 / 4) + 0.5;
                ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(w, ly); ctx.stroke();
            }

            /* Waveform trace */
            ctx.beginPath();
            var amp = pct * 0.4;
            var freq = 2 + pct * 3;
            for (var sx = 0; sx <= w; sx += 1) {
                var nx = sx / w;
                var sy = h / 2 +
                    Math.sin(nx * freq * Math.PI * 2 + phase) * h * amp +
                    Math.sin(nx * freq * 2.7 * Math.PI + phase * 0.7) * h * amp * 0.3;
                if (sx === 0) ctx.moveTo(sx, sy);
                else ctx.lineTo(sx, sy);
            }
            ctx.strokeStyle = 'rgba(' + r2 + ',' + g2 + ',' + b2 + ',' + (0.6 + pct * 0.4) + ')';
            ctx.lineWidth = 2;
            ctx.shadowColor = color;
            ctx.shadowBlur = 6 + pct * 10;
            ctx.stroke();
            ctx.shadowBlur = 0;

            /* Scan line */
            var scanX = (phase * 40 % w);
            ctx.beginPath();
            ctx.moveTo(scanX, 0);
            ctx.lineTo(scanX, h);
            ctx.strokeStyle = 'rgba(' + r2 + ',' + g2 + ',' + b2 + ',0.3)';
            ctx.lineWidth = 1;
            ctx.stroke();

            imgData = ctx.getImageData(0, 0, w, h);
            requestAnimationFrame(draw);
        }
        requestAnimationFrame(draw);
    }

    /* ═══════════════════════════════════
       Segmented Bar Animations
       Canonical implementation — traveling dim square
       for partial fill, blink for full fill. Matches
       the main demo exactly.
       ═══════════════════════════════════ */

    function initSegBar(el) {
        // Clean up previous animation timers
        if (el._segTimer)   { clearInterval(el._segTimer);  el._segTimer = null; }
        if (el._segTimeout) { clearTimeout(el._segTimeout);  el._segTimeout = null; }

        var max   = parseInt(el.dataset.max) || 10;
        var value = parseInt(el.dataset.value) || 0;
        el.innerHTML = '';
        el._prevValue = value;  // Track for animated transitions
        el._segDir = 1;        // Animation direction: 1 = forward, -1 = reverse
        var isFull = (value >= max);
        if (isFull) el.classList.add('bar-full');
        else        el.classList.remove('bar-full');

        var filledSegs = [];
        for (var i = 0; i < max; i++) {
            var seg = document.createElement('div');
            seg.className = 'seg' + (i < value ? ' filled' : '');
            el.appendChild(seg);
            if (i < value) filledSegs.push(seg);
        }

        if (isFull && filledSegs.length > 1) {
            segBarBlink(el, filledSegs);
        } else if (filledSegs.length > 1) {
            segBarTravel(el, filledSegs);
        }
    }

    /**
     * updateSegBar — Animate a bar from its current value to a new value.
     * Decrease: fade-out the removed segments over 150ms then restart animation.
     * Increase: immediately show new segments with a brief bright flash.
     */
    function updateSegBar(el, newValue) {
        if (el._segTimer)    { clearInterval(el._segTimer);   el._segTimer = null; }
        if (el._segTimeout)  { clearTimeout(el._segTimeout);  el._segTimeout = null; }
        if (el._updateTimeout) { clearTimeout(el._updateTimeout); el._updateTimeout = null; }

        var max      = parseInt(el.dataset.max) || 10;
        var oldValue = el._prevValue !== undefined ? el._prevValue : (parseInt(el.dataset.value) || 0);
        newValue = Math.max(0, Math.min(max, newValue));
        el.dataset.value = newValue;
        el._prevValue = newValue;

        /* Determine animation direction from value delta */
        if (newValue !== oldValue) {
            el._segDir = newValue > oldValue ? 1 : -1;
        }

        var segs = el.querySelectorAll('.seg');
        if (!segs.length) { initSegBar(el); return; }

        var isFull = (newValue >= max);
        if (isFull) el.classList.add('bar-full');
        else        el.classList.remove('bar-full');

        if (newValue < oldValue) {
            /* ── Decrease: fade removed segments to the unfilled grey ── */
            for (var d = oldValue - 1; d >= newValue; d--) {
                if (segs[d]) {
                    segs[d].classList.remove('filled');
                }
            }
            /* After background transition completes, clean up and restart */
            el._updateTimeout = setTimeout(function () {
                el._updateTimeout = null;
                for (var i = 0; i < max; i++) {
                    segs[i].classList.remove('seg-dim', 'seg-bright', 'seg-flash');
                    if (i < newValue) segs[i].classList.add('filled');
                    else              segs[i].classList.remove('filled');
                }
                restartBarAnim(el, newValue, max);
            }, 350);

        } else if (newValue > oldValue) {
            /* ── Increase: show immediately, flash briefly ── */
            for (var j = 0; j < max; j++) {
                segs[j].classList.remove('seg-dim', 'seg-bright', 'seg-flash');
                if (j < newValue) segs[j].classList.add('filled');
                else              segs[j].classList.remove('filled');
            }
            /* Flash the newly added segments */
            for (var k = oldValue; k < newValue; k++) {
                if (segs[k]) segs[k].classList.add('seg-flash');
            }
            el._updateTimeout = setTimeout(function () {
                el._updateTimeout = null;
                for (var f = oldValue; f < newValue; f++) {
                    if (segs[f]) segs[f].classList.remove('seg-flash');
                }
                restartBarAnim(el, newValue, max);
            }, 350);
        }
        /* If value unchanged, do nothing */
    }

    function restartBarAnim(el, value, max) {
        if (el._segTimer)   { clearInterval(el._segTimer);  el._segTimer = null; }
        if (el._segTimeout) { clearTimeout(el._segTimeout);  el._segTimeout = null; }
        var segs = el.querySelectorAll('.seg');
        var filledSegs = [];
        for (var i = 0; i < value && i < segs.length; i++) {
            filledSegs.push(segs[i]);
        }
        if (value >= max && filledSegs.length > 1) {
            segBarBlink(el, filledSegs);
        } else if (filledSegs.length > 1) {
            segBarTravel(el, filledSegs);
        }
    }

    /* One darker square snaps across the filled segments, direction-aware.
       Direction (el._segDir): 1 = forward (left-to-right), -1 = reverse (right-to-left). */
    function segBarTravel(el, segs) {
        var dir = el._segDir || 1;
        var pos = dir === 1 ? 0 : segs.length - 1;
        segs[pos].classList.add('seg-dim');
        el._segTimer = setInterval(function () {
            var d = el._segDir || 1;
            segs[pos].classList.remove('seg-dim');
            pos = (pos + d + segs.length) % segs.length;
            segs[pos].classList.add('seg-dim');
        }, 120);
    }

    /* 2 flashes (50 ms on / 50 ms off) then 350 ms pause */
    function segBarBlink(el, segs) {
        var step = 0;
        function tick() {
            if (step < 4) {
                var on = (step % 2 === 0);
                for (var i = 0; i < segs.length; i++) {
                    if (on) segs[i].classList.add('seg-bright');
                    else    segs[i].classList.remove('seg-bright');
                }
                step++;
                el._segTimeout = setTimeout(tick, 50);
            } else {
                step = 0;
                el._segTimeout = setTimeout(tick, 350);
            }
        }
        tick();
    }

    /* ═══════════════════════════════════
       Dialog System (self-contained)
       Provides showDialog / closeDialog for any
       page that includes nDynamic + the dialog overlay HTML.
       ═══════════════════════════════════ */

    var _dialogResolve      = null;
    var _cornerStrobeTimer  = null;

    function showDialog(opts) {
        var $overlay   = document.getElementById('dialog-overlay');
        var $dialogBox = document.getElementById('dialog-box');
        if (!$overlay || !$dialogBox) {
            console.warn('[nDynamic] dialog-overlay / dialog-box not found in DOM');
            return Promise.resolve(null);
        }

        var flashOutline = window.flashOutline || function () { return true; };
        var FLASH_DURATION = 200;

        return new Promise(function (resolve) {
            _dialogResolve = resolve;

            var dialogBeepTitle = (opts.title || '').trim();
            var dialogBeepBody  = (opts.body  || '').trim();

            var html =
                '<span class="corner corner-tl">\u231C</span>' +
                '<span class="corner corner-tr">\u231D</span>' +
                '<span class="corner corner-bl">\u231E</span>' +
                '<span class="corner corner-br">\u231F</span>';
            if (opts.title) html += '<div class="dialog-title">' + escHtml(opts.title) + '</div>';
            if (opts.body)  html += '<div class="dialog-body">'  + escHtml(opts.body)  + '</div>';

            html += '<div class="dialog-actions">';
            (opts.buttons || []).forEach(function (btn) {
                var cls = 'dialog-btn';
                if (btn.style) cls += ' ' + btn.style;
                html += '<button class="' + cls + '" data-val="' + escHtml(btn.value) + '">' + escHtml(btn.label) + '</button>';
            });
            html += '</div>';

            $dialogBox.innerHTML = html;

            $dialogBox.querySelectorAll('.dialog-btn').forEach(function (el) {
                el.addEventListener('click', function () {
                    if (typeof flashOutline === 'function' && !flashOutline(el)) return;
                    if (typeof nbeep === 'function') {
                        nbeep('dialog_btn_' + (el.dataset.val || el.textContent));
                    }
                    setTimeout(function () { closeDialog(el.dataset.val); }, FLASH_DURATION);
                });
            });

            $overlay.classList.add('open');

            // Looping corner strobe: 5 flashes (50ms on/off), 200ms pause, repeat
            (function startCornerStrobe() {
                if (_cornerStrobeTimer) clearTimeout(_cornerStrobeTimer);
                var corners = $dialogBox.querySelectorAll('.corner');
                var flash = 0;
                var totalFlashes = 5;
                var on = false;

                function tick() {
                    if (!$overlay.classList.contains('open')) {
                        corners.forEach(function (c) { c.classList.remove('flash-outline'); });
                        _cornerStrobeTimer = null;
                        return;
                    }
                    if (flash < totalFlashes * 2) {
                        on = !on;
                        corners.forEach(function (c) {
                            if (on) c.classList.add('flash-outline');
                            else    c.classList.remove('flash-outline');
                        });
                        flash++;
                        _cornerStrobeTimer = setTimeout(tick, 50);
                    } else {
                        corners.forEach(function (c) { c.classList.remove('flash-outline'); });
                        _cornerStrobeTimer = setTimeout(function () {
                            flash = 0;
                            tick();
                        }, 200);
                    }
                }

                _cornerStrobeTimer = setTimeout(tick, 50);
            })();

            // Start looping alarm or one-shot beep
            //   opts.alarm (default true): looping alarm beep
            //   opts.beep: one-shot beep, can be:
            //     true          → play a single default-length beep
            //     number (ms)   → play a single beep of that duration
            //     {duration:ms} → play a single beep of that duration
            //   When opts.beep is set, opts.alarm is ignored.
            var hasBeepOpt = opts.beep !== undefined && opts.beep !== false;
            var playAlarm  = hasBeepOpt ? false : (opts.alarm !== undefined ? opts.alarm : true);

            if (typeof nbeep === 'function' && dialogBeepTitle) {
                if (hasBeepOpt) {
                    // One-shot beep (no loop)
                    var beepDur = null; // null = use global config duration
                    if (typeof opts.beep === 'number' && opts.beep > 0) {
                        beepDur = opts.beep;
                    } else if (typeof opts.beep === 'object' && opts.beep && typeof opts.beep.duration === 'number') {
                        beepDur = opts.beep.duration;
                    }
                    nbeep(dialogBeepTitle, false, undefined, beepDur);
                } else if (playAlarm) {
                    nbeep(dialogBeepTitle, dialogBeepBody || true);
                }
            }
        });
    }

    function closeDialog(value) {
        var $overlay   = document.getElementById('dialog-overlay');
        var $dialogBox = document.getElementById('dialog-box');

        if (_cornerStrobeTimer) {
            clearTimeout(_cornerStrobeTimer);
            _cornerStrobeTimer = null;
            if ($dialogBox) {
                $dialogBox.querySelectorAll('.corner').forEach(function (c) {
                    c.classList.remove('flash-outline');
                });
            }
        }
        if ($overlay) $overlay.classList.remove('open');
        if (typeof nDesignAudio === 'object' && nDesignAudio.killActive) {
            nDesignAudio.killActive();
        }
        if (_dialogResolve) {
            var fn = _dialogResolve;
            _dialogResolve = null;
            fn(value);
        }
    }

    // Allow overlay click to dismiss
    (function () {
        var $overlay = document.getElementById('dialog-overlay');
        if ($overlay) {
            $overlay.addEventListener('click', function (e) {
                if (e.target === $overlay) {
                    if (typeof nbeep === 'function') nbeep('dialog_dismiss');
                    closeDialog(null);
                }
            });
        }
    })();

    /* ═══════════════════════════════════
       Grid Dimension Calculator
       ═══════════════════════════════════ */

    /**
     * Compute optimal cols / rowHeight to fill the viewport
     * without scrolling, given the control catalog.
     */
    function computeGrid(containerW, availH, controls, cfg) {
        var gap     = cfg.gap || 6;
        var padding = cfg.padding || 16;

        // Effective area (subtract padding on both axes — box-sizing: border-box)
        var usableW = containerW - padding * 2;
        var usableH = availH - padding * 2;

        // Determine column count
        var cols;
        if (cfg.cols) {
            cols = cfg.cols;
        } else {
            // Auto: pick cols so a 1×1 cell is roughly square.
            // totalArea = sum of (item.cols × item.rows); with perfect packing
            // rows ≈ totalArea / cols.  Square cells ⟹ cols/rows ≈ W/H
            // ⟹ cols = √(totalArea × W/H).
            var totalArea = 0;
            for (var ci = 0; ci < controls.length; ci++) {
                totalArea += (controls[ci].cols || 1) * (controls[ci].rows || 1);
            }
            if (totalArea < 1) totalArea = 1;
            cols = Math.max(2, Math.round(Math.sqrt(totalArea * usableW / usableH)));
        }

        // Count total row-cells needed if laid out optimally
        // Simulate a simple greedy row-packing to estimate total rows
        var totalRows = estimateRows(controls, cols, cfg);

        // Calculate row height to fill available height
        var rowHeight;
        if (cfg.rowHeight) {
            rowHeight = cfg.rowHeight;
        } else {
            // rowHeight = (availH - (totalRows-1)*gap) / totalRows
            rowHeight = Math.max(40, Math.floor((usableH - (totalRows - 1) * gap) / totalRows));
            // Cap max row height for visual sanity
            rowHeight = Math.min(rowHeight, 120);
        }

        return { cols: cols, rowHeight: rowHeight, gap: gap, padding: padding, totalRows: totalRows };
    }

    /**
     * Estimate total grid rows consumed by greedy dense packing.
     * Uses a row-occupancy bitmap approach for accuracy.
     */
    function estimateRows(controls, cols, cfg) {
        var ordered = getOrderedControls(controls, cfg);
        // Bitmap: grid[row][col] = true if occupied
        var grid = [];
        var maxRow = 0;

        function isOpen(r, c, rowSpan, colSpan) {
            for (var dr = 0; dr < rowSpan; dr++) {
                for (var dc = 0; dc < colSpan; dc++) {
                    if (c + dc >= cols) return false;
                    if (grid[r + dr] && grid[r + dr][c + dc]) return false;
                }
            }
            return true;
        }

        function occupy(r, c, rowSpan, colSpan) {
            for (var dr = 0; dr < rowSpan; dr++) {
                if (!grid[r + dr]) grid[r + dr] = [];
                for (var dc = 0; dc < colSpan; dc++) {
                    grid[r + dr][c + dc] = true;
                }
            }
            var bottom = r + rowSpan;
            if (bottom > maxRow) maxRow = bottom;
        }

        for (var i = 0; i < ordered.length; i++) {
            var item = ordered[i];
            var cSpan = Math.min(item.cols || 1, cols);
            var rSpan = item.rows || 1;

            // Check pinned position
            if (cfg.pinned && cfg.pinned[item._origIdx] !== undefined) {
                var pin = cfg.pinned[item._origIdx];
                var pc = Math.max(0, Math.min(pin.col || 0, cols - cSpan));
                var pr = Math.max(0, pin.row || 0);
                /* Defensive: if pinned slot overlaps, fall through to auto-place */
                if (isOpen(pr, pc, rSpan, cSpan)) {
                    occupy(pr, pc, rSpan, cSpan);
                    continue;
                }
            }

            // Find first available slot (dense packing)
            var placed = false;
            for (var r = 0; !placed; r++) {
                for (var c = 0; c <= cols - cSpan; c++) {
                    if (isOpen(r, c, rSpan, cSpan)) {
                        occupy(r, c, rSpan, cSpan);
                        placed = true;
                        break;
                    }
                }
                if (r > 200) break; // Safety valve
            }
        }

        return maxRow;
    }

    /**
     * Return controls in render order, respecting cfg.order if set.
     */
    function getOrderedControls(controls, cfg) {
        var result = [];
        for (var i = 0; i < controls.length; i++) {
            var copy = {};
            for (var k in controls[i]) copy[k] = controls[i][k];
            copy._origIdx = i;
            result.push(copy);
        }
        if (cfg.order && Array.isArray(cfg.order)) {
            var ordered = [];
            for (var j = 0; j < cfg.order.length; j++) {
                var idx = cfg.order[j];
                if (idx >= 0 && idx < result.length) {
                    ordered.push(result[idx]);
                }
            }
            // Append any controls not in the order list
            var used = {};
            for (var m = 0; m < cfg.order.length; m++) used[cfg.order[m]] = true;
            for (var n = 0; n < result.length; n++) {
                if (!used[n]) ordered.push(result[n]);
            }
            return ordered;
        }
        return result;
    }

    /* ═══════════════════════════════════
       Render Individual Control
       ═══════════════════════════════════ */

    function renderControl(item, cols) {
        var el = document.createElement('div');
        var spanCol = Math.min(item.cols || 1, cols);
        var spanRow = item.rows || 1;

        switch (item.type) {
            case 'card': {
                var isOn = item.state === 'on';
                var cardSize = item.size || ((item.cols || 2) + 'x' + (item.rows || 2));
                el.className = 'card ' + (isOn ? 'is-on' : 'is-off') + ' nd-card';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                el.dataset.on = item.on || 'ON';
                el.dataset.off = item.off || 'OFF';
                el.dataset.size = cardSize;
                el.innerHTML = gridCorners() +
                    '<div class="card-icon"><i class="ph ' + (item.icon || 'ph-circle') + '"></i></div>' +
                    '<div class="card-info">' +
                        '<div class="card-name">' + escHtml(item.name || '') + '</div>' +
                        '<div class="card-state">[ ' + escHtml(isOn ? (item.on || 'ON') : (item.off || 'OFF')) + ' ]</div>' +
                    '</div>';
                break;
            }
            case 'slider': {
                el.className = 'grid-cell gc-slider';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                el.innerHTML = gridCorners() +
                    '<div class="slider-header">' +
                        '<span class="panel-label">' + escHtml(item.label || '') + '</span>' +
                        '<span class="slider-readout">[ ' + Math.round(((item.value || 0) / (item.max || 10)) * 100) + '% ]</span>' +
                    '</div>' +
                    '<div class="seg-slider" data-value="' + (item.value || 0) + '" data-max="' + (item.max || 10) + '" data-color="' + (item.color || 'accent') + '"></div>';
                break;
            }
            case 'toggle': {
                el.className = 'grid-cell gc-toggle';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                var optHtml = '';
                (item.options || []).forEach(function (o, i) {
                    optHtml += '<div class="tg-option' + (i === item.active ? ' active' : '') + '" data-val="' + o + '">' + o + '</div>';
                });
                el.innerHTML = gridCorners() +
                    '<div class="panel-label">' + escHtml(item.label || '') + '</div>' +
                    '<div class="toggle-group">' + optHtml + '</div>';
                break;
            }
            case 'button': {
                el.className = 'action-btn grid-btn' + (item.style ? ' ' + item.style : '');
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                el.setAttribute('data-flash', '');
                if (item.lockout) {
                    el.dataset.lockout = item.lockout;
                }
                if (item.dialog) {
                    el.dataset.dialog = JSON.stringify(item.dialog);
                }
                el.innerHTML = (item.icon ? '<i class="ph ' + item.icon + '"></i>' : '') + '<span class="btn-label">' + escHtml(item.label || '') + '</span>';
                break;
            }
            case 'stepper': {
                el.className = 'grid-cell gc-stepper';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                el.innerHTML = gridCorners() +
                    '<div class="panel-label">' + escHtml(item.label || '') + '</div>' +
                    '<div class="stepper">' +
                        '<div class="stepper-btn" data-dir="-1">\u2212</div>' +
                        '<div class="stepper-val">' + (item.value || 0) + '</div>' +
                        '<div class="stepper-btn" data-dir="+1">+</div>' +
                    '</div>';
                break;
            }
            case 'bar': {
                el.className = 'grid-cell gc-bar';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                el.innerHTML = gridCorners() +
                    '<div class="slider-header">' +
                        '<span class="panel-label">' + escHtml(item.label || '') + '</span>' +
                        '<span class="slider-readout">[ ' + Math.round(((item.value || 0) / (item.max || 10)) * 100) + '% ]</span>' +
                    '</div>' +
                    '<div class="seg-bar" data-value="' + (item.value || 0) + '" data-max="' + (item.max || 10) + '" data-color="' + (item.color || 'accent') + '"></div>';
                break;
            }
            case 'status': {
                el.className = 'grid-cell gc-status';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                var rows = '';
                (item.items || []).forEach(function (r) {
                    rows += '<div class="status-row"><span class="status-label">' + escHtml(r.k || '') +
                            '</span><span class="status-val' + (r.c ? ' ' + r.c : '') + '">' +
                            escHtml(r.v || '') + '</span></div>';
                });
                el.innerHTML = gridCorners() +
                    '<div class="panel-label">' + escHtml(item.label || '') + '</div>' +
                    '<div class="status-grid">' + rows + '</div>';
                break;
            }

            /* ── New creative widget types ─────────────── */

            case 'gauge': {
                el.className = 'grid-cell gc-gauge';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                var gVal = item.value || 0;
                var gMax = item.max || 100;
                var gPct = Math.round((gVal / gMax) * 100);
                var gColor = item.color || 'accent';
                var gColorCls = gColor === 'amber' ? ' color-amber' : gColor === 'danger' ? ' color-danger' : '';
                var cx = 100, cy = 90, gr = 70, sw = 8;
                var arcLen = Math.PI * gr;
                var dashOff = arcLen * (1 - gVal / gMax);
                /* Tick marks */
                var ticks = '';
                for (var t = 0; t <= 10; t++) {
                    var ang = Math.PI + (Math.PI * t / 10);
                    var tx1 = cx + (gr + 3) * Math.cos(ang);
                    var ty1 = cy + (gr + 3) * Math.sin(ang);
                    var tx2 = cx + (gr + (t % 5 === 0 ? 10 : 6)) * Math.cos(ang);
                    var ty2 = cy + (gr + (t % 5 === 0 ? 10 : 6)) * Math.sin(ang);
                    ticks += '<line class="gauge-tick" x1="' + tx1 + '" y1="' + ty1 + '" x2="' + tx2 + '" y2="' + ty2 + '"/>';
                }
                /* Needle */
                var needleAng = Math.PI + (Math.PI * gVal / gMax);
                var nx = cx + (gr - 16) * Math.cos(needleAng);
                var ny = cy + (gr - 16) * Math.sin(needleAng);
                el.innerHTML = gridCorners() +
                    '<div class="slider-header">' +
                        '<span class="panel-label">' + escHtml(item.label || '') + '</span>' +
                        '<span class="slider-readout' + gColorCls + '">[ ' + gPct + '% ]</span>' +
                    '</div>' +
                    '<svg class="gauge-svg" viewBox="0 0 200 120" preserveAspectRatio="xMidYMid meet">' +
                        ticks +
                        '<path class="gauge-track" d="M ' + (cx - gr) + ' ' + cy + ' A ' + gr + ' ' + gr + ' 0 0 1 ' + (cx + gr) + ' ' + cy + '" stroke-width="' + sw + '"/>' +
                        '<path class="gauge-fill' + gColorCls + '" d="M ' + (cx - gr) + ' ' + cy + ' A ' + gr + ' ' + gr + ' 0 0 1 ' + (cx + gr) + ' ' + cy + '" stroke-width="' + sw + '" stroke-dasharray="' + arcLen.toFixed(2) + '" stroke-dashoffset="' + dashOff.toFixed(2) + '" data-arc="' + arcLen.toFixed(2) + '"/>' +
                        '<line x1="' + cx + '" y1="' + cy + '" x2="' + nx.toFixed(1) + '" y2="' + ny.toFixed(1) + '" stroke="' + (gColor === 'amber' ? 'var(--amber)' : gColor === 'danger' ? 'var(--danger)' : 'var(--accent)') + '" stroke-width="2" stroke-linecap="round" class="gauge-needle"/>' +
                        '<circle cx="' + cx + '" cy="' + cy + '" r="3" fill="var(--text)"/>' +
                    '</svg>';
                el.dataset.max = gMax;
                el.dataset.color = gColor;
                break;
            }

            case 'wave': {
                el.className = 'grid-cell gc-wave';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                var wVal = item.value || 0;
                var wMax = item.max || 100;
                var wPct = Math.round((wVal / wMax) * 100);
                var wColor = item.color || 'accent';
                var wColorCls = wColor === 'amber' ? ' color-amber' : wColor === 'danger' ? ' color-danger' : '';
                el.innerHTML = gridCorners() +
                    '<div class="slider-header">' +
                        '<span class="panel-label">' + escHtml(item.label || '') + '</span>' +
                        '<span class="slider-readout' + wColorCls + '">[ ' + wPct + '% ]</span>' +
                    '</div>' +
                    '<canvas class="wave-canvas" data-value="' + wVal + '" data-max="' + wMax + '" data-color="' + wColor + '" data-label="' + escHtml(item.label || 'WAVE') + '"></canvas>';
                break;
            }

            case 'matrix': {
                el.className = 'grid-cell gc-matrix';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                var mVal = item.value || 0;
                var mMax = item.max || 100;
                var mPct = Math.round((mVal / mMax) * 100);
                var mSize = item.gridSize || 8;
                var mColor = item.color || 'accent';
                var mColorCls = mColor === 'amber' ? ' color-amber' : mColor === 'danger' ? ' color-danger' : '';
                var mLabel = item.label || '';
                var mCells = buildMatrixPattern(mVal, mMax, mSize, mLabel);
                var mHtml = '';
                for (var mi = 0; mi < mSize * mSize; mi++) {
                    mHtml += '<div class="matrix-cell' + (mCells[mi] ? ' lit' + mColorCls : '') + '"></div>';
                }
                el.innerHTML = gridCorners() +
                    '<div class="slider-header">' +
                        '<span class="panel-label">' + escHtml(mLabel) + '</span>' +
                        '<span class="slider-readout' + mColorCls + '">[ ' + mPct + '% ]</span>' +
                    '</div>' +
                    '<div class="matrix-grid" data-value="' + mVal + '" data-max="' + mMax + '" data-size="' + mSize + '" data-color="' + mColor + '" data-label="' + escHtml(mLabel) + '" style="grid-template-columns:repeat(' + mSize + ',1fr);grid-template-rows:repeat(' + mSize + ',1fr)">' + mHtml + '</div>';
                break;
            }

            case 'ring': {
                el.className = 'grid-cell gc-ring';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                var rItems = item.items || [];
                var ringColors = ['var(--accent)', 'var(--amber)', 'var(--danger)', '#9c27b0', '#4caf50'];
                var rcx = 100, rcy = 100;
                var ringHtml = '';
                var rSpacing = rItems.length <= 3 ? 14 : 10;
                var rOuter = 80;
                var rSw = rItems.length <= 3 ? 8 : 6;
                for (var ri = 0; ri < rItems.length; ri++) {
                    var rr = rOuter - ri * rSpacing;
                    var rCirc = 2 * Math.PI * rr;
                    var rPct = Math.min(1, (rItems[ri].value || 0) / (rItems[ri].max || 100));
                    var rOff = rCirc * (1 - rPct);
                    var rCol = rItems[ri].color ? 'var(--' + rItems[ri].color + ')' : ringColors[ri % ringColors.length];
                    ringHtml += '<circle class="ring-track" cx="' + rcx + '" cy="' + rcy + '" r="' + rr + '" stroke-width="' + rSw + '"/>';
                    ringHtml += '<circle class="ring-fill" cx="' + rcx + '" cy="' + rcy + '" r="' + rr + '" stroke-width="' + rSw + '" stroke="' + rCol + '" stroke-dasharray="' + rCirc.toFixed(2) + '" stroke-dashoffset="' + rOff.toFixed(2) + '" transform="rotate(-90 ' + rcx + ' ' + rcy + ')" data-circ="' + rCirc.toFixed(2) + '"/>';
                }
                /* Build corner labels — up to 4 corners: TL, TR, BL, BR */
                var cornerPos = ['tl', 'tr', 'bl', 'br'];
                var cornerHtml = '';
                for (var ci = 0; ci < Math.min(rItems.length, 4); ci++) {
                    var cName = rItems[ci].name || '';
                    var cPctVal = Math.round(((rItems[ci].value || 0) / (rItems[ci].max || 100)) * 100);
                    var cCol = rItems[ci].color ? 'var(--' + rItems[ci].color + ')' : ringColors[ci % ringColors.length];
                    cornerHtml += '<span class="ring-corner ring-corner-' + cornerPos[ci] + '" style="color:' + cCol + '">' +
                        '<span class="ring-corner-name">' + escHtml(cName) + '</span>' +
                        '<span class="ring-corner-val" data-ring-idx="' + ci + '">[ ' + cPctVal + '% ]</span>' +
                    '</span>';
                }
                /* Fill remaining corners with decorative brackets */
                for (var cj = rItems.length; cj < 4; cj++) {
                    var brk = cj === 0 ? '\u231C' : cj === 1 ? '\u231D' : cj === 2 ? '\u231E' : '\u231F';
                    cornerHtml += '<span class="corner corner-' + cornerPos[cj] + '">' + brk + '</span>';
                }
                el.innerHTML = cornerHtml +
                    '<svg class="ring-svg" viewBox="0 0 200 200" preserveAspectRatio="xMidYMid meet">' + ringHtml + '</svg>';
                break;
            }

            case 'spark': {
                el.className = 'grid-cell gc-spark';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                var spVal = item.value || 0;
                var spMax = item.max || 100;
                var spColor = item.color || 'accent';
                var spColorCls = spColor === 'amber' ? ' color-amber' : spColor === 'danger' ? ' color-danger' : '';
                var spPct = Math.round((spVal / spMax) * 100);
                el.innerHTML = gridCorners() +
                    '<div class="slider-header">' +
                        '<span class="panel-label">' + escHtml(item.label || '') + '</span>' +
                        '<span class="slider-readout' + spColorCls + '">[ ' + spPct + '% ]</span>' +
                    '</div>' +
                    '<canvas class="spark-canvas" data-value="' + spVal + '" data-max="' + spMax + '" data-color="' + spColor + '"></canvas>';
                break;
            }

            case 'scope': {
                el.className = 'grid-cell gc-scope';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                var scVal = item.value || 0;
                var scMax = item.max || 100;
                var scColor = item.color || 'accent';
                el.innerHTML = gridCorners() +
                    '<div class="panel-label">' + escHtml(item.label || '') + '</div>' +
                    '<canvas class="scope-canvas" data-value="' + scVal + '" data-max="' + scMax + '" data-color="' + scColor + '"></canvas>';
                break;
            }

            case 'level': {
                el.className = 'grid-cell gc-level';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + spanRow;
                var lVal = item.value || 0;
                var lMax = item.max || 20;
                var lColor = item.color || 'accent';
                var lColorCls = lColor === 'amber' ? ' color-amber' : lColor === 'danger' ? ' color-danger' : '';
                var lPct = Math.round((lVal / lMax) * 100);
                var warnThresh = Math.floor(lMax * 0.7);
                var critThresh = Math.floor(lMax * 0.9);
                var segs = '';
                for (var li = 0; li < lMax; li++) {
                    var lFilled = li < lVal;
                    var lZone = li >= critThresh ? ' crit' : li >= warnThresh ? ' warn' : '';
                    var lPeakCls = (li === lVal - 1 && lVal > 0) ? ' peak-hold' : '';
                    segs += '<div class="level-seg' + (lFilled ? ' filled' + lZone + lPeakCls : '') + '"></div>';
                }
                el.innerHTML = gridCorners() +
                    '<div class="slider-header">' +
                        '<span class="panel-label">' + escHtml(item.label || '') + '</span>' +
                        '<span class="slider-readout' + lColorCls + '">[ ' + lPct + '% ]</span>' +
                    '</div>' +
                    '<div class="level-meter" data-value="' + lVal + '" data-max="' + lMax + '" data-color="' + lColor + '">' + segs + '</div>';
                break;
            }
        }
        return el;
    }

    /* ═══════════════════════════════════
       Interactive Behavior Wiring
       ═══════════════════════════════════ */

    function wireInteractions(container) {
        var flashOutline = window.flashOutline || function () { return true; };

        /* Check if an element (or its grid-item ancestor) is muted by nInteractive */
        function isMuted(el) {
            return !!(el && el.closest && el.closest('.ni-muted'));
        }

        /**
         * Play a beep respecting per-control hash suffix and soundscape theme.
         * Reads data-ni-beep (hash suffix 001-999) and data-ni-theme (soundMode)
         * from the nearest grid-cell ancestor.
         */
        function beep(el, text, loop, pitch) {
            if (typeof nbeep !== 'function' || isMuted(el)) return;
            var cell = el.closest && el.closest('[data-ni-beep],[data-ni-theme]');
            var suffix = cell && cell.dataset.niBeep ? cell.dataset.niBeep : '';
            var theme  = cell && cell.dataset.niTheme ? cell.dataset.niTheme : '';
            var oldMode;
            if (theme && typeof nDesignAudio !== 'undefined') {
                oldMode = nDesignAudio.config.soundMode;
                nDesignAudio.config.soundMode = theme;
            }
            nbeep(suffix ? text + suffix : text, loop, pitch);
            if (oldMode !== undefined) nDesignAudio.config.soundMode = oldMode;
        }

        // Card toggles
        container.querySelectorAll('.nd-card').forEach(function (card) {
            card.addEventListener('click', function () {
                if (typeof flashOutline === 'function' && !flashOutline(card)) return;
                var isOn = card.classList.contains('is-on');
                var onLabel  = card.dataset.on || 'ON';
                var offLabel = card.dataset.off || 'OFF';
                card.classList.remove('is-on', 'is-off');
                card.classList.add(isOn ? 'is-off' : 'is-on');
                var stateEl  = card.querySelector('.card-state');
                if (stateEl) stateEl.textContent = isOn ? '[ ' + offLabel + ' ]' : '[ ' + onLabel + ' ]';
                beep(card, isOn ? 'off' : 'on');
            });
        });

        // Button flash + optional dialog
        container.querySelectorAll('[data-flash]').forEach(function (el) {
            el.addEventListener('click', function () {
                if (typeof flashOutline === 'function') flashOutline(el);
                // Skip generic beep for AHI-managed buttons — AHI fires its
                // own beep in the capturing phase before emitting the event,
                // so event handlers can override it without nDynamic clobbering.
                if (!el.dataset.ahiId) beep(el, 'action');
                // If button carries a dialog definition, open it
                if (el.dataset.dialog) {
                    try {
                        var opts = JSON.parse(el.dataset.dialog);
                        showDialog(opts);
                    } catch (_) {}
                }
            });
        });

        // Slider init
        // Clean up any prior document-level slider listeners to avoid accumulation
        if (_docSliderListeners) {
            document.removeEventListener('mousemove', _docSliderListeners.onMove);
            document.removeEventListener('touchmove', _docSliderListeners.onTouchMove);
            document.removeEventListener('mouseup',   _docSliderListeners.onUp);
            document.removeEventListener('touchend',  _docSliderListeners.onUp);
            _docSliderListeners = null;
        }

        var _activeSliderDrag = null; // { el, max, readout, lastSoundedValue }

        function sliderInteraction(e) {
            if (!_activeSliderDrag) return;
            var d   = _activeSliderDrag;
            var rect = d.el.getBoundingClientRect();
            var x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
            var segW = rect.width / d.max;
            var idx = Math.max(0, Math.min(d.max - 1, Math.floor(x / segW)));
            var newVal = idx + 1;
            d.el.dataset.value = newVal;
            d.el.querySelectorAll('.seg').forEach(function (s, si) {
                s.classList.toggle('filled', si < newVal);
            });
            if (d.readout) d.readout.textContent = '[ ' + Math.round((newVal / d.max) * 100) + '% ]';
            if (newVal !== d.lastSoundedValue) {
                d.lastSoundedValue = newVal;
                var pct = newVal / d.max;
                var pitchMultiplier = Math.pow(2, (pct - 0.5) * 2);
                beep(d.el, 'adjust', false, pitchMultiplier);
            }
        }

        _docSliderListeners = {
            onMove:      function (e) { if (_activeSliderDrag) sliderInteraction(e); },
            onTouchMove: function (e) { if (_activeSliderDrag) sliderInteraction(e); },
            onUp:        function ()  { _activeSliderDrag = null; }
        };
        document.addEventListener('mousemove', _docSliderListeners.onMove);
        document.addEventListener('touchmove', _docSliderListeners.onTouchMove, { passive: true });
        document.addEventListener('mouseup',   _docSliderListeners.onUp);
        document.addEventListener('touchend',  _docSliderListeners.onUp);

        container.querySelectorAll('.seg-slider').forEach(function (el) {
            var max   = parseInt(el.dataset.max) || 10;
            var value = parseInt(el.dataset.value) || 0;
            var readout = el.parentNode ? el.parentNode.querySelector('.slider-readout') : null;
            el.innerHTML = '';
            for (var i = 0; i < max; i++) {
                var seg = document.createElement('div');
                seg.className = 'seg' + (i < value ? ' filled' : '');
                seg.dataset.idx = i;
                el.appendChild(seg);
            }
            el.addEventListener('mousedown', function (e) {
                _activeSliderDrag = { el: el, max: max, readout: readout, lastSoundedValue: -1 };
                sliderInteraction(e);
            });
            el.addEventListener('touchstart', function (e) {
                _activeSliderDrag = { el: el, max: max, readout: readout, lastSoundedValue: -1 };
                sliderInteraction(e);
            }, { passive: true });
        });

        // Bar init (read-only, with animations matching main demo)
        container.querySelectorAll('.seg-bar').forEach(function (el) {
            initSegBar(el);
        });

        // Toggle groups
        container.querySelectorAll('.toggle-group').forEach(function (group) {
            group.querySelectorAll('.tg-option').forEach(function (opt) {
                opt.addEventListener('click', function () {
                    if (opt.classList.contains('active')) return;
                    if (typeof flashOutline === 'function' && !flashOutline(opt)) return;
                    var cur = group.querySelector('.tg-option.active');
                    if (cur) cur.classList.remove('active');
                    opt.classList.add('active');
                    beep(opt, 'select');
                });
            });
        });

        // Steppers
        container.querySelectorAll('.stepper').forEach(function (el) {
            var valEl = el.querySelector('.stepper-val');
            var val = parseInt(valEl.textContent) || 0;
            el.querySelectorAll('.stepper-btn').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    if (typeof flashOutline === 'function' && !flashOutline(btn)) return;
                    var dir = parseInt(btn.dataset.dir);
                    val = Math.max(0, val + dir);
                    valEl.textContent = val;
                    beep(btn, dir > 0 ? 'increment' : 'decrement');
                });
            });
        });

        // ── Creative widget animations ──

        // Wave canvases
        container.querySelectorAll('.wave-canvas').forEach(function (cv) {
            animateWave(cv);
        });

        // Spark canvases
        container.querySelectorAll('.spark-canvas').forEach(function (cv) {
            animateSpark(cv);
        });

        // Scope canvases
        container.querySelectorAll('.scope-canvas').forEach(function (cv) {
            animateScope(cv);
        });
    }

    /* ═══════════════════════════════════
       Build / Rebuild
       ═══════════════════════════════════ */

    function build() {
        if (!_container || !_controls.length) return;

        var containerRect = _container.getBoundingClientRect();
        var containerW = containerRect.width;

        // Available height: prefer the container's own height (set by flex layout),
        // fall back to viewport-based calculation if container has no definite height yet.
        var viewH  = window.innerHeight;
        var availH = containerRect.height > 50
            ? containerRect.height
            : viewH - containerRect.top - (_config.padding || 16);
        if (availH < 200) availH = 200;

        var dims = computeGrid(containerW, availH, _controls, _config);

        // Apply grid styles
        _container.style.display = 'grid';
        _container.style.gridTemplateColumns = 'repeat(' + dims.cols + ', 1fr)';
        _container.style.gridAutoRows = dims.rowHeight + 'px';
        _container.style.gridAutoFlow = 'dense';
        _container.style.gap = dims.gap + 'px';
        _container.style.padding = dims.padding + 'px';
        _container.style.boxSizing = 'border-box';
        _container.style.width = '100%';
        _container.style.maxHeight = availH + 'px';
        _container.style.overflow = 'hidden';

        // Clear and re-render
        _container.innerHTML = '';

        var ordered = getOrderedControls(_controls, _config);
        var frag = document.createDocumentFragment();

        for (var i = 0; i < ordered.length; i++) {
            var item = ordered[i];
            var el = renderControl(item, dims.cols);
            if (el) {
                /* Apply explicit grid position for pinned items */
                if (_config.pinned && _config.pinned[item._origIdx] !== undefined) {
                    var pin = _config.pinned[item._origIdx];
                    var pCol = Math.max(0, pin.col || 0);
                    var pRow = Math.max(0, pin.row || 0);
                    /* Clamp to grid bounds */
                    var itemCols = Math.min(item.cols || 1, dims.cols);
                    var itemRows = item.rows || 1;
                    pCol = Math.min(pCol, dims.cols - itemCols);
                    /* Use full shorthand so span is preserved alongside start position */
                    el.style.gridColumn = (pCol + 1) + ' / span ' + itemCols;
                    el.style.gridRow    = (pRow + 1) + ' / span ' + itemRows;
                }
                frag.appendChild(el);
            }
        }

        _container.appendChild(frag);

        // Wire all interactions
        wireInteractions(_container);

        _lastWidth  = containerW;
        _lastHeight = viewH;
    }

    function rebuild() {
        // Debounced rebuild (16ms = one frame)
        if (_debounceId) cancelAnimationFrame(_debounceId);
        _debounceId = requestAnimationFrame(function () {
            _debounceId = null;
            build();
        });
    }

    /* ═══════════════════════════════════
       Public API
       ═══════════════════════════════════ */

    /**
     * nDynamic.init(selector, controls, config?)
     *
     * @param {string|Element} selector  CSS selector or DOM element
     * @param {Array}          controls  Array of control definition objects
     * @param {Object}         config    Optional configuration overrides
     */
    function init(selector, controls, config) {
        // Resolve container
        if (typeof selector === 'string') {
            _container = document.querySelector(selector);
        } else {
            _container = selector;
        }

        if (!_container) {
            console.error('[nDynamic] Container not found:', selector);
            return;
        }

        _controls = controls || [];
        _config   = config || {};

        // Initial build
        build();

        // Watch for resize
        if (_observer) _observer.disconnect();

        if (typeof ResizeObserver !== 'undefined') {
            _observer = new ResizeObserver(function (entries) {
                // Only rebuild if dimensions meaningfully changed (>10px)
                var entry = entries[0];
                if (!entry) return;
                var w = entry.contentRect.width;
                var h = window.innerHeight;
                if (Math.abs(w - _lastWidth) > 10 || Math.abs(h - _lastHeight) > 10) {
                    rebuild();
                }
            });
            _observer.observe(_container);
        }

        // Also watch viewport height changes
        if (_resizeHandler) window.removeEventListener('resize', _resizeHandler);
        _resizeHandler = function () {
            var h = window.innerHeight;
            if (Math.abs(h - _lastHeight) > 10) {
                rebuild();
            }
        };
        window.addEventListener('resize', _resizeHandler);
    }

    /**
     * nDynamic.destroy()
     * Tear down the dynamic grid and observers.
     */
    function destroy() {
        if (_observer) {
            _observer.disconnect();
            _observer = null;
        }
        if (_resizeHandler) {
            window.removeEventListener('resize', _resizeHandler);
            _resizeHandler = null;
        }
        if (_docSliderListeners) {
            document.removeEventListener('mousemove', _docSliderListeners.onMove);
            document.removeEventListener('touchmove', _docSliderListeners.onTouchMove);
            document.removeEventListener('mouseup',   _docSliderListeners.onUp);
            document.removeEventListener('touchend',  _docSliderListeners.onUp);
            _docSliderListeners = null;
        }
        if (_container) {
            _container.innerHTML = '';
            _container.removeAttribute('style');
        }
        _controls = [];
        _config   = {};
    }

    /**
     * nDynamic.update(controls?, config?)
     * Update controls and/or config and rebuild.
     */
    function update(controls, config) {
        if (controls) _controls = controls;
        if (config) {
            for (var k in config) _config[k] = config[k];
        }
        rebuild();
    }

    // Expose public API
    return {
        init:               init,
        rebuild:            rebuild,
        destroy:            destroy,
        update:             update,
        showDialog:         showDialog,
        closeDialog:        closeDialog,
        updateSegBar:       updateSegBar,
        buildMatrixPattern: buildMatrixPattern
    };

})();
