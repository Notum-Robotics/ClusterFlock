/* ──────────────────────────────────────────────────────────────
   notumDemo — Demo-Specific Wiring for Notum AHI Homepage
   Copyright © 2026 Notum Robotics. Licensed under the MIT License.

   This file contains ONLY demo-specific behavior:
     • Homepage card toggles, dialog demos, slider knobs
     • Auto-grid population using GRID_CATALOG + renderGridItem
     • Component Properties demo buttons (upload, scan, deploy…)
     • nbeep configuration panel wiring

   FRAMEWORK code lives in:
     nUtils.js    — helpers (escHtml, flashOutline, gridCorners)
     nDynamic.js  — grid layout engine + renderControl
     nInteractive.js — editing layer
     nComp.js     — component properties (progress, status, active)
     nNotify.js   — in-grid notifications
     nStore.js    — reactive state store
     nRegistry.js — module dependency system

   DEPENDENCY: nUtils.js, nComp.js, nCatalog.js must be loaded first.
   ────────────────────────────────────────────────────────────── */

(function () {
    'use strict';

    /* ═══════════════════════════════════
       Helpers (delegated to nUtils / nComp)
       ═══════════════════════════════════ */

    var flashOutline   = nUtils.flashOutline;
    var escHtml        = nUtils.escHtml;
    var FLASH_DURATION = nUtils.FLASH_DURATION;

    /* ═══════════════════════════════════
       Dialog (self-contained copy for homepage)
       ═══════════════════════════════════ */

    var $overlay   = null;
    var $dialogBox = null;
    var dialogResolve = null;
    var cornerStrobeTimer = null;
    var _overlayListenerBound = false;

    function resolveDialogDOM() {
        if (!$overlay)   $overlay   = document.getElementById('dialog-overlay');
        if (!$dialogBox) $dialogBox = document.getElementById('dialog-box');
        if ($overlay && !_overlayListenerBound) {
            _overlayListenerBound = true;
            $overlay.addEventListener('click', function (e) {
                if (e.target === $overlay) {
                    if (typeof nbeep === 'function') nbeep('dialog_dismiss');
                    closeDialog(null);
                }
            });
        }
    }

    function showDialog(opts) {
        resolveDialogDOM();
        if (!$overlay || !$dialogBox) {
            console.warn('[notumDemo] Dialog overlay not found — falling back to window.confirm');
            var msg = (opts.title || '') + '\n' + (opts.body || '');
            return Promise.resolve(window.confirm(msg) ? 'yes' : null);
        }
        return new Promise(function (resolve) {
            dialogResolve = resolve;
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
                    if (!flashOutline(el)) return;
                    if (typeof nbeep === 'function') nbeep('dialog_btn_' + (el.dataset.val || el.textContent));
                    setTimeout(function () { closeDialog(el.dataset.val); }, FLASH_DURATION);
                });
            });

            $overlay.classList.add('open');

            (function startCornerStrobe() {
                if (cornerStrobeTimer) clearTimeout(cornerStrobeTimer);
                var corners = $dialogBox.querySelectorAll('.corner');
                var flash = 0, totalFlashes = 5, on = false;
                function tick() {
                    if (!$overlay.classList.contains('open')) {
                        corners.forEach(function (c) { c.classList.remove('flash-outline'); });
                        cornerStrobeTimer = null;
                        return;
                    }
                    if (flash < totalFlashes * 2) {
                        on = !on;
                        corners.forEach(function (c) {
                            if (on) c.classList.add('flash-outline'); else c.classList.remove('flash-outline');
                        });
                        flash++;
                        cornerStrobeTimer = setTimeout(tick, 50);
                    } else {
                        corners.forEach(function (c) { c.classList.remove('flash-outline'); });
                        cornerStrobeTimer = setTimeout(function () { flash = 0; tick(); }, 200);
                    }
                }
                cornerStrobeTimer = setTimeout(tick, 50);
            })();

            var playAlarm = opts.alarm !== undefined ? opts.alarm : true;
            if (playAlarm && typeof nbeep === 'function' && dialogBeepTitle) {
                nbeep(dialogBeepTitle, dialogBeepBody || true);
            }
        });
    }

    function closeDialog(value) {
        if (cornerStrobeTimer) {
            clearTimeout(cornerStrobeTimer);
            cornerStrobeTimer = null;
            if ($dialogBox) {
                $dialogBox.querySelectorAll('.corner').forEach(function (c) { c.classList.remove('flash-outline'); });
            }
        }
        if ($overlay) $overlay.classList.remove('open');
        if (typeof nDesignAudio === 'object' && nDesignAudio.killActive) nDesignAudio.killActive();
        if (dialogResolve) { var fn = dialogResolve; dialogResolve = null; fn(value); }
    }

    /* ═══════════════════════════════════
       Flash on all [data-flash] buttons
       ═══════════════════════════════════ */

    document.querySelectorAll('[data-flash]').forEach(function (el) {
        el.addEventListener('click', function () { if (!flashOutline(el)) return; });
    });

    /* ═══════════════════════════════════
       Icon Toggle Buttons (.icon-toggle)
       ═══════════════════════════════════ */

    document.querySelectorAll('.icon-toggle').forEach(function (el) {
        el.addEventListener('click', function () {
            if (!flashOutline(el)) return;
            var turningOff = el.classList.contains('is-on');
            el.classList.remove('is-on', 'is-off');
            el.classList.add(turningOff ? 'is-off' : 'is-on');
        });
    });

    /* ═══════════════════════════════════
       Dialog Demos
       ═══════════════════════════════════ */

    function bind(id, fn) { var el = document.getElementById(id); if (el) el.addEventListener('click', fn); }

    bind('dlg-confirm', function () {
        showDialog({ title: 'Confirm Action', body: 'Are you sure you want to proceed?', buttons: [
            { label: 'CANCEL', value: 'no' }, { label: 'CONFIRM', value: 'yes', style: 'primary' }
        ]}).then(function (v) { console.log('[Dialog] confirm →', v); });
    });

    bind('dlg-warning', function () {
        showDialog({ title: 'Warning', body: 'Cloud sync is degraded. Retry connection?', buttons: [
            { label: 'DISMISS', value: 'dismiss' }, { label: 'RETRY', value: 'retry', style: 'warning' }
        ]}).then(function (v) { console.log('[Dialog] warning →', v); });
    });

    bind('dlg-danger', function () {
        showDialog({ title: 'Critical', body: 'This will erase all stored data. This action cannot be undone.', buttons: [
            { label: 'ABORT', value: 'abort' }, { label: 'ERASE ALL', value: 'erase', style: 'danger' }
        ]}).then(function (v) { console.log('[Dialog] danger →', v); });
    });

    bind('dlg-multi', function () {
        showDialog({ title: 'Choose Protocol', body: 'Select a communication protocol for the gateway.', buttons: [
            { label: 'MQTT', value: 'mqtt' }, { label: 'ZIGBEE', value: 'zigbee', style: 'primary' },
            { label: 'Z-WAVE', value: 'zwave', style: 'warning' }, { label: 'THREAD', value: 'thread' }
        ]}).then(function (v) { console.log('[Dialog] multi →', v); });
    });

    bind('dlg-info', function () {
        showDialog({ title: 'System Information', body: 'Firmware v2.4.1-RC3 · Build 20250201 · Kernel 5.15.0', buttons: [
            { label: 'OK', value: 'ok', style: 'primary' }
        ]}).then(function (v) { console.log('[Dialog] info →', v); });
    });

    /* ═══════════════════════════════════
       Toggle Groups
       ═══════════════════════════════════ */

    document.querySelectorAll('.toggle-group').forEach(function (group) {
        group.querySelectorAll('.tg-option').forEach(function (opt) {
            opt.addEventListener('click', function () {
                if (opt.classList.contains('active')) return;
                if (!flashOutline(opt)) return;
                var cur = group.querySelector('.tg-option.active');
                if (cur) cur.classList.remove('active');
                opt.classList.add('active');
                console.log('[Toggle]', group.id, '→', opt.dataset.val);
            });
        });
    });

    /* ═══════════════════════════════════
       Segmented Sliders (interactive)
       ═══════════════════════════════════ */

    function buildSegSlider(el) {
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
        function updateReadout() {
            if (!readout) return;
            var cur = parseInt(el.dataset.value) || 0;
            if (el.id === 'slider-temp') {
                var minT = parseInt(el.dataset.min) || 0;
                readout.textContent = '[ ' + (minT + cur) + '°C ]';
            } else {
                readout.textContent = '[ ' + Math.round((cur / max) * 100) + '% ]';
            }
        }
        var lastSoundedValue = -1;
        function setVal(idx, playSound) {
            var newVal = idx + 1;
            el.dataset.value = newVal;
            el.querySelectorAll('.seg').forEach(function (s, si) { s.classList.toggle('filled', si < newVal); });
            updateReadout();
            if (playSound && newVal !== lastSoundedValue && typeof nbeep === 'function') {
                lastSoundedValue = newVal;
                nbeep('adjust', false, Math.pow(2, (newVal / max - 0.5) * 2));
            }
        }
        var dragging = false;
        function handleInteraction(e) {
            var rect = el.getBoundingClientRect();
            var x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
            var segW = rect.width / max;
            setVal(Math.max(0, Math.min(max - 1, Math.floor(x / segW))), true);
        }
        el.addEventListener('mousedown', function (e) { dragging = true; handleInteraction(e); });
        el.addEventListener('touchstart', function (e) { dragging = true; handleInteraction(e); }, { passive: true });
        document.addEventListener('mousemove', function (e) { if (dragging) handleInteraction(e); });
        document.addEventListener('touchmove', function (e) { if (dragging) handleInteraction(e); }, { passive: true });
        document.addEventListener('mouseup', function () { dragging = false; });
        document.addEventListener('touchend', function () { dragging = false; });
    }

    document.querySelectorAll('.seg-slider').forEach(buildSegSlider);

    /* ═══════════════════════════════════
       Segmented Bars (static display)
       ═══════════════════════════════════ */

    function initSegBar(el) {
        if (el._segTimer)   { clearInterval(el._segTimer);  el._segTimer = null; }
        if (el._segTimeout) { clearTimeout(el._segTimeout);  el._segTimeout = null; }
        var max = parseInt(el.dataset.max) || 10;
        var value = parseInt(el.dataset.value) || 0;
        el.innerHTML = '';
        var isFull = (value >= max);
        if (isFull) el.classList.add('bar-full'); else el.classList.remove('bar-full');
        var filledSegs = [];
        for (var i = 0; i < max; i++) {
            var seg = document.createElement('div');
            seg.className = 'seg' + (i < value ? ' filled' : '');
            el.appendChild(seg);
            if (i < value) filledSegs.push(seg);
        }
        if (isFull && filledSegs.length > 1) segBarBlink(el, filledSegs);
        else if (filledSegs.length > 1)      segBarTravel(el, filledSegs);
    }

    function segBarTravel(el, segs) {
        var pos = 0; segs[0].classList.add('seg-dim');
        el._segTimer = setInterval(function () {
            segs[pos].classList.remove('seg-dim');
            pos = (pos + 1) % segs.length;
            segs[pos].classList.add('seg-dim');
        }, 120);
    }

    function segBarBlink(el, segs) {
        var step = 0;
        function tick() {
            if (step < 4) {
                var on = (step % 2 === 0);
                for (var i = 0; i < segs.length; i++) { if (on) segs[i].classList.add('seg-bright'); else segs[i].classList.remove('seg-bright'); }
                step++;
                el._segTimeout = setTimeout(tick, 50);
            } else { step = 0; el._segTimeout = setTimeout(tick, 350); }
        }
        tick();
    }

    document.querySelectorAll('.seg-bar').forEach(initSegBar);

    /* ═══════════════════════════════════
       Vertical Bars / Sliders
       ═══════════════════════════════════ */

    function initSegBarV(el) {
        if (el._segTimer)   { clearInterval(el._segTimer);  el._segTimer = null; }
        if (el._segTimeout) { clearTimeout(el._segTimeout);  el._segTimeout = null; }
        var max = parseInt(el.dataset.max) || 10;
        var value = parseInt(el.dataset.value) || 0;
        el.innerHTML = '';
        var isFull = (value >= max);
        if (isFull) el.classList.add('bar-full'); else el.classList.remove('bar-full');
        var filledSegs = [];
        for (var i = 0; i < max; i++) {
            var seg = document.createElement('div');
            seg.className = 'seg' + (i < value ? ' filled' : '');
            el.appendChild(seg);
            if (i < value) filledSegs.push(seg);
        }
        if (isFull && filledSegs.length > 1) segBarBlink(el, filledSegs);
        else if (filledSegs.length > 1)      segBarTravel(el, filledSegs);
    }

    document.querySelectorAll('.seg-bar-v').forEach(initSegBarV);

    function buildSegSliderV(el) {
        var max = parseInt(el.dataset.max) || 10;
        var value = parseInt(el.dataset.value) || 0;
        var readout = el.parentNode ? el.parentNode.querySelector('.slider-readout') : null;
        el.innerHTML = '';
        for (var i = 0; i < max; i++) {
            var seg = document.createElement('div');
            seg.className = 'seg' + (i < value ? ' filled' : '');
            seg.dataset.idx = i;
            el.appendChild(seg);
        }
        function updateReadout() {
            if (!readout) return;
            var cur = parseInt(el.dataset.value) || 0;
            readout.textContent = '[ ' + Math.round((cur / max) * 100) + '% ]';
        }
        var lastSoundedValue = -1;
        function setVal(idx, playSound) {
            var newVal = idx + 1;
            el.dataset.value = newVal;
            el.querySelectorAll('.seg').forEach(function (s, si) { s.classList.toggle('filled', si < newVal); });
            updateReadout();
            if (playSound && newVal !== lastSoundedValue && typeof nbeep === 'function') {
                lastSoundedValue = newVal;
                nbeep('adjust', false, Math.pow(2, (newVal / max - 0.5) * 2));
            }
        }
        var dragging = false;
        function handleInteraction(e) {
            var rect = el.getBoundingClientRect();
            var y = (e.touches ? e.touches[0].clientY : e.clientY) - rect.top;
            var segH = rect.height / max;
            var fromTop = Math.max(0, Math.min(max - 1, Math.floor(y / segH)));
            setVal(max - 1 - fromTop, true);
        }
        el.addEventListener('mousedown', function (e) { dragging = true; handleInteraction(e); });
        el.addEventListener('touchstart', function (e) { dragging = true; handleInteraction(e); }, { passive: true });
        document.addEventListener('mousemove', function (e) { if (dragging) handleInteraction(e); });
        document.addEventListener('touchmove', function (e) { if (dragging) handleInteraction(e); }, { passive: true });
        document.addEventListener('mouseup', function () { dragging = false; });
        document.addEventListener('touchend', function () { dragging = false; });
    }

    document.querySelectorAll('.seg-slider-v').forEach(buildSegSliderV);

    /* ═══════════════════════════════════
       Numeric Steppers
       ═══════════════════════════════════ */

    document.querySelectorAll('.stepper').forEach(function (el) {
        if (el.id === 'stepper-seed') return;
        var valEl = el.querySelector('.stepper-val');
        var val = parseInt(valEl.textContent) || 0;
        el.querySelectorAll('.stepper-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                if (!flashOutline(btn)) return;
                val = Math.max(0, val + parseInt(btn.dataset.dir));
                valEl.textContent = val;
            });
        });
    });

    /* ═══════════════════════════════════
       Sub-menu items / Uptime counter
       ═══════════════════════════════════ */

    document.querySelectorAll('.submenu-item').forEach(function (el) {
        el.addEventListener('click', function () {
            if (!flashOutline(el)) return;
            var name = el.querySelector('.submenu-name');
            if (name) console.log('[Submenu] →', name.textContent);
        });
    });

    var uptimeEl = document.getElementById('stat-uptime');
    if (uptimeEl) {
        var baseSec = 14 * 86400 + 7 * 3600 + 32 * 60 + 19;
        setInterval(function () {
            baseSec++;
            var d = Math.floor(baseSec / 86400);
            var h = Math.floor((baseSec % 86400) / 3600);
            var m = Math.floor((baseSec % 3600) / 60);
            var s = baseSec % 60;
            uptimeEl.textContent = d + 'D ' + String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
        }, 1000);
    }

    /* ═══════════════════════════════════
       Demo Cards — Active State Toggle
       ═══════════════════════════════════ */

    function toggleDemoCard(card) {
        var isOn = card.classList.contains('is-on');
        var onLabel = card.dataset.on || 'ACTIVE';
        var offLabel = card.dataset.off || 'INACTIVE';
        card.classList.remove('is-on', 'is-off');
        card.classList.add(isOn ? 'is-off' : 'is-on');
        var stateEl = card.querySelector('.card-state');
        if (stateEl) stateEl.textContent = isOn ? '[ ' + offLabel + ' ]' : '[ ' + onLabel + ' ]';
    }

    document.querySelectorAll('.demo-card').forEach(function (card) {
        card.addEventListener('click', function () {
            if (!flashOutline(card)) return;
            toggleDemoCard(card);
        });
    });

    /* ═══════════════════════════════════
       Auto-Fit Grid (Homepage)
       Uses GRID_CATALOG from nCatalog.js
       Delegates to nDynamic.renderControl via the
       old renderGridItem wrapper for backward compat.
       ═══════════════════════════════════ */

    var gridCorners = nUtils.gridCorners;

    function renderGridItem(item, cols) {
        var el = document.createElement('div');
        var spanCol = Math.min(item.cols, cols);
        switch (item.type) {
            case 'card': {
                var isOn = item.state === 'on';
                var cardSize = item.size || '2x2';
                var cardRows = item.rows || 2;
                el.className = 'card ' + (isOn ? 'is-on' : 'is-off') + ' demo-card';
                el.style.gridColumn = 'span ' + spanCol;
                el.style.gridRow = 'span ' + cardRows;
                el.dataset.on = item.on; el.dataset.off = item.off; el.dataset.size = cardSize;
                el.innerHTML = gridCorners() +
                    '<div class="card-icon"><i class="ph ' + item.icon + '"></i></div>' +
                    '<div class="card-info"><div class="card-name">' + escHtml(item.name) + '</div>' +
                    '<div class="card-state">[ ' + escHtml(isOn ? item.on : item.off) + ' ]</div></div>' +
                    '<div class="card-status">' + (isOn ? 'ON' : 'OFF') + '</div>';
                break;
            }
            case 'slider': {
                el.className = 'grid-cell gc-slider'; el.style.gridColumn = 'span ' + spanCol; el.style.gridRow = 'span 2';
                el.innerHTML = gridCorners() +
                    '<div class="slider-header"><span class="panel-label">' + escHtml(item.label) + '</span>' +
                    '<span class="slider-readout">[ ' + Math.round((item.value / item.max) * 100) + '% ]</span></div>' +
                    '<div class="seg-slider" data-value="' + item.value + '" data-max="' + item.max + '" data-color="' + item.color + '"></div>';
                break;
            }
            case 'toggle': {
                el.className = 'grid-cell gc-toggle'; el.style.gridColumn = 'span ' + spanCol; el.style.gridRow = 'span 1';
                var optHtml = '';
                item.options.forEach(function (o, i) {
                    optHtml += '<div class="tg-option' + (i === item.active ? ' active' : '') + '" data-val="' + o + '">' + o + '</div>';
                });
                el.innerHTML = gridCorners() + '<div class="panel-label">' + escHtml(item.label) + '</div><div class="toggle-group">' + optHtml + '</div>';
                break;
            }
            case 'button': {
                el.className = 'action-btn grid-btn' + (item.style ? ' ' + item.style : '');
                el.style.gridColumn = 'span ' + spanCol; el.style.gridRow = 'span 1';
                el.setAttribute('data-flash', '');
                el.innerHTML = '<i class="ph ' + item.icon + '"></i><span class="btn-label">' + escHtml(item.label) + '</span>';
                break;
            }
            case 'stepper': {
                el.className = 'grid-cell gc-stepper'; el.style.gridColumn = 'span ' + spanCol; el.style.gridRow = 'span 2';
                el.innerHTML = gridCorners() + '<div class="panel-label">' + escHtml(item.label) + '</div>' +
                    '<div class="stepper"><div class="stepper-btn" data-dir="-1">−</div><div class="stepper-val">' + item.value + '</div><div class="stepper-btn" data-dir="+1">+</div></div>';
                break;
            }
            case 'bar': {
                el.className = 'grid-cell gc-bar'; el.style.gridColumn = 'span ' + spanCol; el.style.gridRow = 'span 1';
                el.innerHTML = '<div class="slider-header"><span class="panel-label">' + escHtml(item.label) + '</span>' +
                    '<span class="slider-readout">[ ' + Math.round((item.value / item.max) * 100) + '% ]</span></div>' +
                    '<div class="seg-bar" data-value="' + item.value + '" data-max="' + item.max + '" data-color="' + item.color + '"></div>';
                break;
            }
            case 'status': {
                el.className = 'grid-cell gc-status'; el.style.gridColumn = 'span ' + spanCol; el.style.gridRow = 'span 2';
                var rows = '';
                item.items.forEach(function (r) {
                    rows += '<div class="status-row"><span class="status-label">' + escHtml(r.k) +
                        '</span><span class="status-val' + (r.c ? ' ' + r.c : '') + '">' + escHtml(r.v) + '</span></div>';
                });
                el.innerHTML = gridCorners() + '<div class="panel-label">' + escHtml(item.label) + '</div><div class="status-grid">' + rows + '</div>';
                break;
            }
        }
        return el;
    }

    function autoPopulateGrid() {
        var container = document.getElementById('auto-grid');
        if (!container) return;
        var containerW = container.clientWidth;
        var cols = containerW > 600 ? 4 : 2;
        container.style.gridTemplateColumns = 'repeat(' + cols + ', 1fr)';
        var frag = document.createDocumentFragment();
        GRID_CATALOG.forEach(function (item) {
            var el = renderGridItem(item, cols);
            if (el) frag.appendChild(el);
        });
        container.appendChild(frag);
        container.querySelectorAll('.seg-slider').forEach(buildSegSlider);
        container.querySelectorAll('.seg-bar').forEach(initSegBar);
        container.querySelectorAll('.toggle-group').forEach(function (group) {
            group.querySelectorAll('.tg-option').forEach(function (opt) {
                opt.addEventListener('click', function () {
                    if (opt.classList.contains('active')) return;
                    if (!flashOutline(opt)) return;
                    var cur = group.querySelector('.tg-option.active');
                    if (cur) cur.classList.remove('active');
                    opt.classList.add('active');
                });
            });
        });
        container.querySelectorAll('.stepper').forEach(function (el) {
            var valEl = el.querySelector('.stepper-val');
            var val = parseInt(valEl.textContent) || 0;
            el.querySelectorAll('.stepper-btn').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    if (!flashOutline(btn)) return;
                    val = Math.max(0, val + parseInt(btn.dataset.dir));
                    valEl.textContent = val;
                });
            });
        });
        container.querySelectorAll('[data-flash]').forEach(function (el) {
            el.addEventListener('click', function () { if (!flashOutline(el)) return; });
        });
        container.querySelectorAll('.demo-card').forEach(function (card) {
            card.addEventListener('click', function () {
                if (!flashOutline(card)) return;
                toggleDemoCard(card);
            });
        });
    }

    autoPopulateGrid();

    /* ═══════════════════════════════════
       Component Properties — Demo Wiring
       ═══════════════════════════════════ */

    if (typeof nComp !== 'undefined') {
        // Upload: click → progress 0→100 then clear
        (function () {
            var btn = document.getElementById('cp-upload');
            if (!btn) return;
            var busy = false;
            btn.addEventListener('click', function () {
                if (busy) return; busy = true;
                var pct = 0;
                nComp.progress(btn, 0); nComp.status(btn, 'busy');
                var iv = setInterval(function () {
                    pct += 5 + Math.floor(pct / 10);
                    if (pct >= 100) { pct = 100; clearInterval(iv); nComp.progress(btn, 100); nComp.status(btn, 'ok');
                        setTimeout(function () { nComp.progress(btn, null); nComp.status(btn, null); busy = false; }, 2000);
                    } else { nComp.progress(btn, pct); }
                }, 250);
            });
        })();

        // Scan: indeterminate → complete
        (function () {
            var btn = document.getElementById('cp-scan-btn');
            if (!btn) return; var busy = false;
            btn.addEventListener('click', function () {
                if (busy) return; busy = true;
                nComp.progress(btn, -1); nComp.status(btn, 'busy');
                setTimeout(function () {
                    nComp.progress(btn, 100); nComp.status(btn, 'ok');
                    setTimeout(function () { nComp.progress(btn, null); nComp.status(btn, null); busy = false; }, 2000);
                }, 3000);
            });
        })();

        // Firmware
        (function () {
            var btn = document.getElementById('cp-firmware');
            if (!btn) return; var busy = false;
            btn.addEventListener('click', function () {
                if (busy) return; busy = true;
                var pct = 0; nComp.progress(btn, 0); nComp.status(btn, 'busy');
                var iv = setInterval(function () {
                    pct += 2 + Math.floor(pct / 20);
                    if (pct >= 100) { pct = 100; clearInterval(iv); nComp.progress(btn, 100); nComp.status(btn, 'ok');
                        setTimeout(function () { nComp.progress(btn, null); nComp.status(btn, null); busy = false; }, 2500);
                    } else { nComp.progress(btn, pct); }
                }, 180);
            });
        })();

        // Status demo
        (function () {
            var map = { 'cp-stat-ok': 'ok', 'cp-stat-warn': 'warn', 'cp-stat-error': 'error', 'cp-stat-busy': 'busy' };
            Object.keys(map).forEach(function (id) { var el = document.getElementById(id); if (el) nComp.status(el, map[id]); });
        })();

        // Deploy Build — 8s lockout
        (function () {
            var btn = document.getElementById('cp-deploy');
            if (!btn) return;
            btn.addEventListener('click', function () {
                if (btn.getAttribute('data-progress')) return;
                var pct = 0; nComp.progress(btn, 0); nComp.status(btn, 'busy');
                var iv = setInterval(function () {
                    pct += 1 + Math.floor(Math.random() * 3);
                    if (pct >= 100) { pct = 100; clearInterval(iv); nComp.progress(btn, 100); nComp.status(btn, 'ok');
                        setTimeout(function () { nComp.progress(btn, null); nComp.status(btn, null); }, 2500);
                    } else { nComp.progress(btn, pct); if (pct > 70) nComp.status(btn, 'warn'); }
                }, 200);
            });
        })();

        // Full Backup — 12s lockout
        (function () {
            var btn = document.getElementById('cp-backup');
            if (!btn) return;
            btn.addEventListener('click', function () {
                if (btn.getAttribute('data-progress')) return;
                var pct = 0; nComp.progress(btn, 0); nComp.status(btn, 'busy');
                var iv = setInterval(function () {
                    pct += 1;
                    if (pct >= 100) { pct = 100; clearInterval(iv); nComp.progress(btn, 100); nComp.status(btn, 'ok');
                        setTimeout(function () { nComp.progress(btn, null); nComp.status(btn, null); }, 3000);
                    } else { nComp.progress(btn, pct); }
                }, 110);
            });
        })();

        // Purge Cache
        (function () {
            var btn = document.getElementById('cp-purge');
            if (!btn) return;
            btn.addEventListener('click', function () {
                if (btn.getAttribute('data-progress')) return;
                nComp.progress(btn, -1); nComp.status(btn, 'busy');
                setTimeout(function () {
                    nComp.progress(btn, 100); nComp.status(btn, 'ok');
                    setTimeout(function () { nComp.progress(btn, null); nComp.status(btn, null); }, 2000);
                }, 4500);
            });
        })();

        // Diagnostics — 15s lockout
        (function () {
            var btn = document.getElementById('cp-diagnostics');
            if (!btn) return;
            btn.addEventListener('click', function () {
                if (btn.getAttribute('data-progress')) return;
                var pct = 0; nComp.progress(btn, 0); nComp.status(btn, 'busy');
                var iv = setInterval(function () {
                    var inc = pct < 30 ? 2 : (pct < 70 ? 1 : 2);
                    pct += inc;
                    if (pct >= 100) { pct = 100; clearInterval(iv); nComp.progress(btn, 100); nComp.status(btn, 'warn');
                        setTimeout(function () { nComp.status(btn, 'ok');
                            setTimeout(function () { nComp.progress(btn, null); nComp.status(btn, null); }, 2500);
                        }, 1500);
                    } else { nComp.progress(btn, pct); if (pct > 50 && pct < 75) nComp.status(btn, 'warn'); else if (pct >= 75) nComp.status(btn, 'busy'); }
                }, 140);
            });
        })();

        // OTA Update
        (function () {
            var btn = document.getElementById('cp-ota');
            if (!btn) return;
            btn.addEventListener('click', function () {
                if (btn.getAttribute('data-progress')) return;
                var pct = 0; nComp.progress(btn, 0); nComp.status(btn, 'busy');
                var iv = setInterval(function () {
                    pct += 3 + Math.floor(Math.random() * 3);
                    if (pct >= 100) { pct = 100; clearInterval(iv); nComp.progress(btn, 100); nComp.status(btn, 'ok');
                        setTimeout(function () { nComp.progress(btn, null); nComp.status(btn, null); }, 3000);
                    } else { nComp.progress(btn, pct); }
                }, 400);
            });
        })();

        // Active/Inactive toggle pair
        (function () {
            var toggle = document.getElementById('cp-active-toggle');
            var a = document.getElementById('cp-target-a');
            var b = document.getElementById('cp-target-b');
            if (!toggle || !a || !b) return;
            toggle.addEventListener('click', function () {
                var aOn = a.getAttribute('data-active') === 'true';
                nComp.active(a, !aOn); nComp.active(b, aOn);
            });
        })();
    }

    /* ═══════════════════════════════════
       nbeep — Audio Integration Layer
       ═══════════════════════════════════ */

    if (typeof nbeep === 'function' && typeof nDesignAudio === 'object') {
        function preWarm() { nDesignAudio.warmUp(); }
        document.addEventListener('mousedown',  preWarm, { passive: true });
        document.addEventListener('touchstart', preWarm, { passive: true });

        document.addEventListener('click', function (e) {
            var target = e.target.closest(
                '[data-nbeep], [data-flash], .tg-option, .stepper-btn, .submenu-item, .demo-card, .dialog-btn, .seg-slider, .seg-slider-v, .icon-toggle'
            );
            if (!target) return;
            if (target.id === 'nbeep-play' || target.closest('#nbeep-input')) return;
            if (target.id && target.id.indexOf('dlg-') === 0) return;
            var beepStr;
            if (target.dataset.nbeep)                    beepStr = target.dataset.nbeep;
            else if (target.classList.contains('tg-option'))    beepStr = 'select';
            else if (target.classList.contains('stepper-btn'))  beepStr = target.dataset.dir === '-1' ? 'decrement' : 'increment';
            else if (target.classList.contains('icon-toggle'))  beepStr = target.classList.contains('is-on') ? 'off' : 'on';
            else if (target.classList.contains('demo-card'))    beepStr = target.classList.contains('is-on') ? 'off' : 'on';
            else if (target.classList.contains('dialog-btn'))   {
                var val = (target.dataset.val || '').toLowerCase();
                beepStr = (val === 'yes' || val === 'confirm' || target.classList.contains('primary')) ? 'confirm' : 'dismiss';
            }
            else if (target.classList.contains('submenu-item')) beepStr = 'select';
            else if (target.classList.contains('seg-slider') || target.classList.contains('seg-slider-v')) return;
            else beepStr = target.classList.contains('danger') ? 'danger' : target.classList.contains('warning') ? 'warn' : 'action';
            nbeep(beepStr);
        });

        // Volume slider
        var nbeepVolEl = document.getElementById('nbeep-vol');
        if (nbeepVolEl) {
            new MutationObserver(function () {
                var val = parseInt(nbeepVolEl.dataset.value) || 0;
                var max = parseInt(nbeepVolEl.dataset.max) || 20;
                nDesignAudio.config.masterVolume = val / max;
                var readout = document.getElementById('nbeep-vol-val');
                if (readout) readout.textContent = '[ ' + Math.round((val / max) * 100) + '% ]';
            }).observe(nbeepVolEl, { attributes: true, attributeFilter: ['data-value'] });
        }

        // Duration slider
        var nbeepDurEl = document.getElementById('nbeep-dur');
        if (nbeepDurEl) {
            new MutationObserver(function () {
                var val = parseInt(nbeepDurEl.dataset.value) || 0;
                var max = parseInt(nbeepDurEl.dataset.max) || 20;
                var pct = val / max;
                nDesignAudio.config.maxDuration = Math.max(0.02, pct);
                var readout = document.getElementById('nbeep-dur-val');
                if (readout) readout.textContent = '[ ' + Math.round(pct * 100) + '% ]';
            }).observe(nbeepDurEl, { attributes: true, attributeFilter: ['data-value'] });
        }

        // Scale toggle
        var tgScale = document.getElementById('tg-scale');
        if (tgScale) {
            tgScale.addEventListener('click', function (e) {
                var opt = e.target.closest('.tg-option');
                if (opt && opt.dataset.val) nDesignAudio.config.scale = opt.dataset.val;
            });
        }

        // Soundscape toggle
        var tgMusical = document.getElementById('tg-musical');
        if (tgMusical) {
            tgMusical.addEventListener('click', function (e) {
                var opt = e.target.closest('.tg-option');
                if (!opt || !opt.dataset.val) return;
                var val = opt.dataset.val;
                nDesignAudio.config.useMusicalScale = true;
                nDesignAudio.config.soundMode = val === 'harmonic' ? 'harmonic' : val === 'ncars' ? 'ncars' : val === 'ncars2' ? 'ncars2' : 'standard';
            });
        }

        // Seed stepper
        var seedStepper = document.getElementById('stepper-seed');
        if (seedStepper) {
            var seedValEl = seedStepper.querySelector('.stepper-val');
            var seedVal = parseInt(seedValEl.textContent) || 2026;
            var seedReadout = document.getElementById('nbeep-seed-val');
            seedStepper.querySelectorAll('.stepper-btn').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    seedVal = Math.max(0, seedVal + parseInt(btn.dataset.dir));
                    seedValEl.textContent = seedVal;
                    nDesignAudio.config.globalSeed = seedVal;
                    if (seedReadout) seedReadout.textContent = '[ ' + seedVal + ' ]';
                });
            });
        }

        // Test input + play
        var nbeepInput = document.getElementById('nbeep-input');
        var nbeepPlay  = document.getElementById('nbeep-play');
        var nbeepHash  = document.getElementById('nbeep-hash-val');
        function playTestBeep() {
            if (!nbeepInput) return;
            var text = nbeepInput.value.trim() || 'test';
            nbeep(text);
            if (nbeepHash) {
                var hash = nDesignAudio.cyrb53(text.substring(0, 512), nDesignAudio.config.globalSeed);
                nbeepHash.textContent = '0x' + hash.toString(16).toUpperCase().padStart(14, '0');
            }
        }
        if (nbeepPlay) { nbeepPlay.addEventListener('click', function () { if (!flashOutline(nbeepPlay)) return; playTestBeep(); }); }
        if (nbeepInput) { nbeepInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); playTestBeep(); } }); }
    }

})();
