/* ──────────────────────────────────────────────────────────────
   nUtils — Shared Utilities for Notum AHI
   Copyright © 2026 Notum Robotics. Licensed under the MIT License.

   Provides common helper functions used across all framework
   modules: HTML escaping, grid corner ornaments, flash-outline
   with lockout support, and audio context pre-warming.

   Load this script AFTER nbeep.js (if audio is desired) and
   BEFORE nDynamic.js, nInteractive.js, nNotify.js, nComp.js,
   or any page-level script.

   GLOBAL: window.nUtils, window.flashOutline
   ────────────────────────────────────────────────────────────── */

var nUtils = (function () {
    'use strict';

    /* ═══════════════════════════════════
       escHtml — Escape HTML entities
       ═══════════════════════════════════ */

    function escHtml(s) {
        var d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    /* ═══════════════════════════════════
       gridCorners — Corner flash ornaments
       Standard four-corner bracket markup used by
       cards, grid cells, and dialog boxes.
       ═══════════════════════════════════ */

    function gridCorners() {
        return '<span class="corner corner-tl">\u231C</span>' +
               '<span class="corner corner-tr">\u231D</span>' +
               '<span class="corner corner-bl">\u231E</span>' +
               '<span class="corner corner-br">\u231F</span>';
    }

    /* ═══════════════════════════════════
       flashOutline — Lockout-aware flash
       Strobes `.flash-outline` on an element with
       optional extended lockout via data-lockout="ms".
       Returns false if the element is already locked.
       ═══════════════════════════════════ */

    var FLASH_DURATION = 200;
    var FLASH_TICK     = 10;
    var flashLocks     = new WeakMap();

    function flashOutline(el, onDone) {
        if (!el || flashLocks.has(el)) return false;
        flashLocks.set(el, true);

        var lockoutMs = parseInt(el.dataset.lockout) || 0;
        var totalMs = Math.max(FLASH_DURATION, lockoutMs);
        var extendedLockout = lockoutMs > FLASH_DURATION;
        if (extendedLockout) el.classList.add('n-locked');

        var tick = (lockoutMs >= 400) ? 150 : FLASH_TICK;
        var elapsed = 0;
        var on = true;
        el.classList.add('flash-outline');

        var iv = setInterval(function () {
            elapsed += tick;
            on = !on;
            if (on) el.classList.add('flash-outline');
            else    el.classList.remove('flash-outline');

            if (elapsed >= totalMs) {
                clearInterval(iv);
                el.classList.remove('flash-outline');
                if (extendedLockout) el.classList.remove('n-locked');
                flashLocks.delete(el);
                if (typeof onDone === 'function') onDone();
            }
        }, tick);
        return true;
    }

    /* ═══════════════════════════════════
       Audio Pre-warm
       Resumes the AudioContext on the first user
       gesture so subsequent nbeep() calls play
       without the ~50-150ms suspend→resume lag.
       ═══════════════════════════════════ */

    if (typeof nDesignAudio === 'object' && nDesignAudio.warmUp) {
        function preWarm() { nDesignAudio.warmUp(); }
        document.addEventListener('mousedown',  preWarm, { passive: true });
        document.addEventListener('touchstart', preWarm, { passive: true });
    }

    /* ═══════════════════════════════════
       Expose
       ═══════════════════════════════════ */

    window.flashOutline = flashOutline;

    return {
        escHtml:        escHtml,
        gridCorners:    gridCorners,
        flashOutline:   flashOutline,
        FLASH_DURATION: FLASH_DURATION
    };

})();
