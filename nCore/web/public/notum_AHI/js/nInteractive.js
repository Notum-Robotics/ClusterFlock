/* ──────────────────────────────────────────────────────────────
   nInteractive — Interactive Layout Editor Module  [INTERNAL]
   Copyright © 2026 Notum Robotics. Licensed under the MIT License.

   INTERNAL MODULE — used by notumAHI.render() automatically.
   Do NOT call nInteractive.init() directly from agent code.
   Doing so bypasses AHI's control registry and breaks patch(),
   lock(), read(), badge, and all id-based operations.

   Wraps nDynamic to add a hold‑to‑edit interaction layer with
   drag‑and‑drop reordering, context menus, and per‑control
   lock / mute / resize options.

   DEPENDENCY: nUtils.js and nDynamic.js must be loaded first.

   DIRECT USAGE (demo pages only, not for agents):
     nInteractive.init('#my-container', controlArray, config?);

   INTERACTION MODEL:
     Normal mode — controls work normally (toggle, slider, etc.).
       • Hold any control for 5 s → enter edit mode.

     Edit mode — actions suppressed, grid enters editing state.
       • Drag a control   → ghost + drop zone → release to reorder.
       • Hold 2 s         → context menu (lock / mute / resize / close).
       • Right-click      → context menu (same as hold 2 s).
       • Tap empty space  → exit edit mode.
       • Press Escape     → exit edit mode.

   CONFIG (extends nDynamic config):
     holdEnterMs    Number    — ms to hold for edit mode  (default 5000)
     holdContextMs  Number    — ms to hold for context    (default 2000)
     onEditChange   Function  — callback(isEditing) on mode change
   ────────────────────────────────────────────────────────────── */

