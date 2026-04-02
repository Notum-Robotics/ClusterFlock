/* ──────────────────────────────────────────────────────────────
   nStore — Reactive State Store for Notum AHI
   Copyright © 2026 Notum Robotics. Licensed under the MIT License.

   Observable state container with path-based access and
   automatic UI re-rendering.  Designed so agents can set
   state declaratively and the UI updates without imperative
   bookkeeping.

   USAGE:
     // Create a store bound to a grid engine
     var store = nStore.create({
       controls: [ ... ],
       config:   { cols: 4 }
     });

     // Bind to nDynamic or nInteractive (auto-renders on change)
     store.bind('#my-container', 'nInteractive');  // or 'nDynamic'

     // Declarative state changes → automatic UI update
     store.set('controls[0].state', 'off');
     store.set('config.cols', 6);
     store.set('controls[2].value', 18);

     // Batch multiple changes (single re-render)
     store.batch(function () {
       store.set('controls[0].state', 'on');
       store.set('controls[1].value', 5);
       store.set('config.cols', 4);
     });

     // Subscribe to changes
     var unsub = store.subscribe(function (path, value, oldValue) {
       console.log(path, ':', oldValue, '→', value);
     });
     unsub();  // unsubscribe

     // Read state
     store.get('controls[0].state');  // → 'off'
     store.getAll();                  // → full state object

     // Merge partial state (shallow merge at top level)
     store.merge({ config: { cols: 6 } });

   DEPENDENCY: None required.  Optional: nDynamic.js, nInteractive.js
   GLOBAL: window.nStore
   ────────────────────────────────────────────────────────────── */

