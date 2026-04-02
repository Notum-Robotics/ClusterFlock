/* ──────────────────────────────────────────────────────────────
   nComp — Component Properties System
   Copyright © 2026 Notum Robotics. Licensed under the MIT License.

   Uniform API for attaching progress bars, status pips, and
   active-state indicators to any DOM element.  Used by the
   main component demo, AHI framework, and any page that
   needs runtime state decoration on controls.

   DEPENDENCY: None required (self-contained).
   OPTIONAL:   nUtils.js (for consistent helper availability).

   USAGE:
     nComp.progress(el, 42);      // 0-100: segmented bar
     nComp.progress(el, -1);      // indeterminate scan
     nComp.progress(el, null);    // remove
     nComp.status(el, 'ok');      // ok | warn | error | busy | null
     nComp.active(el, true);      // data-active="true" | "false" | remove

   GLOBAL: window.nComp
   ────────────────────────────────────────────────────────────── */

var nComp = (function () {
    'use strict';

    var N_PROGRESS_SEGS = 10;

    /* ═══════════════════════════════════
       Internal Helpers
       ═══════════════════════════════════ */

    function clearProgressTimers(el) {
        if (el._nProgTimer)   { clearInterval(el._nProgTimer);  el._nProgTimer = null; }
        if (el._nProgTimeout) { clearTimeout(el._nProgTimeout); el._nProgTimeout = null; }
    }

    function ensureProgressBar(el) {
        var bar = el.querySelector('.n-progress');
        if (bar) return bar;
        bar = document.createElement('div');
        bar.className = 'n-progress';
        for (var i = 0; i < N_PROGRESS_SEGS; i++) {
            var seg = document.createElement('div');
            seg.className = 'n-seg';
            bar.appendChild(seg);
        }
        if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
        el.appendChild(bar);
        return bar;
    }

    function ensureStatusPip(el) {
        var pip = el.querySelector('.n-status-pip');
        if (pip) return pip;
        pip = document.createElement('span');
        pip.className = 'n-status-pip';
        if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
        el.appendChild(pip);
        return pip;
    }

    /* ═══════════════════════════════════
       Progress Animations
       ═══════════════════════════════════ */

    /** Travel reads el._nProgFilled dynamically so it survives incremental updates.
        Direction (el._nProgDir): 1 = forward (left→right), -1 = reverse (right→left). */
    function nProgressTravel(el) {
        var dir = el._nProgDir || 1;
        var segs = el._nProgFilled;
        var pos = dir === 1 ? 0 : (segs ? segs.length - 1 : 0);
        if (segs && segs.length > 0) segs[pos].classList.add('seg-dim');
        el._nProgTimer = setInterval(function () {
            var segs = el._nProgFilled;
            if (!segs || segs.length < 2) return;
            var d = el._nProgDir || 1;
            if (pos >= 0 && pos < segs.length) segs[pos].classList.remove('seg-dim');
            pos = (pos + d + segs.length) % segs.length;
            segs[pos].classList.add('seg-dim');
        }, 120);
    }

    /** 2 flashes (50 ms on / 50 ms off) then 350 ms pause, repeating. */
    function nProgressBlink(el, segs) {
        var step = 0;
        function tick() {
            if (step < 4) {
                var on = (step % 2 === 0);
                for (var i = 0; i < segs.length; i++) {
                    if (on) segs[i].classList.add('seg-bright');
                    else    segs[i].classList.remove('seg-bright');
                }
                step++;
                el._nProgTimeout = setTimeout(tick, 50);
            } else {
                step = 0;
                el._nProgTimeout = setTimeout(tick, 350);
            }
        }
        tick();
    }

    /** Indeterminate scan: one highlighted segment sweeps across all slots. */
    function nProgressScan(el, bar) {
        var segs = bar.querySelectorAll('.n-seg');
        var dir = el._nProgDir || 1;
        var pos = dir === 1 ? 0 : segs.length - 1;
        segs[pos].classList.add('seg-scan');
        el._nProgTimer = setInterval(function () {
            var d = el._nProgDir || 1;
            segs[pos].classList.remove('seg-scan');
            pos = (pos + d + segs.length) % segs.length;
            segs[pos].classList.add('seg-scan');
        }, 100);
    }

    /* ═══════════════════════════════════
       Public API
       ═══════════════════════════════════ */

    var api = {
        /** Set progress: 0-100, -1 for indeterminate, null to remove */
        progress: function (el, value) {
            if (!el) return;
            if (value === null || value === undefined) {
                clearProgressTimers(el);
                var bar = el.querySelector('.n-progress');
                if (bar) bar.remove();
                el.removeAttribute('data-progress');
                el.style.removeProperty('--prog-filled');
                el._nProgState = null;
                el._nProgFilled = null;
                el._nProgDir = 1;
                el._nProgPrev = null;
                return;
            }
            var bar = ensureProgressBar(el);
            var prevState = el._nProgState || null;

            if (value === -1) {
                if (prevState !== 'scan') {
                    clearProgressTimers(el);
                    bar.querySelectorAll('.n-seg').forEach(function (s) {
                        s.classList.remove('filled', 'seg-dim', 'seg-bright', 'seg-scan');
                    });
                    el.setAttribute('data-progress', 'indeterminate');
                    el.style.removeProperty('--prog-filled');
                    el._nProgState = 'scan';
                    el._nProgFilled = null;
                    nProgressScan(el, bar);
                }
                return;
            }

            value = Math.max(0, Math.min(100, value));

            /* Determine animation direction from value delta */
            var prev = el._nProgPrev;
            if (prev !== null && prev !== undefined && value !== prev) {
                el._nProgDir = value > prev ? 1 : -1;
            } else if (el._nProgDir === undefined) {
                el._nProgDir = 1;
            }
            el._nProgPrev = value;

            el.setAttribute('data-progress', Math.round(value));
            var segs = bar.querySelectorAll('.n-seg');
            var filled = Math.round((value / 100) * N_PROGRESS_SEGS);
            el.style.setProperty('--prog-filled', filled);

            if (filled >= N_PROGRESS_SEGS) {
                /* Full — switch to blink */
                if (prevState !== 'full') {
                    clearProgressTimers(el);
                    segs.forEach(function (s) {
                        s.classList.remove('seg-dim', 'seg-bright', 'seg-scan');
                        s.classList.add('filled');
                    });
                    el._nProgState = 'full';
                    el._nProgFilled = null;
                    var allSegs = [];
                    segs.forEach(function (s) { allSegs.push(s); });
                    nProgressBlink(el, allSegs);
                }
            } else {
                /* Partial — update fills, keep travel alive */
                var filledArr = [];
                segs.forEach(function (s, i) {
                    if (i < filled) {
                        s.classList.add('filled');
                        filledArr.push(s);
                    } else {
                        s.classList.remove('filled', 'seg-dim');
                    }
                });
                el._nProgFilled = filledArr;

                if (prevState !== 'partial') {
                    /* Starting fresh — clear any previous animation and launch travel */
                    clearProgressTimers(el);
                    segs.forEach(function (s) { s.classList.remove('seg-bright', 'seg-scan'); });
                    el._nProgState = 'partial';
                    if (filledArr.length > 1) {
                        nProgressTravel(el);
                    }
                } else if (!el._nProgTimer && filledArr.length > 1) {
                    /* Travel was never started (had <2 segs initially), start now */
                    nProgressTravel(el);
                }
                /* else: travel is already running and reads el._nProgFilled dynamically */
            }
        },

        /** Set status: 'ok' | 'warn' | 'error' | 'busy' | null */
        status: function (el, status) {
            if (!el) return;
            if (!status) {
                el.removeAttribute('data-status');
                var pip = el.querySelector('.n-status-pip');
                if (pip) pip.remove();
                return;
            }
            el.setAttribute('data-status', status);
            ensureStatusPip(el);
        },

        /** Set active state: true | false | null to remove */
        active: function (el, active) {
            if (!el) return;
            if (active === null || active === undefined) {
                el.removeAttribute('data-active');
            } else {
                el.setAttribute('data-active', active ? 'true' : 'false');
            }
        }
    };

    window.nComp = api;
    return api;

})();