var nInteractive = (function () {
    'use strict';

    /* ═══════════════════════════════════
       Constants
       ═══════════════════════════════════ */

    var HOLD_ENTER_MS  = 5000;
    var HOLD_CTX_MS    = 2000;
    var HOLD_CANCEL_PX = 12;
    var DRAG_THRESHOLD = 8;

    /* ═══════════════════════════════════
       Internal State
       ═══════════════════════════════════ */

    var _container     = null;
    var _controls      = [];
    var _config        = {};
    var _editMode      = false;

    /* Hold */
    var _holdTimer     = null;
    var _holdStart     = null;   // { x, y, idx, ctrlEl }

    /* Drag */
    var _dragActive    = false;
    var _dragIdx       = -1;
    var _dragCols      = 1;
    var _dragRows      = 1;
    var _dragGhost     = null;
    var _dragOffsetX   = 0;
    var _dragOffsetY   = 0;
    var _dropIndicator = null;
    var _dropCol       = 0;
    var _dropRow       = 0;
    var _lockedSnap    = {};  // idx → { col, row } snapshot of locked positions at drag start

    /* Cached drag-session bitmap (avoids getComputedStyle per mousemove) */
    var _dragGeo       = null;  // { cols, rowH, gap, pad }
    var _dragBitmap    = null;  // { grid, cols }

    /* Context menu */
    var _ctxEl         = null;

    /* Per-control metadata */
    var _locked        = {};
    var _muted         = {};
    var _beepSuffix    = {};   // idx → '001'–'999'  (hash suffix for nbeep)
    var _beepTheme     = {};   // idx → soundMode key (per-control soundscape)

    /* UI */
    var _editBanner    = null;

    /* Resize handle drag */
    var _resizeActive  = false;
    var _resizeIdx     = -1;
    var _resizeStartW  = 0;
    var _resizeStartH  = 0;
    var _resizeStartX  = 0;
    var _resizeStartY  = 0;
    var _resizeOrigCols = 1;
    var _resizeOrigRows = 1;

    /* Bound handler refs (for removal) */
    var _hDown, _hMove, _hUp, _hCancel, _hKey, _hCtx, _sClick, _sMouse, _sTouch;

    /* ═══════════════════════════════════
       Helpers (delegated to nUtils)
       ═══════════════════════════════════ */

    var escHtml = nUtils.escHtml;

    function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

    function dist(x1, y1, x2, y2) {
        var dx = x2 - x1, dy = y2 - y1;
        return Math.sqrt(dx * dx + dy * dy);
    }

    /** Walk up from el to find the control wrapper tagged with data-ni-idx */
    function findCtrlEl(el) {
        while (el && el !== _container && el !== document.body) {
            if (el.dataset && el.dataset.niIdx !== undefined) return el;
            el = el.parentNode;
        }
        return null;
    }

    /** Check if el belongs to our own floating UI (context menu, banner) */
    function isOwnUI(el) {
        while (el && el !== document.body) {
            if (el === _ctxEl || el === _editBanner) return true;
            el = el.parentNode;
        }
        return false;
    }

    /** Read grid geometry from the container's computed style */
    function getGridGeo() {
        var s    = getComputedStyle(_container);
        var cols = s.gridTemplateColumns ? s.gridTemplateColumns.split(' ').length : 4;
        var rowH = parseInt(s.gridAutoRows) || 80;
        var gap  = parseInt(s.gap) || parseInt(s.gridGap) || 6;
        var pad  = parseInt(s.paddingLeft) || 16;
        return { cols: cols, rowH: rowH, gap: gap, pad: pad };
    }

    /* ═══════════════════════════════════
       Collision Bitmap Utilities
       Prevents any two pinned items from
       occupying the same grid cells.
       ═══════════════════════════════════ */

    /** Check if a rectangle of cells is free in the bitmap */
    function isBitmapFree(grid, col, row, rSpan, cSpan, cols) {
        for (var dr = 0; dr < rSpan; dr++) {
            for (var dc = 0; dc < cSpan; dc++) {
                if (col + dc < 0 || col + dc >= cols) return false;
                if (row + dr < 0) return false;
                if (grid[row + dr] && grid[row + dr][col + dc]) return false;
            }
        }
        return true;
    }

    /** Mark a rectangle of cells as occupied */
    function occupyBitmap(grid, col, row, rSpan, cSpan) {
        for (var dr = 0; dr < rSpan; dr++) {
            if (!grid[row + dr]) grid[row + dr] = [];
            for (var dc = 0; dc < cSpan; dc++) {
                grid[row + dr][col + dc] = true;
            }
        }
    }

    /**
     * Build an occupancy bitmap from all pinned items,
     * optionally excluding one control index (the one being moved).
     */
    function buildPinnedBitmap(excludeIdx) {
        var geo  = getGridGeo();
        var cols = geo.cols;
        var grid = [];

        if (_config.pinned) {
            for (var key in _config.pinned) {
                var idx = parseInt(key, 10);
                if (idx === excludeIdx) continue;
                var pin  = _config.pinned[idx];
                var item = _controls[idx];
                if (!item || !pin) continue;
                var cSpan = Math.min(item.cols || 1, cols);
                var rSpan = item.rows || 1;
                var c     = Math.max(0, Math.min(pin.col || 0, cols - cSpan));
                var r     = Math.max(0, pin.row || 0);
                occupyBitmap(grid, c, r, rSpan, cSpan);
            }
        }
        return { grid: grid, cols: cols };
    }

    /**
     * Find nearest free slot, sorted by true Euclidean distance.
     * Expands search ring-by-ring for efficiency; within each ring
     * all candidates are sorted by distance so the closest cell wins.
     * Returns { col, row } or null.
     */
    function findNearestFreeSlot(grid, targetCol, targetRow, rSpan, cSpan, cols, maxSearch) {
        if (isBitmapFree(grid, targetCol, targetRow, rSpan, cSpan, cols)) {
            return { col: targetCol, row: targetRow };
        }
        for (var radius = 1; radius <= (maxSearch || 30); radius++) {
            /* Collect all perimeter candidates at this radius */
            var candidates = [];
            for (var dr = -radius; dr <= radius; dr++) {
                for (var dc = -radius; dc <= radius; dc++) {
                    if (Math.abs(dr) !== radius && Math.abs(dc) !== radius) continue;
                    var r = targetRow + dr;
                    var c = targetCol + dc;
                    if (r < 0 || c < 0 || c + cSpan > cols) continue;
                    candidates.push({ col: c, row: r, d: dr * dr + dc * dc });
                }
            }
            /* Sort by squared distance (avoids sqrt, same ordering) */
            candidates.sort(function (a, b) { return a.d - b.d; });
            for (var i = 0; i < candidates.length; i++) {
                if (isBitmapFree(grid, candidates[i].col, candidates[i].row, rSpan, cSpan, cols)) {
                    return { col: candidates[i].col, row: candidates[i].row };
                }
            }
        }
        return null;
    }

    /**
     * All card sizes, ordered largest → smallest by total area.
     * Used for both resolvePin shrink and the resize sub-menu.
     */
    var CARD_SIZES = [
        { s: '3x3', c: 3, r: 3, area: 9, label: '3\u00D73  XL' },
        { s: '3x2', c: 3, r: 2, area: 6, label: '3\u00D72  WIDE' },
        { s: '2x3', c: 2, r: 3, area: 6, label: '2\u00D73  TALL' },
        { s: '2x2', c: 2, r: 2, area: 4, label: '2\u00D72  FULL' },
        { s: '3x1', c: 3, r: 1, area: 3, label: '3\u00D71  STRIP' },
        { s: '2x1', c: 2, r: 1, area: 2, label: '2\u00D71  HALF-H' },
        { s: '1x3', c: 1, r: 3, area: 3, label: '1\u00D73  PILLAR' },
        { s: '1x2', c: 1, r: 2, area: 2, label: '1\u00D72  HALF-V' },
        { s: '1x1', c: 1, r: 1, area: 1, label: '1\u00D71  QUARTER' }
    ];

    /**
     * Resolve a valid pin position for a control at its current size.
     * Tries: (1) exact position, (2) nearest free slot.
     * Never auto-resizes — size changes are explicit via the resize sub-menu.
     * Returns { col, row } or null.
     */
    function resolvePin(idx, targetCol, targetRow) {
        var bm   = buildPinnedBitmap(idx);
        var item = _controls[idx];
        if (!item) return null;

        var cols  = bm.cols;
        var cSpan = Math.min(item.cols || 1, cols);
        var rSpan = item.rows || 1;

        /* 1. Try exact position */
        if (isBitmapFree(bm.grid, targetCol, targetRow, rSpan, cSpan, cols)) {
            return { col: targetCol, row: targetRow };
        }

        /* 2. Nearest slot at current size */
        return findNearestFreeSlot(bm.grid, targetCol, targetRow, rSpan, cSpan, cols, 30);
    }

    /**
     * Quick placement check against a pre-built bitmap.
     * Returns { col, row } or null.  No side-effects.
     */
    function findPlacement(grid, targetCol, targetRow, rSpan, cSpan, cols) {
        if (isBitmapFree(grid, targetCol, targetRow, rSpan, cSpan, cols)) {
            return { col: targetCol, row: targetRow };
        }
        return findNearestFreeSlot(grid, targetCol, targetRow, rSpan, cSpan, cols, 30);
    }

    /**
     * Sanitize all pins: clamp to column bounds, resolve overlaps.
     * Locked items get priority; overlapping non-locked items are unpinned.
     */
    function sanitizePins() {
        if (!_config.pinned) return;

        var geo  = getGridGeo();
        var cols = geo.cols;
        var grid = [];
        var clean = {};

        /* Sort keys: locked first (they win ties), then by index */
        var keys = Object.keys(_config.pinned).sort(function (a, b) {
            var al = _locked[parseInt(a, 10)] ? 0 : 1;
            var bl = _locked[parseInt(b, 10)] ? 0 : 1;
            return al - bl || parseInt(a, 10) - parseInt(b, 10);
        });

        for (var i = 0; i < keys.length; i++) {
            var idx  = parseInt(keys[i], 10);
            var pin  = _config.pinned[idx];
            var item = _controls[idx];
            if (!item || !pin) continue;

            var cSpan = Math.min(item.cols || 1, cols);
            var rSpan = item.rows || 1;
            var col   = Math.max(0, Math.min(pin.col || 0, cols - cSpan));
            var row   = Math.max(0, pin.row || 0);

            if (isBitmapFree(grid, col, row, rSpan, cSpan, cols)) {
                occupyBitmap(grid, col, row, rSpan, cSpan);
                clean[idx] = { col: col, row: row };
            } else {
                var nearest = findNearestFreeSlot(grid, col, row, rSpan, cSpan, cols, 30);
                if (nearest) {
                    occupyBitmap(grid, nearest.col, nearest.row, rSpan, cSpan);
                    clean[idx] = nearest;
                }
                /* else: unpin — let CSS auto-flow place it */
            }
        }

        _config.pinned = clean;
    }

    /** Subtle error feedback when placement fails */
    function showPlacementError() {
        if (typeof nbeep === 'function') nbeep('error');
        _container.classList.add('ni-placement-error');
        setTimeout(function () {
            _container.classList.remove('ni-placement-error');
        }, 600);
    }

    /* ═══════════════════════════════════
       Tag Controls with data-ni-idx
       (Run after every nDynamic build)
       ═══════════════════════════════════ */

    function applyIndices() {
        var order    = _config.order;
        var children = _container.children;
        var render   = [];

        if (order && Array.isArray(order)) {
            var used = {};
            for (var j = 0; j < order.length; j++) {
                if (order[j] >= 0 && order[j] < _controls.length) {
                    render.push(order[j]);
                    used[order[j]] = true;
                }
            }
            for (var n = 0; n < _controls.length; n++) {
                if (!used[n]) render.push(n);
            }
        } else {
            for (var i = 0; i < _controls.length; i++) render.push(i);
        }

        var ci = 0;
        for (var k = 0; k < children.length && ci < render.length; k++) {
            var idx = render[ci];
            children[k].dataset.niIdx = idx;
            children[k].classList.toggle('ni-locked', !!_locked[idx]);
            children[k].classList.toggle('ni-muted',  !!_muted[idx]);
            /* Sound customisation data attributes */
            if (_beepSuffix[idx]) children[k].dataset.niBeep  = _beepSuffix[idx];
            else delete children[k].dataset.niBeep;
            if (_beepTheme[idx])  children[k].dataset.niTheme = _beepTheme[idx];
            else delete children[k].dataset.niTheme;
            ci++;
        }
    }

    /* ═══════════════════════════════════
       Edit Mode
       ═══════════════════════════════════ */

    /* ═══════════════════════════════════
       Resize Handles
       In edit mode, controls get a corner
       drag handle for direct-manipulation
       resize (bottom-right corner).
       ═══════════════════════════════════ */

    function attachResizeHandles() {
        if (!_editMode || !_container) return;
        _container.querySelectorAll('[data-ni-idx]').forEach(function (el) {
            if (el.querySelector('.ni-resize-handle')) return;
            var idx = parseInt(el.dataset.niIdx, 10);
            if (_locked[idx]) return;
            var handle = document.createElement('div');
            handle.className = 'ni-resize-handle';
            handle.innerHTML = '<i class="ph ph-resize"></i>';
            handle.dataset.niResize = idx;
            el.appendChild(handle);
        });
    }

    function removeResizeHandles() {
        if (!_container) return;
        _container.querySelectorAll('.ni-resize-handle').forEach(function (h) {
            h.parentNode.removeChild(h);
        });
    }

    function startResize(idx, x, y) {
        var ctrlEl = _container.querySelector('[data-ni-idx="' + idx + '"]');
        if (!ctrlEl) return;
        _resizeActive = true;
        _resizeIdx = idx;
        _resizeStartX = x;
        _resizeStartY = y;
        var rect = ctrlEl.getBoundingClientRect();
        _resizeStartW = rect.width;
        _resizeStartH = rect.height;
        _resizeOrigCols = _controls[idx].cols || 1;
        _resizeOrigRows = _controls[idx].rows || 1;
        ctrlEl.classList.add('ni-resizing');
        if (typeof nbeep === 'function') nbeep('resize_start');
    }

    function updateResize(x, y) {
        if (!_resizeActive) return;
        var geo = getGridGeo();
        var bx = _container.getBoundingClientRect();
        var cellW = (bx.width - geo.pad * 2 - Math.max(0, geo.cols - 1) * geo.gap) / geo.cols;

        var dx = x - _resizeStartX;
        var dy = y - _resizeStartY;

        var newW = _resizeStartW + dx;
        var newH = _resizeStartH + dy;

        var newCols = clamp(Math.round(newW / (cellW + geo.gap)), 1, Math.min(3, geo.cols));
        var newRows = clamp(Math.round(newH / (geo.rowH + geo.gap)), 1, 3);

        if (newCols !== _controls[_resizeIdx].cols || newRows !== _controls[_resizeIdx].rows) {
            _controls[_resizeIdx].cols = newCols;
            _controls[_resizeIdx].rows = newRows;
            _controls[_resizeIdx].size = newCols + 'x' + newRows;

            /* Re-validate pin after resize */
            if (_config.pinned && _config.pinned[_resizeIdx] !== undefined) {
                var oldPin = _config.pinned[_resizeIdx];
                var resolved = resolvePin(_resizeIdx, oldPin.col, oldPin.row);
                if (resolved) _config.pinned[_resizeIdx] = resolved;
                else delete _config.pinned[_resizeIdx];
            }

            rebuildGrid();
            /* Re-attach handles after rebuild */
            setTimeout(attachResizeHandles, 80);
        }
    }

    function endResize() {
        if (!_resizeActive) return;
        _resizeActive = false;
        var el = _container.querySelector('[data-ni-idx="' + _resizeIdx + '"]');
        if (el) el.classList.remove('ni-resizing');
        _resizeIdx = -1;
        if (typeof nbeep === 'function') nbeep('resize');
    }

    function enterEditMode() {
        if (_editMode) return;
        _editMode = true;
        _container.classList.add('ni-edit-mode');
        _container.style.touchAction = 'none';

        if (!_editBanner) {
            _editBanner = document.createElement('div');
            _editBanner.className = 'ni-edit-banner';
            _editBanner.innerHTML =
                '<i class="ph ph-cursor-click"></i> EDIT MODE' +
                '<span class="ni-banner-hint"> — drag to reorder · resize from corners · hold for options · tap empty area or press ESC to exit</span>';
            document.body.appendChild(_editBanner);
        }
        _editBanner.classList.add('visible');

        /* Attach resize handles to all unlocked controls */
        setTimeout(attachResizeHandles, 80);

        if (typeof nbeep === 'function') nbeep('edit_mode');
        if (typeof _config.onEditChange === 'function') _config.onEditChange(true);
    }

    function exitEditMode() {
        if (!_editMode) return;
        _editMode = false;
        _container.classList.remove('ni-edit-mode');
        _container.style.touchAction = '';

        if (_editBanner) _editBanner.classList.remove('visible');
        closeContextMenu();
        cancelDrag();
        endResize();
        removeResizeHandles();

        if (typeof nbeep === 'function') nbeep('exit_edit');
        if (typeof _config.onEditChange === 'function') _config.onEditChange(false);
    }

    /* ═══════════════════════════════════
       Context Menu
       ═══════════════════════════════════ */

    function showContextMenu(idx, x, y) {
        closeContextMenu();
        var item = _controls[idx];
        if (!item) return;

        var menu = document.createElement('div');
        menu.className = 'ni-ctx-menu';
        menu.style.position = 'fixed';
        menu.style.zIndex   = '2000';

        /* Corners + title */
        menu.innerHTML =
            '<span class="corner corner-tl">\u231C</span>' +
            '<span class="corner corner-tr">\u231D</span>' +
            '<span class="corner corner-bl">\u231E</span>' +
            '<span class="corner corner-br">\u231F</span>' +
            '<div class="ni-ctx-title">' +
                escHtml(item.name || item.label || item.type.toUpperCase()) +
            '</div>';

        /* Lock / Unlock */
        var isLocked = !!_locked[idx];
        addMenuItem(menu,
            isLocked ? 'ph-lock-key-open' : 'ph-lock-key',
            isLocked ? 'UNLOCK POSITION'  : 'LOCK POSITION',
            function () {
                if (isLocked) {
                    delete _locked[idx];
                    /* Remove pin so the control reflows normally */
                    if (_config.pinned) delete _config.pinned[idx];
                } else {
                    _locked[idx] = true;
                    /* Collision-safe pin via centre-point snapping */
                    var ctrlEl = _container.querySelector('[data-ni-idx="' + idx + '"]');
                    if (ctrlEl) {
                        var snap = snapElToCell(ctrlEl);
                        if (!_config.pinned) _config.pinned = {};
                        var resolved = resolvePin(idx, snap.col, snap.row);
                        if (resolved) {
                            _config.pinned[idx] = { col: resolved.col, row: resolved.row };
                        } else {
                            /* No valid slot — error feedback, still lock but don't pin */
                            showPlacementError();
                        }
                    }
                }
                closeContextMenu();
                rebuildGrid();
                if (typeof nbeep === 'function') nbeep(isLocked ? 'unlock' : 'lock');
            });

        /* Mute / Unmute */
        var isMuted = !!_muted[idx];
        addMenuItem(menu,
            isMuted ? 'ph-speaker-high' : 'ph-speaker-slash',
            isMuted ? 'UNMUTE SOUNDS'   : 'MUTE SOUNDS',
            function () {
                if (isMuted) delete _muted[idx]; else _muted[idx] = true;
                closeContextMenu();
                refreshVisuals();
                if (typeof nbeep === 'function') nbeep(isMuted ? 'unmute' : 'mute');
            });

        /* ── Soundscape theme selector ── */
        (function () {
            var THEMES = [
                { key: '',         label: 'DEFAULT' },
                { key: 'standard', label: 'STANDARD' },
                { key: 'harmonic', label: 'HARMONIC' },
                { key: 'ncars',    label: 'NCARS' },
                { key: 'ncars2',   label: 'NCARS 2' }
            ];

            var themeHeader = document.createElement('div');
            themeHeader.className = 'ni-ctx-item ni-ctx-resize-header';
            themeHeader.innerHTML = '<i class="ph ph-music-notes"></i> SOUNDSCAPE';
            themeHeader.style.pointerEvents = 'none';
            themeHeader.style.opacity = '0.5';
            menu.appendChild(themeHeader);

            var themeRow = document.createElement('div');
            themeRow.className = 'ni-ctx-size-row';

            var curTheme = _beepTheme[idx] || '';
            THEMES.forEach(function (th) {
                var btn = document.createElement('div');
                btn.className = 'ni-ctx-size-btn' + (th.key === curTheme ? ' active' : '');
                btn.textContent = th.label;
                btn.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    if (th.key) _beepTheme[idx] = th.key;
                    else delete _beepTheme[idx];
                    refreshVisuals();
                    /* Preview the sound with new theme */
                    if (typeof nbeep === 'function' && typeof nDesignAudio !== 'undefined') {
                        var oldMode = nDesignAudio.config.soundMode;
                        if (th.key) nDesignAudio.config.soundMode = th.key;
                        nbeep((_beepSuffix[idx] ? 'preview' + _beepSuffix[idx] : 'preview'));
                        nDesignAudio.config.soundMode = oldMode;
                    }
                    /* Update row active state */
                    themeRow.querySelectorAll('.ni-ctx-size-btn').forEach(function (b) { b.classList.remove('active'); });
                    btn.classList.add('active');
                });
                themeRow.appendChild(btn);
            });
            menu.appendChild(themeRow);
        })();

        /* ── Sound hash suffix selector (001–999) ── */
        (function () {
            var hashHeader = document.createElement('div');
            hashHeader.className = 'ni-ctx-item ni-ctx-resize-header';
            hashHeader.innerHTML = '<i class="ph ph-waveform"></i> SOUND HASH';
            hashHeader.style.pointerEvents = 'none';
            hashHeader.style.opacity = '0.5';
            menu.appendChild(hashHeader);

            var hashRow = document.createElement('div');
            hashRow.className = 'ni-ctx-hash-row';

            var curVal = _beepSuffix[idx] ? parseInt(_beepSuffix[idx], 10) : 0;

            var minusBtn = document.createElement('div');
            minusBtn.className = 'ni-ctx-hash-btn';
            minusBtn.textContent = '\u2212';
            minusBtn.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
            hashRow.appendChild(minusBtn);

            var display = document.createElement('div');
            display.className = 'ni-ctx-hash-val';
            display.textContent = curVal === 0 ? 'OFF' : pad3(curVal);
            hashRow.appendChild(display);

            var plusBtn = document.createElement('div');
            plusBtn.className = 'ni-ctx-hash-btn';
            plusBtn.textContent = '+';
            plusBtn.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
            hashRow.appendChild(plusBtn);

            function pad3(n) { return ('000' + n).slice(-3); }

            function update(dir) {
                curVal += dir;
                if (curVal > 999) curVal = 0;
                if (curVal < 0)   curVal = 999;
                if (curVal === 0) {
                    delete _beepSuffix[idx];
                    display.textContent = 'OFF';
                } else {
                    _beepSuffix[idx] = pad3(curVal);
                    display.textContent = pad3(curVal);
                }
                refreshVisuals();
                /* Preview the beep with current suffix + theme */
                if (typeof nbeep === 'function') {
                    var oldMode;
                    if (_beepTheme[idx] && typeof nDesignAudio !== 'undefined') {
                        oldMode = nDesignAudio.config.soundMode;
                        nDesignAudio.config.soundMode = _beepTheme[idx];
                    }
                    nbeep(curVal ? 'preview' + pad3(curVal) : 'preview');
                    if (oldMode !== undefined) nDesignAudio.config.soundMode = oldMode;
                }
            }

            minusBtn.addEventListener('click', function (e) { e.stopPropagation(); update(-1); });
            plusBtn.addEventListener('click',  function (e) { e.stopPropagation(); update(1);  });

            menu.appendChild(hashRow);
        })();

        /* Resize sub-menu — shows all sizes except current */
        {
            var curSize = (item.cols || 2) + 'x' + (item.rows || 2);
            var geo = getGridGeo();

            /* Header row */
            var resizeHeader = document.createElement('div');
            resizeHeader.className = 'ni-ctx-item ni-ctx-resize-header';
            resizeHeader.innerHTML = '<i class="ph ph-resize"></i> RESIZE';
            resizeHeader.style.pointerEvents = 'none';
            resizeHeader.style.opacity = '0.5';
            menu.appendChild(resizeHeader);

            /* Size option row */
            var sizeRow = document.createElement('div');
            sizeRow.className = 'ni-ctx-size-row';

            for (var si = 0; si < CARD_SIZES.length; si++) {
                (function (sz) {
                    /* Skip if wider than grid allows */
                    if (sz.c > geo.cols) return;

                    var btn = document.createElement('div');
                    btn.className = 'ni-ctx-size-btn' + (sz.s === curSize ? ' active' : '');
                    btn.textContent = sz.s.replace('x', '\u00D7');
                    btn.title = sz.label;
                    btn.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
                    btn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        if (sz.s === curSize) return;
                        var flash = window.flashOutline;
                        if (typeof flash === 'function' && !flash(btn)) return;
                        setTimeout(function () {
                            item.size = sz.s;
                            item.cols = sz.c;
                            item.rows = sz.r;
                            /* Re-validate pin after resize */
                            if (_config.pinned && _config.pinned[idx] !== undefined) {
                                var oldPin = _config.pinned[idx];
                                var resolved = resolvePin(idx, oldPin.col, oldPin.row);
                                if (resolved) {
                                    _config.pinned[idx] = { col: resolved.col, row: resolved.row };
                                } else {
                                    delete _config.pinned[idx];
                                }
                            }
                            closeContextMenu();
                            rebuildGrid();
                            if (typeof nbeep === 'function') nbeep('resize');
                        }, 200);
                    });
                    sizeRow.appendChild(btn);
                })(CARD_SIZES[si]);
            }

            menu.appendChild(sizeRow);
        }

        /* Close */
        addMenuItem(menu, 'ph-x', 'CLOSE', function () { closeContextMenu(); });

        /* Initial position */
        menu.style.left = x + 'px';
        menu.style.top  = y + 'px';
        document.body.appendChild(menu);

        /* Viewport clamp (after paint) */
        requestAnimationFrame(function () {
            var r = menu.getBoundingClientRect();
            if (r.right > window.innerWidth - 8) {
                menu.style.left = Math.max(8, window.innerWidth - r.width - 8) + 'px';
            }
            if (r.bottom > window.innerHeight - 8) {
                menu.style.top = Math.max(8, window.innerHeight - r.height - 8) + 'px';
            }
        });

        _ctxEl = menu;
        if (typeof nbeep === 'function') nbeep('ctx_open');
    }

    function addMenuItem(menu, icon, label, handler) {
        var el = document.createElement('div');
        el.className = 'ni-ctx-item';
        el.innerHTML = '<i class="ph ' + icon + '"></i> ' + escHtml(label);
        /* stop the pointer event from re-triggering hold detection */
        el.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
        el.addEventListener('click', function (e) {
            e.stopPropagation();
            var flash = window.flashOutline;
            if (typeof flash === 'function' && !flash(el)) return;
            setTimeout(handler, 200);
        });
        menu.appendChild(el);
    }

    function closeContextMenu() {
        if (_ctxEl && _ctxEl.parentNode) _ctxEl.parentNode.removeChild(_ctxEl);
        _ctxEl = null;
    }

    function refreshVisuals() {
        var els = _container.querySelectorAll('[data-ni-idx]');
        els.forEach(function (c) {
            var idx = parseInt(c.dataset.niIdx, 10);
            c.classList.toggle('ni-locked', !!_locked[idx]);
            c.classList.toggle('ni-muted',  !!_muted[idx]);
            if (_beepSuffix[idx]) c.dataset.niBeep  = _beepSuffix[idx];
            else delete c.dataset.niBeep;
            if (_beepTheme[idx])  c.dataset.niTheme = _beepTheme[idx];
            else delete c.dataset.niTheme;
        });
    }

    /* ═══════════════════════════════════
       Drag System
       ═══════════════════════════════════ */

    /**
     * Snap an element to its grid cell using centre-point mapping.
     * More robust than edge-based Math.round — immune to sub-pixel drift.
     */
    function snapElToCell(el) {
        var geo   = getGridGeo();
        var bx    = _container.getBoundingClientRect();
        var cellW = (bx.width - geo.pad * 2 - Math.max(0, geo.cols - 1) * geo.gap) / geo.cols;
        var r     = el.getBoundingClientRect();
        /* Use element centre relative to container content box */
        var cx = (r.left + r.width / 2)  - bx.left - geo.pad;
        var cy = (r.top  + r.height / 2) - bx.top  - geo.pad;
        return {
            col: clamp(Math.floor(cx / (cellW + geo.gap)), 0, Math.max(0, geo.cols - 1)),
            row: clamp(Math.floor(cy / (geo.rowH + geo.gap)), 0, 200)
        };
    }

    function startDrag(idx, x, y) {
        if (_locked[idx]) return;
        _dragActive = true;
        _dragIdx    = idx;

        var item  = _controls[idx];
        _dragCols = item.cols || 1;
        _dragRows = item.rows || 1;

        /* Clear stale pins: only locked controls keep their pins */
        if (_config.pinned) {
            for (var pidx in _config.pinned) {
                var pi = parseInt(pidx, 10);
                if (!_locked[pi]) delete _config.pinned[pi];
            }
        }

        /* Snapshot locked positions using centre-point snapping */
        _lockedSnap = {};
        _container.querySelectorAll('[data-ni-idx]').forEach(function (el) {
            var ci = parseInt(el.dataset.niIdx, 10);
            if (_locked[ci]) {
                _lockedSnap[ci] = snapElToCell(el);
            }
        });

        /* Cache bitmap + geo for the duration of this drag (perf) */
        _dragGeo    = getGridGeo();
        _dragBitmap = buildPinnedBitmap(_dragIdx);

        var srcEl = _container.querySelector('[data-ni-idx="' + idx + '"]');
        if (!srcEl) { cancelDrag(); return; }

        var rect     = srcEl.getBoundingClientRect();
        _dragOffsetX = x - rect.left;
        _dragOffsetY = y - rect.top;

        /* Ghost (fixed, follows pointer) */
        _dragGhost = srcEl.cloneNode(true);
        _dragGhost.classList.add('ni-drag-ghost');
        _dragGhost.style.cssText =
            'position:fixed;z-index:3000;pointer-events:none;' +
            'width:' + rect.width + 'px;height:' + rect.height + 'px;' +
            'left:'  + rect.left  + 'px;top:'    + rect.top    + 'px;';
        document.body.appendChild(_dragGhost);

        /* Dim source */
        srcEl.classList.add('ni-drag-source');

        /* Drop indicator (fixed, snaps to grid) */
        _dropIndicator = document.createElement('div');
        _dropIndicator.className = 'ni-drop-indicator';
        _dropIndicator.style.cssText = 'position:fixed;z-index:2500;display:none;';
        document.body.appendChild(_dropIndicator);

        if (typeof nbeep === 'function') nbeep('drag_start');
    }

    function updateDrag(x, y) {
        if (!_dragGhost) return;

        _dragGhost.style.left = (x - _dragOffsetX) + 'px';
        _dragGhost.style.top  = (y - _dragOffsetY) + 'px';

        if (!_dropIndicator || !_dragGeo) return;

        /* Use cached geo — avoids getComputedStyle per mousemove */
        var geo = _dragGeo;
        var bx  = _container.getBoundingClientRect();

        var cellW = (bx.width - geo.pad * 2 - Math.max(0, geo.cols - 1) * geo.gap) / geo.cols;
        var relX  = x - bx.left - geo.pad;
        var relY  = y - bx.top  - geo.pad;
        var cursorCol = clamp(Math.floor(relX / (cellW + geo.gap)), 0, Math.max(0, geo.cols - _dragCols));
        var cursorRow = clamp(Math.floor(relY / (geo.rowH + geo.gap)), 0, 100);

        /* Resolve actual placement — indicator shows where the item WILL land */
        var valid   = false;
        var showCol = cursorCol;
        var showRow = cursorRow;

        if (_dragBitmap) {
            var resolved = findPlacement(
                _dragBitmap.grid, cursorCol, cursorRow,
                _dragRows, _dragCols, _dragBitmap.cols
            );
            if (resolved) {
                showCol = resolved.col;
                showRow = resolved.row;
                valid   = true;
            }
        } else {
            valid = true;
        }

        _dropCol = showCol;
        _dropRow = showRow;

        var w = _dragCols * cellW + Math.max(0, _dragCols - 1) * geo.gap;
        var h = _dragRows * geo.rowH + Math.max(0, _dragRows - 1) * geo.gap;

        _dropIndicator.style.left    = (bx.left + geo.pad + showCol * (cellW + geo.gap)) + 'px';
        _dropIndicator.style.top     = (bx.top  + geo.pad + showRow * (geo.rowH + geo.gap)) + 'px';
        _dropIndicator.style.width   = w + 'px';
        _dropIndicator.style.height  = h + 'px';
        _dropIndicator.style.display = 'block';

        _dropIndicator.classList.toggle('ni-drop-invalid', !valid);
    }

    function endDrag(x, y) {
        if (!_dragActive) return;

        if (!_config.pinned) _config.pinned = {};

        /* Re-pin all locked elements to their pre-drag positions first */
        for (var li in _lockedSnap) {
            var lockIdx = parseInt(li, 10);
            _config.pinned[lockIdx] = _lockedSnap[lockIdx];
        }

        /* Resolve placement with collision detection (never auto-resizes) */
        var placement = resolvePin(_dragIdx, _dropCol, _dropRow);

        if (placement) {
            _config.pinned[_dragIdx] = { col: placement.col, row: placement.row };
            cleanupDrag();
            rebuildGrid();
            if (typeof nbeep === 'function') nbeep('drag_end');
        } else {
            /* No valid placement — subtle error, cancel the drop */
            showPlacementError();
            cleanupDrag();
            rebuildGrid();
        }
    }

    function cancelDrag() { cleanupDrag(); }

    function cleanupDrag() {
        _dragActive  = false;
        _dragIdx     = -1;
        _dropCol     = 0;
        _dropRow     = 0;
        _lockedSnap  = {};
        _dragGeo     = null;
        _dragBitmap  = null;
        if (_dragGhost && _dragGhost.parentNode) _dragGhost.parentNode.removeChild(_dragGhost);
        _dragGhost = null;
        if (_dropIndicator && _dropIndicator.parentNode) _dropIndicator.parentNode.removeChild(_dropIndicator);
        _dropIndicator = null;
        var src = _container.querySelector('.ni-drag-source');
        if (src) src.classList.remove('ni-drag-source');
    }

    /* ═══════════════════════════════════
       Pointer Event Handlers
       ═══════════════════════════════════ */

    /** Right-click handler — in edit mode, show our context menu instead of the browser's */
    function handleContextMenu(e) {
        if (!_editMode) return;             // normal mode: let the browser handle it
        if (isOwnUI(e.target)) return;      // our own UI: ignore

        var ctrlEl = findCtrlEl(e.target);
        var idx    = ctrlEl ? parseInt(ctrlEl.dataset.niIdx, 10) : -1;
        if (idx < 0) return;                // not on a control: ignore

        e.preventDefault();
        e.stopPropagation();

        /* Cancel any running hold timer so we don't double-fire */
        if (_holdTimer) { clearTimeout(_holdTimer); _holdTimer = null; }
        _holdStart = null;

        showContextMenu(idx, e.clientX, e.clientY);
    }

    function handlePointerDown(e) {
        /* Dismiss context menu on outside click */
        if (_ctxEl && !isOwnUI(e.target)) closeContextMenu();

        /* Ignore clicks on our floating UI */
        if (isOwnUI(e.target)) return;

        var ctrlEl = findCtrlEl(e.target);
        var idx    = ctrlEl ? parseInt(ctrlEl.dataset.niIdx, 10) : -1;

        /* Empty space in edit mode → exit */
        if (idx < 0) {
            if (_editMode) exitEditMode();
            return;
        }

        /* Record hold start */
        _holdStart = { x: e.clientX, y: e.clientY, idx: idx, ctrlEl: ctrlEl };

        /* Check if we hit a resize handle */
        if (_editMode) {
            var resizeHandle = e.target.closest && e.target.closest('.ni-resize-handle');
            if (resizeHandle) {
                var rIdx = parseInt(resizeHandle.dataset.niResize, 10);
                startResize(rIdx, e.clientX, e.clientY);
                _holdStart = null;
                e.preventDefault();
                return;
            }
        }

        var duration = _editMode ? HOLD_CTX_MS : HOLD_ENTER_MS;

        _holdTimer = setTimeout(function () {
            _holdTimer = null;
            if (_editMode) {
                showContextMenu(idx, _holdStart.x, _holdStart.y);
            } else {
                enterEditMode();
            }
            _holdStart = null;
        }, duration);

        if (_editMode) e.preventDefault();
    }

    function handlePointerMove(e) {
        if (!_holdStart && !_dragActive && !_resizeActive) return;

        if (_resizeActive) {
            updateResize(e.clientX, e.clientY);
            e.preventDefault();
            return;
        }

        if (_holdStart) {
            var moved = dist(e.clientX, e.clientY, _holdStart.x, _holdStart.y);

            if (_editMode && moved > DRAG_THRESHOLD) {
                /* Cancel hold & start drag */
                if (_holdTimer) { clearTimeout(_holdTimer); _holdTimer = null; }
                if (!_dragActive) {
                    startDrag(_holdStart.idx, _holdStart.x, _holdStart.y);
                    _holdStart = null;
                }
            } else if (!_editMode && moved > HOLD_CANCEL_PX) {
                /* Cancel hold in normal mode (finger/cursor drifted) */
                if (_holdTimer) { clearTimeout(_holdTimer); _holdTimer = null; }
                _holdStart = null;
            }
        }

        if (_dragActive) {
            updateDrag(e.clientX, e.clientY);
            e.preventDefault();
        }
    }

    function handlePointerUp(e) {
        if (_holdTimer) { clearTimeout(_holdTimer); _holdTimer = null; }
        _holdStart = null;
        if (_resizeActive) { endResize(); return; }
        if (_dragActive) endDrag(e.clientX, e.clientY);
    }

    function handlePointerCancel() {
        if (_holdTimer) { clearTimeout(_holdTimer); _holdTimer = null; }
        _holdStart = null;
        if (_resizeActive) endResize();
        cancelDrag();
    }

    function handleKeyDown(e) {
        if (e.key === 'Escape') {
            if (_ctxEl)       closeContextMenu();
            else if (_editMode) exitEditMode();
        }
    }

    /* ═══════════════════════════════════
       Event Suppression (Edit Mode)
       On capturing phase we stop native clicks/taps
       from reaching nDynamic's event handlers.
       ═══════════════════════════════════ */

    function suppressInEdit(e) {
        if (!_editMode) return;
        if (isOwnUI(e.target)) return;
        e.stopPropagation();
        e.preventDefault();
    }

    /* ═══════════════════════════════════
       Grid Rebuild (wraps nDynamic)
       ═══════════════════════════════════ */

    function rebuildGrid() {
        sanitizePins();
        nDynamic.update(_controls, _config);
        setTimeout(function () {
            applyIndices();
            if (_editMode) attachResizeHandles();
        }, 60);
    }

    /* ═══════════════════════════════════
       Init / Destroy
       ═══════════════════════════════════ */

    function init(selector, controls, config) {
        _container = typeof selector === 'string'
            ? document.querySelector(selector) : selector;

        if (!_container) {
            console.error('[nInteractive] Container not found:', selector);
            return;
        }

        _controls = controls || [];
        _config   = config   || {};
        _editMode = false;
        _locked   = {};
        _muted    = {};

        /* Override defaults from config (use defaults if keys not present) */
        HOLD_ENTER_MS = _config.holdEnterMs   || 5000;
        HOLD_CTX_MS   = _config.holdContextMs || 2000;

        /* Delegate to nDynamic for initial render */
        nDynamic.init(_container, _controls, _config);

        /* Tag controls after layout settles */
        setTimeout(applyIndices, 100);

        /* Ensure container is a positioning context */
        if (getComputedStyle(_container).position === 'static') {
            _container.style.position = 'relative';
        }

        /* === Bind events === */

        _hDown   = handlePointerDown;
        _hMove   = handlePointerMove;
        _hUp     = handlePointerUp;
        _hCancel = handlePointerCancel;
        _hKey    = handleKeyDown;
        _hCtx    = handleContextMenu;
        _sClick  = suppressInEdit;
        _sMouse  = suppressInEdit;
        _sTouch  = suppressInEdit;

        _container.addEventListener('pointerdown',  _hDown, { passive: false });
        _container.addEventListener('contextmenu',  _hCtx,  { passive: false });
        document.addEventListener('pointermove',    _hMove, { passive: false });
        document.addEventListener('pointerup',      _hUp);
        document.addEventListener('pointercancel',  _hCancel);
        document.addEventListener('keydown',        _hKey);

        /* Capturing-phase suppression of nDynamic handlers in edit mode */
        _container.addEventListener('click',      _sClick,  true);
        _container.addEventListener('mousedown',  _sMouse,  true);
        _container.addEventListener('touchstart', _sTouch,  true);
    }

    function destroy() {
        exitEditMode();
        cancelDrag();
        closeContextMenu();

        if (_editBanner && _editBanner.parentNode) {
            _editBanner.parentNode.removeChild(_editBanner);
            _editBanner = null;
        }

        if (_container) {
            _container.removeEventListener('pointerdown',  _hDown);
            _container.removeEventListener('contextmenu',  _hCtx);
            _container.removeEventListener('click',        _sClick,  true);
            _container.removeEventListener('mousedown',    _sMouse,  true);
            _container.removeEventListener('touchstart',   _sTouch,  true);
        }

        document.removeEventListener('pointermove',   _hMove);
        document.removeEventListener('pointerup',     _hUp);
        document.removeEventListener('pointercancel', _hCancel);
        document.removeEventListener('keydown',       _hKey);

        nDynamic.destroy();
        _container = null;
        _controls  = [];
        _config    = {};
    }

    /* ═══════════════════════════════════
       Public API
       ═══════════════════════════════════ */

    return {
        init:      init,
        destroy:   destroy,
        isEditing: function () { return _editMode; },
        enterEdit: enterEditMode,
        exitEdit:  exitEditMode,
        rebuild:   rebuildGrid,
        update:    function (controls, config) {
            if (controls) _controls = controls;
            if (config) { for (var k in config) _config[k] = config[k]; }
            rebuildGrid();
        },

        /**
         * getState() — Serialize the full interactive state.
         * Returns { controls, config, locked, muted, beepSuffix, beepTheme }.
         * Useful for saving/restoring layouts, or for agent introspection.
         */
        getState: function () {
            return {
                controls:   _controls.slice(),
                config:     JSON.parse(JSON.stringify(_config)),
                locked:     JSON.parse(JSON.stringify(_locked)),
                muted:      JSON.parse(JSON.stringify(_muted)),
                beepSuffix: JSON.parse(JSON.stringify(_beepSuffix)),
                beepTheme:  JSON.parse(JSON.stringify(_beepTheme))
            };
        },

        /**
         * restoreState(state) — Restore a previously-saved state.
         * @param {Object} state — from getState()
         */
        restoreState: function (state) {
            if (!state) return;
            if (state.controls) _controls = state.controls;
            if (state.config)   _config   = state.config;
            if (state.locked)   _locked   = state.locked;
            if (state.muted)    _muted    = state.muted;
            if (state.beepSuffix) _beepSuffix = state.beepSuffix;
            if (state.beepTheme)  _beepTheme  = state.beepTheme;
            rebuildGrid();
        },

        /**
         * lockControl(idx) / unlockControl(idx) — Programmatic lock/unlock.
         */
        lockControl: function (idx) {
            _locked[idx] = true;
            var ctrlEl = _container && _container.querySelector('[data-ni-idx="' + idx + '"]');
            if (ctrlEl) {
                var snap = snapElToCell(ctrlEl);
                if (!_config.pinned) _config.pinned = {};
                var resolved = resolvePin(idx, snap.col, snap.row);
                if (resolved) _config.pinned[idx] = resolved;
            }
            rebuildGrid();
        },

        unlockControl: function (idx) {
            delete _locked[idx];
            if (_config.pinned) delete _config.pinned[idx];
            rebuildGrid();
        },

        /**
         * muteControl(idx) / unmuteControl(idx) — Programmatic mute/unmute.
         */
        muteControl: function (idx) {
            _muted[idx] = true;
            refreshVisuals();
        },

        unmuteControl: function (idx) {
            delete _muted[idx];
            refreshVisuals();
        },

        /**
         * resizeControl(idx, cols, rows) — Programmatic resize.
         */
        resizeControl: function (idx, cols, rows) {
            if (!_controls[idx]) return;
            _controls[idx].cols = clamp(cols, 1, 3);
            _controls[idx].rows = clamp(rows, 1, 3);
            _controls[idx].size = _controls[idx].cols + 'x' + _controls[idx].rows;
            if (_config.pinned && _config.pinned[idx] !== undefined) {
                var oldPin = _config.pinned[idx];
                var resolved = resolvePin(idx, oldPin.col, oldPin.row);
                if (resolved) _config.pinned[idx] = resolved;
                else delete _config.pinned[idx];
            }
            rebuildGrid();
        }
    };

})();