var nStore = (function () {
    'use strict';

    /* ═══════════════════════════════════
       Path Utilities
       ═══════════════════════════════════ */

    /**
     * Parse a path string into an array of keys.
     * Supports dot notation and bracket indexing:
     *   'controls[0].state' → ['controls', 0, 'state']
     *   'config.cols'       → ['config', 'cols']
     */
    function parsePath(path) {
        if (Array.isArray(path)) return path;
        var parts = [];
        var re = /([^.\[\]]+)|\[(\d+)\]/g;
        var match;
        while ((match = re.exec(path)) !== null) {
            if (match[2] !== undefined) {
                parts.push(parseInt(match[2], 10));
            } else {
                parts.push(match[1]);
            }
        }
        return parts;
    }

    /** Get a nested value from an object by path */
    function getByPath(obj, parts) {
        var cur = obj;
        for (var i = 0; i < parts.length; i++) {
            if (cur === null || cur === undefined) return undefined;
            cur = cur[parts[i]];
        }
        return cur;
    }

    /** Set a nested value on an object by path, creating intermediates */
    function setByPath(obj, parts, value) {
        var cur = obj;
        for (var i = 0; i < parts.length - 1; i++) {
            var key = parts[i];
            var nextKey = parts[i + 1];
            if (cur[key] === undefined || cur[key] === null) {
                cur[key] = (typeof nextKey === 'number') ? [] : {};
            }
            cur = cur[key];
        }
        var lastKey = parts[parts.length - 1];
        var oldValue = cur[lastKey];
        cur[lastKey] = value;
        return oldValue;
    }

    /** Deep clone (JSON-safe) */
    function deepClone(obj) {
        if (obj === null || typeof obj !== 'object') return obj;
        try {
            return JSON.parse(JSON.stringify(obj));
        } catch (_) {
            return obj;
        }
    }

    /* ═══════════════════════════════════
       Store Factory
       ═══════════════════════════════════ */

    function create(initialState) {
        var _state       = deepClone(initialState || {});
        var _subscribers = [];
        var _batching    = false;
        var _dirty       = false;
        var _changes     = [];     // buffered changes during batch
        var _engine      = null;   // 'nDynamic' | 'nInteractive'
        var _container   = null;
        var _renderTimer = null;

        /** Schedule a render (debounced to next frame) */
        function scheduleRender() {
            if (_renderTimer) return;
            _renderTimer = requestAnimationFrame(function () {
                _renderTimer = null;
                doRender();
            });
        }

        /** Perform the actual grid render */
        function doRender() {
            if (!_container || !_engine) return;

            var controls = _state.controls || [];
            var config   = _state.config   || {};

            if (_engine === 'nInteractive' && typeof nInteractive !== 'undefined') {
                nInteractive.update(controls, config);
            } else if (typeof nDynamic !== 'undefined') {
                nDynamic.update(controls, config);
            }
        }

        /** Notify subscribers */
        function notify(path, value, oldValue) {
            for (var i = 0; i < _subscribers.length; i++) {
                try {
                    _subscribers[i](path, value, oldValue);
                } catch (e) {
                    console.error('[nStore] Subscriber error:', e);
                }
            }
        }

        var store = {
            /**
             * Get a value by path.
             * @param {string} [path] — omit to get the entire state
             * @returns {*}
             */
            get: function (path) {
                if (!path) return deepClone(_state);
                return getByPath(_state, parsePath(path));
            },

            /**
             * Alias for get() with no args — returns full state.
             */
            getAll: function () {
                return deepClone(_state);
            },

            /**
             * Set a value by path.  Triggers re-render and subscriber notification.
             *
             * @param {string} path    Dot/bracket path (e.g. 'controls[0].state')
             * @param {*}      value   New value
             */
            set: function (path, value) {
                var parts    = parsePath(path);
                var oldValue = setByPath(_state, parts, value);

                if (_batching) {
                    _dirty = true;
                    _changes.push({ path: path, value: value, oldValue: oldValue });
                } else {
                    notify(path, value, oldValue);
                    scheduleRender();
                }
            },

            /**
             * Batch multiple set() calls into a single render.
             * Subscriber notifications are still fired per-set.
             *
             * @param {Function} fn — function containing set() calls
             */
            batch: function (fn) {
                _batching = true;
                _dirty = false;
                _changes = [];

                try {
                    fn();
                } finally {
                    _batching = false;

                    // Fire deferred notifications
                    for (var i = 0; i < _changes.length; i++) {
                        var c = _changes[i];
                        notify(c.path, c.value, c.oldValue);
                    }
                    _changes = [];

                    if (_dirty) {
                        _dirty = false;
                        scheduleRender();
                    }
                }
            },

            /**
             * Shallow-merge an object into the state.
             * Commonly used for partial config updates.
             *
             * @param {Object} partial — keys to merge at top level
             */
            merge: function (partial) {
                if (!partial || typeof partial !== 'object') return;
                var paths = Object.keys(partial);
                if (paths.length > 1) {
                    store.batch(function () {
                        for (var i = 0; i < paths.length; i++) {
                            store.set(paths[i], partial[paths[i]]);
                        }
                    });
                } else if (paths.length === 1) {
                    store.set(paths[0], partial[paths[0]]);
                }
            },

            /**
             * Replace the entire state.  Triggers re-render.
             * @param {Object} newState
             */
            reset: function (newState) {
                var oldState = _state;
                _state = deepClone(newState || {});
                notify('*', _state, oldState);
                scheduleRender();
            },

            /**
             * Subscribe to state changes.
             * @param {Function} fn — callback(path, value, oldValue)
             * @returns {Function} Unsubscribe function
             */
            subscribe: function (fn) {
                _subscribers.push(fn);
                return function () {
                    var idx = _subscribers.indexOf(fn);
                    if (idx >= 0) _subscribers.splice(idx, 1);
                };
            },

            /**
             * Bind the store to a grid container.
             * State changes will auto-render into this container.
             *
             * @param {string|Element} selector  Container selector or element
             * @param {string}         engine    'nDynamic' or 'nInteractive' (default)
             */
            bind: function (selector, engine) {
                _engine = engine || 'nInteractive';

                if (typeof selector === 'string') {
                    _container = document.querySelector(selector);
                } else {
                    _container = selector;
                }

                if (!_container) {
                    console.error('[nStore] Container not found:', selector);
                    return store;
                }

                // Initial render
                var controls = _state.controls || [];
                var config   = _state.config   || {};

                if (_engine === 'nInteractive' && typeof nInteractive !== 'undefined') {
                    nInteractive.init(_container, controls, config);
                } else if (typeof nDynamic !== 'undefined') {
                    nDynamic.init(_container, controls, config);
                }

                return store;
            },

            /**
             * Destroy the binding and clean up.
             */
            destroy: function () {
                if (_renderTimer) {
                    cancelAnimationFrame(_renderTimer);
                    _renderTimer = null;
                }
                if (_engine === 'nInteractive' && typeof nInteractive !== 'undefined') {
                    nInteractive.destroy();
                } else if (typeof nDynamic !== 'undefined') {
                    nDynamic.destroy();
                }
                _subscribers = [];
                _container   = null;
                _engine      = null;
            },

            /**
             * Convenience: patch a single control by index.
             * @param {number} index    Control index
             * @param {Object} partial  Fields to merge
             */
            patchControl: function (index, partial) {
                var controls = _state.controls;
                if (!controls || !controls[index]) return;
                var keys = Object.keys(partial);
                if (keys.length === 0) return;

                store.batch(function () {
                    for (var i = 0; i < keys.length; i++) {
                        store.set('controls[' + index + '].' + keys[i], partial[keys[i]]);
                    }
                });
            },

            /**
             * Convenience: find a control by its name/label/id and patch it.
             * @param {string} name    Matches control.name, control.label, or control.id
             * @param {Object} partial Fields to merge
             */
            patchControlByName: function (name, partial) {
                var controls = _state.controls || [];
                for (var i = 0; i < controls.length; i++) {
                    var c = controls[i];
                    if (c.name === name || c.label === name || c.id === name) {
                        store.patchControl(i, partial);
                        return;
                    }
                }
                console.warn('[nStore] Control not found:', name);
            }
        };

        return store;
    }

    /* ═══════════════════════════════════
       Public API
       ═══════════════════════════════════ */

    return {
        create: create,
        version: '1.0.0'
    };

})();
