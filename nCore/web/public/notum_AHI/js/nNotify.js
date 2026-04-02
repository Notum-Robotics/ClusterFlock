/* ──────────────────────────────────────────────────────────────
   nNotify — In-Grid Notification System
   Copyright © 2026 Notum Robotics (n-r.hr). Licensed under the MIT License.

   Non-interactive, non-dialog notifications that display over
   the top row of a dynamic grid layout.  The top-row elements
   are visually grayed out with a dithered overlay and a compact
   notification box is drawn inside.

   FEATURES:
     • Rate-limited — max once every 30 seconds by default
     • Queued — notifications arriving during cooldown are queued
     • Auto-dismiss after configurable linger time
     • Instant dismiss on tap/click
     • Corner flashers, icon, title, subtitle
     • Audio feedback via nbeep (if available)
     • Works with nDynamic, nInteractive, or any CSS Grid container

   USAGE:
     nNotify.show({
       icon:     'ph-bell',           // Phosphor icon class
       title:    'SYSTEM UPDATE',     // Title text (uppercase recommended)
       subtitle: 'Firmware v2.1 installed successfully',
       linger:   5000                 // Auto-dismiss after 5s (default: 4000)
     });

     nNotify.init('#my-container');   // Bind to a specific grid container
     nNotify.dismiss();               // Programmatic dismiss
     nNotify.destroy();               // Tear down and clear queue

   GLOBAL: window.nNotify
   DEPENDENCY: nUtils.js (for escHtml). nbeep.js optional (audio feedback).
   ────────────────────────────────────────────────────────────── */

