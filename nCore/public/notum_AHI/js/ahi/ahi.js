/* ──────────────────────────────────────────────────────────────
   Notum AHI — Automatic Human Interface for Autonomous Agents
   Copyright © 2026 Notum Robotics (n-r.hr). Licensed under the MIT License.

   A declarative, JSON-driven interface layer that enables coding
   agents, tool-calling LLMs, and AI systems to build rich UIs
   on the fly for human operators.

   Wraps the Notum AHI layout engine (nDynamic) with a
   stable, index-free, id-based API designed for machine callers.

   DEPENDENCY: nDynamic.js must be loaded first.
               nbeep.js is optional (audio feedback).

   GLOBAL: window.notumAHI

   QUICK START (agent-side):
     notumAHI.render([
       { id: 'light', type: 'card', cols: 2, rows: 2, state: 'on',
         icon: 'ph-lightbulb', name: 'AMBIENT', on: 'ACTIVE', off: 'INACTIVE' },
       { id: 'vol', type: 'slider', cols: 4, rows: 2,
         label: 'VOLUME', max: 20, value: 14, color: 'accent' }
     ]);
     notumAHI.onEvent(function (evt) { console.log(evt); });
   ────────────────────────────────────────────────────────────── */

var notumAHI = (function () {
    'use strict';

    /* ═══════════════════════════════════
       Version & Constants
       ═══════════════════════════════════ */

    var VERSION = '1.0.0';
    var TOAST_DURATION = 3000;
    var TOAST_FADE     = 300;

    /* ═══════════════════════════════════
       Internal State
       ═══════════════════════════════════ */

    var _container    = null;   // DOM element
    var _controls     = [];     // current control definitions (with AHI extensions)
    var _config       = {};     // nDynamic layout config
    var _listeners    = [];     // onEvent callbacks
    var _initialized  = false;
    var _toastStack   = [];     // active toast elements
    var _layoutReady  = false;  // true after first layout engine init

    /* Id → index lookup cache (rebuilt on every render) */
    var _idMap = {};

    /* ═══════════════════════════════════
       Helpers
       ═══════════════════════════════════ */

    function deepClone(obj) {
        return JSON.parse(JSON.stringify(obj));
    }

    function rebuildIdMap() {
        _idMap = {};
        for (var i = 0; i < _controls.length; i++) {
            if (_controls[i].id) {
                _idMap[_controls[i].id] = i;
            }
        }
    }

    /** Resolve an id or numeric index to an array index */
    function resolveIndex(idOrIndex) {
        if (typeof idOrIndex === 'number') return idOrIndex;
        if (typeof idOrIndex === 'string' && _idMap[idOrIndex] !== undefined) {
            return _idMap[idOrIndex];
        }
        return -1;
    }

    /** Fire an event to all registered listeners */
    function emit(evt) {
        evt.timestamp = Date.now();
        evt._ahi = VERSION;
        for (var i = 0; i < _listeners.length; i++) {
            try { _listeners[i](evt); } catch (e) {
                console.error('[notumAHI] Event listener error:', e);
            }
        }
    }

    /** Get the nDynamic-rendered DOM element for a control index */
    function getControlEl(idx) {
        if (!_container) return null;
        /* Walk children, skipping non-control elements (e.g. nNotify overlay) */
        var children = _container.children;
        var ci = 0; // control index counter
        for (var i = 0; i < children.length; i++) {
            if (children[i].classList.contains('nn-overlay')) continue;
            if (ci === idx) return children[i];
            ci++;
        }
        return null;
    }

    /* ═══════════════════════════════════
       Event Wiring
       Intercepts user interactions on the
       rendered grid and emits structured events.
       ═══════════════════════════════════ */

    var _delegatedHandler = null;
    var _mutationObserver = null;

    function wireEvents() {
        if (_delegatedHandler) {
            _container.removeEventListener('click', _delegatedHandler, true);
        }

        _delegatedHandler = function (e) {
            var target = e.target;

            /* ── Card toggle ── */
            var card = target.closest && target.closest('.nd-card');
            if (card) {
                var cardIdx = findControlIndex(card);
                if (cardIdx >= 0) {
                    var ctrl = _controls[cardIdx];
                    /* Check for confirm dialog */
                    if (ctrl.confirm) {
                        e.stopImmediatePropagation();
                        e.preventDefault();
                        notumAHI.dialog(ctrl.confirm).then(function (val) {
                            if (val && val !== 'no' && val !== 'cancel' && val !== null) {
                                var newState = card.classList.contains('is-on') ? 'off' : 'on';
                                ctrl.state = newState;
                                emit({
                                    event: ctrl.onEvent || 'toggle',
                                    id: ctrl.id || null,
                                    index: cardIdx,
                                    control: ctrl.name || ctrl.label || ctrl.type,
                                    value: newState,
                                    dialogResponse: val
                                });
                            }
                        });
                        return;
                    }
                    /* Wait for flash to complete, then read new state */
                    setTimeout(function () {
                        var newState = card.classList.contains('is-on') ? 'on' : 'off';
                        ctrl.state = newState;
                        emit({
                            event: ctrl.onEvent || 'toggle',
                            id: ctrl.id || null,
                            index: cardIdx,
                            control: ctrl.name || ctrl.label || ctrl.type,
                            value: newState
                        });
                    }, 50);
                }
                return;
            }

            /* ── Button click ── */
            var btn = target.closest && target.closest('.action-btn');
            if (btn) {
                var btnIdx = findControlIndex(btn);
                if (btnIdx >= 0) {
                    var bCtrl = _controls[btnIdx];
                    if (bCtrl.disabled) {
                        e.stopImmediatePropagation();
                        e.preventDefault();
                        return;
                    }
                    /* If button has a confirm guard and no data-dialog (ahi-level confirm) */
                    if (bCtrl.confirm && !btn.dataset.dialog) {
                        e.stopImmediatePropagation();
                        e.preventDefault();
                        notumAHI.dialog(bCtrl.confirm).then(function (val) {
                            if (val && val !== 'no' && val !== 'cancel' && val !== null) {
                                emit({
                                    event: bCtrl.onEvent || 'button_click',
                                    id: bCtrl.id || null,
                                    index: btnIdx,
                                    control: bCtrl.label || bCtrl.type,
                                    value: val,
                                    dialogResponse: val
                                });
                            }
                        });
                        return;
                    }
                    /* Default beep BEFORE emit — event handlers can
                       override with a custom nbeep() call which will
                       killActive() this one and start their own sound. */
                    if (typeof nbeep === 'function') {
                        nbeep(bCtrl.label || bCtrl.id || 'action');
                    }
                    emit({
                        event: bCtrl.onEvent || 'button_click',
                        id: bCtrl.id || null,
                        index: btnIdx,
                        control: bCtrl.label || bCtrl.type,
                        value: bCtrl.label
                    });
                }
                return;
            }

            /* ── Toggle group option ── */
            var tgOpt = target.closest && target.closest('.tg-option');
            if (tgOpt) {
                var tgCell = tgOpt.closest('.gc-toggle');
                if (tgCell) {
                    var tgIdx = findControlIndex(tgCell);
                    if (tgIdx >= 0) {
                        setTimeout(function () {
                            var activeOpt = tgCell.querySelector('.tg-option.active');
                            var val = activeOpt ? activeOpt.dataset.val : null;
                            var tCtrl = _controls[tgIdx];
                            if (tCtrl.options) {
                                tCtrl.active = tCtrl.options.indexOf(val);
                            }
                            emit({
                                event: tCtrl.onEvent || 'toggle_select',
                                id: tCtrl.id || null,
                                index: tgIdx,
                                control: tCtrl.label || tCtrl.type,
                                value: val
                            });
                        }, 50);
                    }
                }
                return;
            }

            /* ── Stepper button ── */
            var stepBtn = target.closest && target.closest('.stepper-btn');
            if (stepBtn) {
                var stepCell = stepBtn.closest('.gc-stepper');
                if (stepCell) {
                    var stepIdx = findControlIndex(stepCell);
                    if (stepIdx >= 0) {
                        setTimeout(function () {
                            var valEl = stepCell.querySelector('.stepper-val');
                            var val = parseInt(valEl ? valEl.textContent : '0') || 0;
                            var sCtrl = _controls[stepIdx];
                            sCtrl.value = val;
                            emit({
                                event: sCtrl.onEvent || 'stepper_change',
                                id: sCtrl.id || null,
                                index: stepIdx,
                                control: sCtrl.label || sCtrl.type,
                                value: val,
                                direction: parseInt(stepBtn.dataset.dir)
                            });
                        }, 50);
                    }
                }
                return;
            }
        };

        _container.addEventListener('click', _delegatedHandler, true);

        /* ── Slider value changes (via MutationObserver) ── */
        if (_mutationObserver) _mutationObserver.disconnect();

        _mutationObserver = new MutationObserver(function (mutations) {
            mutations.forEach(function (m) {
                if (m.type === 'attributes' && m.attributeName === 'data-value') {
                    var slider = m.target;
                    if (!slider.classList.contains('seg-slider')) return;
                    var cell = slider.closest('.gc-slider');
                    if (!cell) return;
                    var slIdx = findControlIndex(cell);
                    if (slIdx >= 0) {
                        var slCtrl = _controls[slIdx];
                        var newVal = parseInt(slider.dataset.value) || 0;
                        slCtrl.value = newVal;
                        emit({
                            event: slCtrl.onEvent || 'slider_change',
                            id: slCtrl.id || null,
                            index: slIdx,
                            control: slCtrl.label || slCtrl.type,
                            value: newVal,
                            max: slCtrl.max || 10,
                            percent: Math.round((newVal / (slCtrl.max || 10)) * 100)
                        });
                    }
                }
            });
        });

        /* Observe slider data-value changes */
        var sliders = _container.querySelectorAll('.seg-slider');
        sliders.forEach(function (sl) {
            _mutationObserver.observe(sl, { attributes: true, attributeFilter: ['data-value'] });
        });
    }

    /** Walk up from an element to find which control index it belongs to */
    function findControlIndex(el) {
        /* If nInteractive tagged elements, use that */
        if (el.dataset && el.dataset.niIdx !== undefined) {
            return parseInt(el.dataset.niIdx, 10);
        }
        /* Otherwise, count siblings */
        var children = _container.children;
        var node = el;
        while (node && node.parentNode !== _container) {
            node = node.parentNode;
        }
        if (!node) return -1;
        for (var i = 0; i < children.length; i++) {
            if (children[i] === node) return i;
        }
        return -1;
    }

    /* ═══════════════════════════════════
       AHI-Extended Control Processing
       Adds disabled, hidden, badge, tooltip,
       confirm behaviour to rendered elements.
       ═══════════════════════════════════ */

    function applyAHIExtensions() {
        if (!_container) return;
        var children = _container.children;
        for (var i = 0; i < _controls.length && i < children.length; i++) {
            var ctrl = _controls[i];
            var el = children[i];

            /* disabled */
            if (ctrl.disabled) {
                el.classList.add('ahi-disabled');
                el.style.pointerEvents = 'none';
            } else {
                el.classList.remove('ahi-disabled');
                el.style.pointerEvents = '';
            }

            /* hidden */
            if (ctrl.hidden) {
                el.classList.add('ahi-hidden');
                el.style.visibility = 'hidden';
                el.style.opacity = '0';
            } else {
                el.classList.remove('ahi-hidden');
                el.style.visibility = '';
                /* Don't reset opacity if disabled */
                if (!ctrl.disabled) el.style.opacity = '';
            }

            /* badge */
            var existingBadge = el.querySelector('.ahi-badge');
            if (ctrl.badge !== undefined && ctrl.badge !== null && ctrl.badge !== '') {
                if (!existingBadge) {
                    existingBadge = document.createElement('span');
                    existingBadge.className = 'ahi-badge';
                    if (getComputedStyle(el).position === 'static') {
                        el.style.position = 'relative';
                    }
                    el.appendChild(existingBadge);
                }
                existingBadge.textContent = String(ctrl.badge);
            } else if (existingBadge) {
                existingBadge.remove();
            }

            /* tooltip */
            if (ctrl.tooltip) {
                el.title = ctrl.tooltip;
                el.dataset.ahiTooltip = ctrl.tooltip;
            } else {
                el.removeAttribute('title');
                delete el.dataset.ahiTooltip;
            }

            /* lockout */
            if (ctrl.lockout) {
                el.dataset.lockout = String(ctrl.lockout);
            }

            /* progress (via nComp if available) */
            if (typeof nComp !== 'undefined' && ctrl.progress !== undefined) {
                nComp.progress(el, ctrl.progress);
            }

            /* status (via nComp if available) */
            if (typeof nComp !== 'undefined' && ctrl.status !== undefined) {
                nComp.status(el, ctrl.status);
            }

            /* data-ahi-id for external references */
            if (ctrl.id) {
                el.dataset.ahiId = ctrl.id;
            }
        }
    }

    /* ═══════════════════════════════════
       Toast Notifications
       ═══════════════════════════════════ */

    function ensureToastContainer() {
        var tc = document.getElementById('ahi-toast-container');
        if (!tc) {
            tc = document.createElement('div');
            tc.id = 'ahi-toast-container';
            tc.style.cssText =
                'position:fixed;top:16px;right:16px;z-index:5000;' +
                'display:flex;flex-direction:column;gap:8px;pointer-events:none;';
            document.body.appendChild(tc);
        }
        return tc;
    }

    /* ═══════════════════════════════════
       Dialog Overlay Bootstrapping
       Ensures the dialog DOM exists even
       if the host page didn't include it.
       ═══════════════════════════════════ */

    function ensureDialogOverlay() {
        if (document.getElementById('dialog-overlay')) return;
        var overlay = document.createElement('div');
        overlay.id = 'dialog-overlay';
        overlay.className = 'dialog-overlay';
        var box = document.createElement('div');
        box.id = 'dialog-box';
        box.className = 'dialog-box';
        overlay.appendChild(box);
        document.body.appendChild(overlay);

        /* Dismiss on overlay click */
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) {
                if (typeof nbeep === 'function') nbeep('dialog_dismiss');
                if (typeof nDynamic !== 'undefined' && nDynamic.closeDialog) {
                    nDynamic.closeDialog(null);
                }
            }
        });
    }

    /* ═══════════════════════════════════
       Public API
       ═══════════════════════════════════ */

    /**
     * notumAHI.init(selectorOrElement, config?)
     *
     * Initialize the AHI framework. Must be called before render().
     * If not called explicitly, render() will auto-init to 'body'
     * or the first .ahi-container element.
     */
    function init(selector, config) {
        if (typeof selector === 'string') {
            _container = document.querySelector(selector);
        } else if (selector && selector.nodeType) {
            _container = selector;
        } else {
            _container = document.querySelector('.ahi-container') || document.body;
        }

        _config = config || {};
        _initialized = true;

        ensureDialogOverlay();

        return notumAHI;
    }

    /**
     * notumAHI.render(controls, config?)
     *
     * Replace the entire UI with a new set of controls.
     * This is the primary method agents use to build interfaces.
     *
     * @param {Array}  controls  Array of control definition objects (see schema)
     * @param {Object} config    Optional layout configuration
     * @returns {Object} notumAHI (for chaining)
     */
    function render(controls, config) {
        if (!_initialized) init();

        _controls = deepClone(controls || []);
        if (config) {
            for (var k in config) _config[k] = config[k];
        }
        rebuildIdMap();

        /* Strip AHI-only fields before passing to nDynamic */
        var nControls = _controls.map(function (c) {
            var nc = {};
            for (var key in c) {
                /* Skip AHI extension fields — nDynamic doesn't know about them */
                if (key === 'id' || key === 'disabled' || key === 'hidden' ||
                    key === 'badge' || key === 'tooltip' || key === 'confirm' ||
                    key === 'onEvent' || key === 'progress' || key === 'status') continue;
                nc[key] = c[key];
            }
            return nc;
        });

        /* Use nInteractive when available for edit-mode support, otherwise nDynamic */
        if (!_layoutReady) {
            /* First render — full init */
            if (typeof nInteractive !== 'undefined') {
                nInteractive.init(_container, nControls, _config);
            } else {
                nDynamic.init(_container, nControls, _config);
            }
            _layoutReady = true;
        } else {
            /* Subsequent renders — update in place to avoid stacking event listeners */
            if (typeof nInteractive !== 'undefined') {
                nInteractive.update(nControls, _config);
            } else {
                nDynamic.update(nControls, _config);
            }
        }

        /* Apply AHI extensions after nDynamic renders */
        setTimeout(function () {
            applyAHIExtensions();
            wireEvents();
        }, 80);

        return notumAHI;
    }

    /**
     * notumAHI.patch(idOrIndex, changes)
     *
     * Update a specific control's properties in place without
     * re-rendering the entire layout. Efficient for live updates.
     *
     * @param {string|number} idOrIndex  Control id string or numeric index
     * @param {Object}        changes    Partial control definition to merge
     * @returns {Object} notumAHI (for chaining)
     */
    function patch(idOrIndex, changes) {
        if (!_layoutReady) {
            console.error('[notumAHI] patch() called before render(). ' +
                'Did you call nInteractive.init() directly? Use notumAHI.render() instead.');
            return notumAHI;
        }
        var idx = resolveIndex(idOrIndex);
        if (idx < 0 || idx >= _controls.length) {
            console.error('[notumAHI] patch: control not found: "' + idOrIndex +
                '". Registered ids: ' + Object.keys(_idMap).join(', '));
            return notumAHI;
        }

        var ctrl = _controls[idx];
        for (var key in changes) {
            ctrl[key] = changes[key];
        }

        /* Determine if we need a full rebuild or can do in-place */
        var needsRebuild = false;
        var structuralKeys = ['type', 'cols', 'rows', 'size', 'icon', 'options', 'max', 'gridSize'];
        for (var sk = 0; sk < structuralKeys.length; sk++) {
            if (changes.hasOwnProperty(structuralKeys[sk])) {
                needsRebuild = true;
                break;
            }
        }

        if (needsRebuild) {
            /* Full rebuild needed for structural changes */
            render(_controls, null);
        } else {
            /* In-place update for non-structural changes */
            var el = getControlEl(idx);
            if (!el) return notumAHI;

            /* value → slider/stepper/bar */
            if (changes.value !== undefined) {
                var slider = el.querySelector('.seg-slider');
                if (slider) {
                    slider.dataset.value = changes.value;
                    /* Re-render slider segments */
                    var max = parseInt(slider.dataset.max) || 10;
                    slider.querySelectorAll('.seg').forEach(function (s, si) {
                        s.classList.toggle('filled', si < changes.value);
                    });
                    var readout = el.querySelector('.slider-readout');
                    if (readout) {
                        readout.textContent = '[ ' + Math.round((changes.value / max) * 100) + '% ]';
                    }
                }

                /* Animated bar update */
                var segBar = el.querySelector('.seg-bar');
                if (segBar) {
                    if (typeof nDynamic !== 'undefined' && nDynamic.updateSegBar) {
                        nDynamic.updateSegBar(segBar, changes.value);
                    } else {
                        /* Fallback: reinit */
                        segBar.dataset.value = changes.value;
                    }
                    /* Update readout */
                    var barMax = parseInt(segBar.dataset.max) || 10;
                    var barReadout = el.querySelector('.slider-readout');
                    if (barReadout) {
                        barReadout.textContent = '[ ' + Math.round((changes.value / barMax) * 100) + '% ]';
                    }
                }

                var stepVal = el.querySelector('.stepper-val');
                if (stepVal) stepVal.textContent = changes.value;

                /* ── Gauge update ── */
                var gaugeFill = el.querySelector('.gauge-fill');
                if (gaugeFill) {
                    var gMax = parseInt(el.dataset.max) || 100;
                    var gPct = Math.round((changes.value / gMax) * 100);
                    var arcLen = parseFloat(gaugeFill.dataset.arc) || (Math.PI * 70);
                    var dashOff = arcLen * (1 - changes.value / gMax);
                    gaugeFill.style.strokeDashoffset = dashOff.toFixed(2);
                    var gReadout = el.querySelector('.slider-readout');
                    if (gReadout) gReadout.textContent = '[ ' + gPct + '% ]';
                    /* Update needle */
                    var needle = el.querySelector('.gauge-needle');
                    if (needle) {
                        var nAng = Math.PI + (Math.PI * changes.value / gMax);
                        var ncx = 100, ncy = 90;
                        needle.setAttribute('x2', (ncx + 54 * Math.cos(nAng)).toFixed(1));
                        needle.setAttribute('y2', (ncy + 54 * Math.sin(nAng)).toFixed(1));
                    }
                }

                /* ── Wave update ── */
                var waveCv = el.querySelector('.wave-canvas');
                if (waveCv) {
                    waveCv.dataset.value = changes.value;
                    var wMax = parseInt(waveCv.dataset.max) || 100;
                    var wReadout = el.querySelector('.slider-readout');
                    if (wReadout) wReadout.textContent = '[ ' + Math.round((changes.value / wMax) * 100) + '% ]';
                }

                /* ── Matrix update ── */
                var matrixGrid = el.querySelector('.matrix-grid');
                if (matrixGrid) {
                    var mMax = parseInt(matrixGrid.dataset.max) || 100;
                    var mSize = parseInt(matrixGrid.dataset.size) || 8;
                    var mColor = matrixGrid.dataset.color || 'accent';
                    var mColorCls = mColor === 'amber' ? ' color-amber' : mColor === 'danger' ? ' color-danger' : '';
                    matrixGrid.dataset.value = changes.value;
                    if (typeof nDynamic !== 'undefined' && nDynamic.buildMatrixPattern) {
                        var mLabel = matrixGrid.dataset.label || '';
                        var cells = nDynamic.buildMatrixPattern(changes.value, mMax, mSize, mLabel);
                        var divs = matrixGrid.querySelectorAll('.matrix-cell');
                        for (var mci = 0; mci < divs.length; mci++) {
                            divs[mci].className = 'matrix-cell' + (cells[mci] ? ' lit' + mColorCls : '');
                        }
                    }
                    var matReadout = el.querySelector('.slider-readout');
                    if (matReadout) matReadout.textContent = '[ ' + Math.round((changes.value / mMax) * 100) + '% ]';
                }

                /* ── Spark update ── */
                var sparkCv = el.querySelector('.spark-canvas');
                if (sparkCv) {
                    sparkCv.dataset.value = changes.value;
                    var spMax = parseInt(sparkCv.dataset.max) || 100;
                    /* Push value into history immediately on patch */
                    if (sparkCv._sparkHistory) {
                        sparkCv._sparkHistory.push(changes.value);
                        if (sparkCv._sparkHistory.length > 60) sparkCv._sparkHistory.shift();
                    }
                    var spReadout = el.querySelector('.slider-readout');
                    if (spReadout) spReadout.textContent = '[ ' + Math.round((changes.value / spMax) * 100) + '% ]';
                }

                /* ── Scope update ── */
                var scopeCv = el.querySelector('.scope-canvas');
                if (scopeCv) scopeCv.dataset.value = changes.value;

                /* ── Level update ── */
                var levelMeter = el.querySelector('.level-meter');
                if (levelMeter) {
                    var lMax = parseInt(levelMeter.dataset.max) || 20;
                    levelMeter.dataset.value = changes.value;
                    var warnTh = Math.floor(lMax * 0.7);
                    var critTh = Math.floor(lMax * 0.9);
                    var lSegs = levelMeter.querySelectorAll('.level-seg');
                    for (var lsi = 0; lsi < lSegs.length; lsi++) {
                        var lFilled = lsi < changes.value;
                        var lZone = lsi >= critTh ? ' crit' : lsi >= warnTh ? ' warn' : '';
                        var lPeak = (lsi === changes.value - 1 && changes.value > 0) ? ' peak-hold' : '';
                        lSegs[lsi].className = 'level-seg' + (lFilled ? ' filled' + lZone + lPeak : '');
                    }
                    var lReadout = el.querySelector('.slider-readout');
                    if (lReadout) lReadout.textContent = '[ ' + Math.round((changes.value / lMax) * 100) + '% ]';
                }
            }

            /* ── Ring items update ── */
            if (changes.items !== undefined && el.classList.contains('gc-ring')) {
                var ringFills = el.querySelectorAll('.ring-fill');
                var ringItems = changes.items;
                for (var rfi = 0; rfi < Math.min(ringFills.length, ringItems.length); rfi++) {
                    var rfCirc = parseFloat(ringFills[rfi].dataset.circ);
                    var rfPct = Math.min(1, (ringItems[rfi].value || 0) / (ringItems[rfi].max || 100));
                    var rfOff = rfCirc * (1 - rfPct);
                    ringFills[rfi].style.strokeDashoffset = rfOff.toFixed(2);
                }
                /* Update corner value readouts */
                var cornerVals = el.querySelectorAll('.ring-corner-val');
                for (var cvi = 0; cvi < cornerVals.length; cvi++) {
                    var idx = parseInt(cornerVals[cvi].dataset.ringIdx);
                    if (ringItems[idx] !== undefined) {
                        var cvPct = Math.round(((ringItems[idx].value || 0) / (ringItems[idx].max || 100)) * 100);
                        cornerVals[cvi].textContent = '[ ' + cvPct + '% ]';
                    }
                }
            }

            /* state → card */
            if (changes.state !== undefined) {
                if (el.classList.contains('nd-card') || el.classList.contains('is-on') || el.classList.contains('is-off')) {
                    var isOn = changes.state === 'on';
                    el.classList.remove('is-on', 'is-off');
                    el.classList.add(isOn ? 'is-on' : 'is-off');
                    var stateEl = el.querySelector('.card-state');
                    var statusEl = el.querySelector('.card-status');
                    if (stateEl) {
                        stateEl.textContent = '[ ' + (isOn ? (ctrl.on || 'ON') : (ctrl.off || 'OFF')) + ' ]';
                    }
                    if (statusEl) statusEl.textContent = isOn ? 'ON' : 'OFF';
                }
            }

            /* label → panel-label text */
            if (changes.label !== undefined) {
                var labelEl = el.querySelector('.panel-label');
                if (labelEl) labelEl.textContent = changes.label;
            }

            /* name → card-name */
            if (changes.name !== undefined) {
                var nameEl = el.querySelector('.card-name');
                if (nameEl) nameEl.textContent = changes.name;
            }

            /* items → status-grid rows (in-place to avoid full rebuild) */
            if (changes.items !== undefined && !el.classList.contains('gc-ring')) {
                var statusGrid = el.querySelector('.status-grid');
                var statusRows = statusGrid ? statusGrid.querySelectorAll('.status-row') : [];
                if (statusRows.length === changes.items.length) {
                    /* Same row count — update text & color in place */
                    for (var ri = 0; ri < changes.items.length; ri++) {
                        var row = statusRows[ri];
                        var r   = changes.items[ri];
                        var lbl = row.querySelector('.status-label');
                        var val = row.querySelector('.status-val');
                        if (lbl) lbl.textContent = r.k || '';
                        if (val) {
                            val.textContent = r.v || '';
                            val.className = 'status-val' + (r.c ? ' ' + r.c : '');
                        }
                    }
                } else {
                    /* Row count changed — fall back to full rebuild */
                    render(_controls, null);
                    return notumAHI;
                }
            }

            /* Apply AHI extensions (disabled, hidden, badge, tooltip, progress, status) */
            applyAHIExtensions();
        }

        rebuildIdMap();
        return notumAHI;
    }

    /**
     * notumAHI.insert(index, control)
     *
     * Insert a new control at the given position.
     *
     * @param {number} index    Position to insert at (-1 or omit to append)
     * @param {Object} control  Control definition object
     * @returns {Object} notumAHI (for chaining)
     */
    function insert(index, control) {
        if (!control) { control = index; index = -1; }
        var c = deepClone(control);
        if (index < 0 || index >= _controls.length) {
            _controls.push(c);
        } else {
            _controls.splice(index, 0, c);
        }
        render(_controls, null);
        return notumAHI;
    }

    /**
     * notumAHI.remove(idOrIndex)
     *
     * Remove a control from the layout.
     *
     * @param {string|number} idOrIndex  Control id or numeric index
     * @returns {Object} notumAHI (for chaining)
     */
    function remove(idOrIndex) {
        var idx = resolveIndex(idOrIndex);
        if (idx < 0 || idx >= _controls.length) {
            console.error('[notumAHI] remove: control not found: "' + idOrIndex +
                '". Registered ids: ' + Object.keys(_idMap).join(', '));
            return notumAHI;
        }
        _controls.splice(idx, 1);
        render(_controls, null);
        return notumAHI;
    }

    /**
     * notumAHI.dialog(definition)
     *
     * Summon a modal dialog and wait for the user's response.
     * Returns a Promise that resolves with the button value
     * or null if dismissed.
     *
     * @param {Object} definition  { title, body, buttons: [{ label, value, style? }] }
     * @returns {Promise<string|null>}
     */
    function dialog(definition) {
        ensureDialogOverlay();
        if (typeof nDynamic !== 'undefined' && nDynamic.showDialog) {
            return nDynamic.showDialog(definition).then(function (val) {
                emit({
                    event: 'dialog_response',
                    dialog: definition.title || '',
                    value: val
                });
                return val;
            });
        }
        /* Fallback: basic browser confirm */
        var msg = (definition.title || '') + '\n' + (definition.body || '');
        var result = window.confirm(msg) ? 'yes' : null;
        emit({ event: 'dialog_response', dialog: definition.title || '', value: result });
        return Promise.resolve(result);
    }

    /**
     * notumAHI.dismiss(value?)
     *
     * Close the current dialog programmatically.
     *
     * @param {string} value  Optional value to resolve the dialog promise with
     */
    function dismiss(value) {
        if (typeof nDynamic !== 'undefined' && nDynamic.closeDialog) {
            nDynamic.closeDialog(value || null);
        }
    }

    /**
     * notumAHI.lock(idOrIndex)
     *
     * Disable interaction on a specific control.
     *
     * @param {string|number} idOrIndex  Control id or index
     * @returns {Object} notumAHI
     */
    function lock(idOrIndex) {
        return patch(idOrIndex, { disabled: true });
    }

    /**
     * notumAHI.unlock(idOrIndex)
     *
     * Re-enable interaction on a specific control.
     *
     * @param {string|number} idOrIndex  Control id or index
     * @returns {Object} notumAHI
     */
    function unlock(idOrIndex) {
        return patch(idOrIndex, { disabled: false });
    }

    /**
     * notumAHI.read(idOrIndex?)
     *
     * Read the current state of one or all controls.
     * Returns a snapshot of the control definitions with
     * current values as reported by the DOM.
     *
     * @param {string|number} idOrIndex  Optional — omit to read all
     * @returns {Object|Array}
     */
    function read(idOrIndex) {
        /* Sync DOM state back to _controls */
        syncFromDOM();

        if (idOrIndex !== undefined && idOrIndex !== null) {
            var idx = resolveIndex(idOrIndex);
            if (idx >= 0 && idx < _controls.length) {
                return deepClone(_controls[idx]);
            }
            return null;
        }
        return deepClone(_controls);
    }

    /** Read live values from the DOM back into _controls */
    function syncFromDOM() {
        if (!_container) return;
        var children = _container.children;
        for (var i = 0; i < _controls.length && i < children.length; i++) {
            var el = children[i];
            var ctrl = _controls[i];

            /* Slider value */
            var slider = el.querySelector('.seg-slider');
            if (slider) ctrl.value = parseInt(slider.dataset.value) || 0;

            /* Card state */
            if (el.classList.contains('is-on')) ctrl.state = 'on';
            else if (el.classList.contains('is-off')) ctrl.state = 'off';

            /* Stepper value */
            var stepVal = el.querySelector('.stepper-val');
            if (stepVal) ctrl.value = parseInt(stepVal.textContent) || 0;

            /* Toggle active */
            var activeOpt = el.querySelector('.tg-option.active');
            if (activeOpt && ctrl.options) {
                ctrl.active = ctrl.options.indexOf(activeOpt.dataset.val);
            }
        }
    }

    /**
     * notumAHI.onEvent(callback)
     *
     * Register a callback to receive all user interaction events.
     * Events are structured JSON objects.
     *
     * @param {Function} callback  fn(event) — receives event objects
     * @returns {Function} unsubscribe function
     */
    function onEvent(callback) {
        if (typeof callback !== 'function') return function () {};
        _listeners.push(callback);
        return function unsubscribe() {
            _listeners = _listeners.filter(function (fn) { return fn !== callback; });
        };
    }

    /**
     * notumAHI.toast(message, level?, duration?)
     *
     * Show an ephemeral notification toast.
     *
     * @param {string} message   Text to display
     * @param {string} level     'info' | 'warn' | 'error' | 'ok' (default: 'info')
     * @param {number} duration  Display time in ms (default: 3000)
     */
    function toast(message, level, duration) {
        level = level || 'info';
        duration = duration || TOAST_DURATION;

        var tc = ensureToastContainer();
        var t = document.createElement('div');
        t.className = 'ahi-toast ahi-toast-' + level;

        var colorMap = { info: '#00E5FF', warn: '#FFB300', error: '#FF3333', ok: '#00E5FF' };
        var borderColor = colorMap[level] || colorMap.info;

        t.style.cssText =
            'pointer-events:auto;padding:10px 16px;' +
            'background:#141414;color:#F4F4F4;' +
            'border:1px solid ' + borderColor + ';' +
            'border-left:3px solid ' + borderColor + ';' +
            'font-family:Rajdhani,sans-serif;font-size:0.85rem;' +
            'font-weight:500;letter-spacing:0.04em;text-transform:uppercase;' +
            'opacity:0;transition:opacity ' + TOAST_FADE + 'ms;' +
            'max-width:360px;word-break:break-word;';

        t.textContent = message;
        tc.appendChild(t);
        _toastStack.push(t);

        /* Fade in */
        requestAnimationFrame(function () {
            t.style.opacity = '1';
        });

        if (typeof nbeep === 'function') {
            nbeep('toast_' + level);
        }

        /* Auto-dismiss */
        setTimeout(function () {
            t.style.opacity = '0';
            setTimeout(function () {
                if (t.parentNode) t.parentNode.removeChild(t);
                _toastStack = _toastStack.filter(function (x) { return x !== t; });
            }, TOAST_FADE);
        }, duration);

        emit({ event: 'toast', message: message, level: level });
    }

    /**
     * notumAHI.notify(opts)
     *
     * Show an in-grid notification over the top row of controls.
     * Non-interactive, rate-limited (30s default), auto-dismissed.
     * Requires nNotify.js to be loaded.
     *
     * @param {Object} opts
     *   icon     {string}  Phosphor icon class (e.g. 'ph-bell')
     *   title    {string}  Notification title
     *   subtitle {string}  Secondary text line
     *   linger   {number}  Auto-dismiss time in ms (default: 4000)
     * @returns {Object} notumAHI (for chaining)
     */
    function notify(opts) {
        if (typeof nNotify !== 'undefined') {
            /* Ensure nNotify is bound to our container */
            if (_container && !nNotify.isActive()) {
                nNotify.init(_container);
            }
            nNotify.show(opts || {});
        } else {
            /* Fallback: use toast if nNotify not loaded */
            toast(opts.title || opts.subtitle || 'Notification', 'info', opts.linger || 4000);
        }
        emit({ event: 'notify', title: opts.title || '', subtitle: opts.subtitle || '' });
        return notumAHI;
    }

    /**
     * notumAHI.destroy()
     *
     * Tear down the AHI instance and clean up all listeners.
     */
    function destroy() {
        if (_delegatedHandler && _container) {
            _container.removeEventListener('click', _delegatedHandler, true);
        }
        if (_mutationObserver) _mutationObserver.disconnect();
        if (typeof nInteractive !== 'undefined') nInteractive.destroy();
        else if (typeof nDynamic !== 'undefined') nDynamic.destroy();

        _container    = null;
        _controls     = [];
        _config       = {};
        _listeners    = [];
        _idMap        = {};
        _initialized  = false;
        _layoutReady  = false;

        /* Remove toasts */
        _toastStack.forEach(function (t) {
            if (t.parentNode) t.parentNode.removeChild(t);
        });
        _toastStack = [];

        var tc = document.getElementById('ahi-toast-container');
        if (tc) tc.remove();
    }

    /* ═══════════════════════════════════
       Expose Public API
       Notum Robotics — n-r.hr
       ═══════════════════════════════════ */

    var api = {
        /* Core */
        version:  VERSION,
        init:     init,
        render:   render,
        patch:    patch,
        insert:   insert,
        remove:   remove,
        destroy:  destroy,

        /* Dialogs */
        dialog:   dialog,
        dismiss:  dismiss,

        /* Control state */
        lock:     lock,
        unlock:   unlock,
        read:     read,

        /* Events */
        onEvent:  onEvent,

        /* Notifications */
        toast:    toast,
        notify:   notify,

        /* Flow engine (loaded separately via ahi-flow.js) */
        flow:     null  // Populated by ahi-flow.js if loaded
    };

    return api;
})();

/* ── Global export ────────────────────────────────────────── */
window.notumAHI = notumAHI;