var nNotify = (function () {
    'use strict';

    /* ═══════════════════════════════════
       Configuration
       ═══════════════════════════════════ */

    var RATE_LIMIT_MS     = 30000;   // Minimum gap between notifications
    var DEFAULT_LINGER_MS = 4000;    // Default auto-dismiss time
    var FADE_MS           = 250;     // Fade-in / fade-out duration
    var STROBE_ON_MS      = 50;      // Corner flasher on/off timing
    var STROBE_BURSTS     = 4;       // Number of flash bursts per cycle
    var STROBE_PAUSE_MS   = 300;     // Pause between strobe cycles

    /* ═══════════════════════════════════
       Internal State
       ═══════════════════════════════════ */

    var _container       = null;   // The grid container to overlay
    var _queue           = [];     // Pending notifications
    var _lastShowTime    = 0;      // Timestamp of last notification shown
    var _active          = null;   // Currently visible notification element
    var _lingerTimer     = null;   // Auto-dismiss timer
    var _strobeTimer     = null;   // Corner flash timer
    var _cooldownTimer   = null;   // Rate-limit drain timer
    var _initialized     = false;

    /* ═══════════════════════════════════
       Helpers (delegated to nUtils)
       ═══════════════════════════════════ */

    var escHtml = nUtils.escHtml;

    /**
     * Find the bounding rect that covers all top-row grid children.
     * "Top row" = all children whose grid-row-start is 1 (the first row),
     * OR whose top offset matches the first child's top offset.
     */
    function getTopRowRect() {
        if (!_container || !_container.children.length) return null;

        var containerRect = _container.getBoundingClientRect();
        var children = _container.children;
        var gap = parseInt(getComputedStyle(_container).gap) || 6;

        // Find the topmost child's top position
        var firstTop = Infinity;
        for (var i = 0; i < children.length; i++) {
            var child = children[i];
            // Skip notification overlay itself
            if (child.classList.contains('nn-overlay')) continue;
            var r = child.getBoundingClientRect();
            if (r.top < firstTop) firstTop = r.top;
        }
        if (firstTop === Infinity) return null;

        // Collect all children in the same top row (within 4px tolerance)
        var topRowChildren = [];
        var rowBottom = 0;
        for (var j = 0; j < children.length; j++) {
            var ch = children[j];
            if (ch.classList.contains('nn-overlay')) continue;
            var cr = ch.getBoundingClientRect();
            if (Math.abs(cr.top - firstTop) < 4) {
                topRowChildren.push(ch);
                if (cr.bottom > rowBottom) rowBottom = cr.bottom;
            }
        }
        if (!topRowChildren.length) return null;

        // Compute the bounding rect spanning the entire top row
        var left   = containerRect.right;
        var right  = containerRect.left;
        for (var k = 0; k < topRowChildren.length; k++) {
            var tr = topRowChildren[k].getBoundingClientRect();
            if (tr.left < left)  left  = tr.left;
            if (tr.right > right) right = tr.right;
        }

        return {
            top:      firstTop - containerRect.top,
            left:     left - containerRect.left,
            width:    right - left,
            height:   rowBottom - firstTop,
            children: topRowChildren
        };
    }

    /* ═══════════════════════════════════
       Corner Strobe Animation
       ═══════════════════════════════════ */

    function startStrobe(box) {
        if (_strobeTimer) { clearTimeout(_strobeTimer); _strobeTimer = null; }
        var corners = box.querySelectorAll('.corner');
        if (!corners.length) return;

        var flash = 0;
        var on = false;

        function tick() {
            if (!_active) {
                corners.forEach(function (c) { c.classList.remove('flash-outline'); });
                _strobeTimer = null;
                return;
            }
            if (flash < STROBE_BURSTS * 2) {
                on = !on;
                corners.forEach(function (c) {
                    if (on) c.classList.add('flash-outline');
                    else    c.classList.remove('flash-outline');
                });
                flash++;
                _strobeTimer = setTimeout(tick, STROBE_ON_MS);
            } else {
                corners.forEach(function (c) { c.classList.remove('flash-outline'); });
                _strobeTimer = setTimeout(function () {
                    flash = 0;
                    tick();
                }, STROBE_PAUSE_MS);
            }
        }

        _strobeTimer = setTimeout(tick, STROBE_ON_MS);
    }

    /* ═══════════════════════════════════
       Display Logic
       ═══════════════════════════════════ */

    function renderNotification(opts) {
        if (_active) dismiss(true);  // Clear any lingering notification

        var rect = getTopRowRect();
        if (!rect) {
            console.warn('[nNotify] No top-row elements found in container');
            return;
        }

        /* Grayout overlay spanning the top row */
        var overlay = document.createElement('div');
        overlay.className = 'nn-overlay';
        overlay.style.cssText =
            'position:absolute;' +
            'top:' + rect.top + 'px;' +
            'left:' + rect.left + 'px;' +
            'width:' + rect.width + 'px;' +
            'height:' + rect.height + 'px;' +
            'z-index:900;' +
            'pointer-events:auto;' +
            'cursor:pointer;' +
            'display:flex;align-items:center;justify-content:center;' +
            'opacity:0;transition:opacity ' + FADE_MS + 'ms ease;';

        /* Dithered grayout background (same pattern as dialog overlay) */
        var grayBg = document.createElement('div');
        grayBg.className = 'nn-grayout';
        grayBg.style.cssText =
            'position:absolute;inset:0;border-radius:var(--radius,2px);' +
            'background-color:rgba(0,0,0,0.60);' +
            'background-image:url("data:image/svg+xml,' +
            encodeURIComponent('<svg width="4" height="4" xmlns="http://www.w3.org/2000/svg"><rect x="0" y="0" width="2" height="2" fill="#000" fill-opacity="0.50"/><rect x="2" y="2" width="2" height="2" fill="#000" fill-opacity="0.50"/></svg>') +
            '");' +
            'background-size:4px 4px;' +
            'image-rendering:pixelated;';
        overlay.appendChild(grayBg);

        /* Notification box */
        var box = document.createElement('div');
        box.className = 'nn-box';

        var iconHtml = opts.icon
            ? '<div class="nn-icon"><i class="ph ' + escHtml(opts.icon) + '"></i></div>'
            : '';

        box.innerHTML =
            '<span class="corner corner-tl">\u231C</span>' +
            '<span class="corner corner-tr">\u231D</span>' +
            '<span class="corner corner-bl">\u231E</span>' +
            '<span class="corner corner-br">\u231F</span>' +
            iconHtml +
            '<div class="nn-content">' +
                (opts.title    ? '<div class="nn-title">' + escHtml(opts.title) + '</div>' : '') +
                (opts.subtitle ? '<div class="nn-subtitle">' + escHtml(opts.subtitle) + '</div>' : '') +
            '</div>';

        overlay.appendChild(box);

        /* Ensure container has position for absolute overlay */
        var pos = getComputedStyle(_container).position;
        if (pos === 'static') _container.style.position = 'relative';

        /* Insert overlay as first child so it doesn't shift grid layout */
        _container.insertBefore(overlay, _container.firstChild);
        _active = overlay;

        /* Fade in */
        requestAnimationFrame(function () {
            requestAnimationFrame(function () {
                overlay.style.opacity = '1';
            });
        });

        /* Start corner strobe */
        startStrobe(box);

        /* Audio feedback */
        if (typeof nbeep === 'function') {
            nbeep('notify_' + (opts.title || 'alert'));
        }

        /* Dismiss on tap/click */
        overlay.addEventListener('click', function () {
            dismiss();
        });
        overlay.addEventListener('touchend', function (e) {
            e.preventDefault();
            dismiss();
        });

        /* Auto-dismiss after linger time */
        var linger = opts.linger || DEFAULT_LINGER_MS;
        _lingerTimer = setTimeout(function () {
            dismiss();
        }, linger);

        _lastShowTime = Date.now();
    }

    /**
     * Dismiss the active notification.
     * @param {boolean} immediate  Skip fade-out (for replacing)
     */
    function dismiss(immediate) {
        if (_lingerTimer)  { clearTimeout(_lingerTimer);  _lingerTimer = null; }
        if (_strobeTimer)  { clearTimeout(_strobeTimer);  _strobeTimer = null; }

        if (!_active) return;

        var el = _active;
        _active = null;

        /* Stop the looping beep if active */
        if (typeof nDesignAudio === 'object' && nDesignAudio.killActive) {
            nDesignAudio.killActive();
        }

        if (immediate) {
            if (el.parentNode) el.parentNode.removeChild(el);
            drainQueue();
        } else {
            el.style.opacity = '0';
            el.style.pointerEvents = 'none';
            setTimeout(function () {
                if (el.parentNode) el.parentNode.removeChild(el);
                drainQueue();
            }, FADE_MS);
        }
    }

    /* ═══════════════════════════════════
       Queue & Rate Limiting
       ═══════════════════════════════════ */

    function drainQueue() {
        if (_cooldownTimer) { clearTimeout(_cooldownTimer); _cooldownTimer = null; }
        if (!_queue.length) return;

        var elapsed = Date.now() - _lastShowTime;
        if (elapsed >= RATE_LIMIT_MS) {
            renderNotification(_queue.shift());
        } else {
            var wait = RATE_LIMIT_MS - elapsed;
            _cooldownTimer = setTimeout(function () {
                _cooldownTimer = null;
                if (_queue.length) {
                    renderNotification(_queue.shift());
                }
            }, wait);
        }
    }

    /* ═══════════════════════════════════
       Public API
       ═══════════════════════════════════ */

    /**
     * nNotify.init(selectorOrElement)
     *
     * Bind the notification system to a grid container.
     * Must be called before show(). If not called,
     * show() tries to auto-detect the container.
     *
     * @param {string|Element} selector  CSS selector or DOM element
     */
    function init(selector) {
        if (typeof selector === 'string') {
            _container = document.querySelector(selector);
        } else if (selector && selector.nodeType) {
            _container = selector;
        }
        _initialized = !!_container;
    }

    /**
     * nNotify.show(opts)
     *
     * Queue a notification for display. Rate-limited to one
     * notification per 30 seconds (configurable).
     *
     * @param {Object} opts
     *   icon     {string}  Phosphor icon class (e.g. 'ph-bell')
     *   title    {string}  Notification title (uppercase recommended)
     *   subtitle {string}  Secondary text line
     *   linger   {number}  Auto-dismiss delay in ms (default: 4000)
     */
    function show(opts) {
        if (!opts) return;

        /* Auto-detect container if not initialized */
        if (!_container) {
            _container = document.querySelector('#ahi-container') ||
                         document.querySelector('#interactive-container') ||
                         document.querySelector('#dynamic-container') ||
                         document.querySelector('#auto-grid') ||
                         document.querySelector('[style*="display: grid"], [style*="display:grid"]');
            _initialized = !!_container;
        }
        if (!_container) {
            console.warn('[nNotify] No grid container found');
            return;
        }

        /* If a notification is active or within cooldown, queue it */
        var elapsed = Date.now() - _lastShowTime;
        if (_active || elapsed < RATE_LIMIT_MS) {
            _queue.push(opts);
            if (!_active && !_cooldownTimer) drainQueue();
            return;
        }

        renderNotification(opts);
    }

    /**
     * nNotify.config(opts)
     *
     * Override default timing configuration.
     *
     * @param {Object} opts
     *   rateLimit  {number}  Minimum ms between notifications (default: 30000)
     *   linger     {number}  Default linger time in ms (default: 4000)
     *   fade       {number}  Fade transition duration in ms (default: 250)
     */
    function config(opts) {
        if (!opts) return;
        if (opts.rateLimit !== undefined) RATE_LIMIT_MS     = opts.rateLimit;
        if (opts.linger !== undefined)    DEFAULT_LINGER_MS = opts.linger;
        if (opts.fade !== undefined)      FADE_MS           = opts.fade;
    }

    /**
     * nNotify.destroy()
     *
     * Tear down: dismiss any active notification and clear the queue.
     */
    function destroy() {
        dismiss(true);
        _queue = [];
        if (_cooldownTimer) { clearTimeout(_cooldownTimer); _cooldownTimer = null; }
        _container   = null;
        _initialized = false;
        _lastShowTime = 0;
    }

    /**
     * nNotify.isActive()
     * @returns {boolean} True if a notification is currently visible
     */
    function isActive() {
        return !!_active;
    }

    /**
     * nNotify.queueLength()
     * @returns {number} Number of notifications waiting in the queue
     */
    function queueLength() {
        return _queue.length;
    }

    /* ═══════════════════════════════════
       Expose Public API
       ═══════════════════════════════════ */

    return {
        init:        init,
        show:        show,
        dismiss:     dismiss,
        destroy:     destroy,
        config:      config,
        isActive:    isActive,
        queueLength: queueLength
    };

})();

/* ── Global export ────────────────────────────────────────── */
window.nNotify = nNotify;
