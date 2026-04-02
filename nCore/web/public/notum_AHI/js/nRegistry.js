/* ──────────────────────────────────────────────────────────────
   nRegistry — Module Dependency System for Notum AHI
   Copyright © 2026 Notum Robotics. Licensed under the MIT License.

   Lightweight module registry with dependency validation and
   error reporting.  Replaces silent failures from load-order
   mistakes with clear console errors and a dependency graph
   that modules can query at runtime.

   USAGE:
     // Register a module (factory receives resolved deps as arguments):
     Notum.register('nUtils', [], function () { ... return api; });
     Notum.register('nDynamic', ['nUtils'], function (nUtils) { ... });

     // Retrieve a module:
     var utils = Notum.require('nUtils');

     // Check what's loaded:
     Notum.list();          // → ['nUtils', 'nDynamic', ...]
     Notum.has('nUtils');   // → true

   BACKWARD COMPAT:
     Registered modules are also assigned to window[name] so
     existing code relying on globals continues to work without
     changes.  Legacy globals already on window are auto-adopted
     as "legacy" modules (no deps, no factory) the first time
     another module declares them as a dependency.

   GLOBAL: window.Notum
   ────────────────────────────────────────────────────────────── */

var Notum = (function () {
    'use strict';

    /* ═══════════════════════════════════
       Internal State
       ═══════════════════════════════════ */

    var _modules  = {};   // name → { api, deps[] }
    var _pending  = [];   // deferred registrations waiting on deps
    var _loadOrder = [];  // names in resolution order

    /* ═══════════════════════════════════
       Helpers
       ═══════════════════════════════════ */

    var _badge = ['color:#00e5ff;font-weight:bold', 'color:inherit'];

    function _log(level, msg) {
        console[level]('%c[Notum]%c ' + msg, _badge[0], _badge[1]);
    }

    /** Return dep names not yet in _modules */
    function _missing(deps) {
        var out = [];
        for (var i = 0; i < deps.length; i++) {
            if (!_modules[deps[i]]) out.push(deps[i]);
        }
        return out;
    }

    /** Store a module — single point for _modules + _loadOrder + window */
    function _store(name, moduleApi, deps) {
        _modules[name] = { api: moduleApi, deps: deps || [] };
        _loadOrder.push(name);
        if (moduleApi != null) window[name] = moduleApi;
    }

    /** Adopt a legacy global from window (if present and not already known) */
    function _adopt(name) {
        if (_modules[name] || window[name] === undefined) return false;
        _store(name, window[name], []);
        _log('log', name + ' adopted from window (legacy global)');
        return true;
    }

    /** Try to flush pending registrations whose deps are now met */
    function _flush() {
        var progressed = true;
        while (progressed) {
            progressed = false;
            for (var i = _pending.length - 1; i >= 0; i--) {
                var e = _pending[i];
                if (_missing(e.deps).length === 0) {
                    _pending.splice(i, 1);
                    _resolve(e.name, e.deps, e.factory);
                    progressed = true;
                }
            }
        }
    }

    /** Run factory, store result */
    function _resolve(name, deps, factory) {
        var args = [];
        for (var i = 0; i < deps.length; i++) args.push(_modules[deps[i]].api);

        var result;
        try { result = factory.apply(null, args); }
        catch (e) { _log('error', 'Factory for "' + name + '" threw: ' + (e.message || e)); return; }

        _store(name, result, deps);
        _log('log', name + ' registered' + (deps.length ? ' (deps: ' + deps.join(', ') + ')' : ''));
    }

    /* ═══════════════════════════════════
       Public API
       ═══════════════════════════════════ */

    var api = {

        /** Register a module. Factory receives resolved dep APIs as arguments. */
        register: function (name, deps, factory) {
            if (_modules[name]) {
                _log('warn', '"' + name + '" already registered — skipping');
                return;
            }

            deps = deps || [];
            for (var i = 0; i < deps.length; i++) _adopt(deps[i]);

            var unmet = _missing(deps);
            if (unmet.length === 0) {
                _resolve(name, deps, factory);
                _flush();
            } else {
                _log('warn', '"' + name + '" deferred — waiting on: ' + unmet.join(', '));
                _pending.push({ name: name, deps: deps, factory: factory });
            }
        },

        /** Retrieve a module API (auto-adopts legacy globals). */
        require: function (name) {
            if (_modules[name]) return _modules[name].api;
            if (_adopt(name))   return _modules[name].api;
            _log('error', '"' + name + '" not found. Loaded: ' + _loadOrder.join(', '));
            return undefined;
        },

        /** True if module is registered or exists on window. */
        has: function (name) {
            return !!_modules[name] || window[name] !== undefined;
        },

        /** Module names in load order. */
        list: function () {
            return _loadOrder.slice();
        },

        /** Pending modules with their unresolved deps. */
        pending: function () {
            return _pending.map(function (e) {
                return { name: e.name, waiting: _missing(e.deps) };
            });
        },

        /** Log a health report to the console. */
        status: function () {
            _log('log', '─── Module Status ───');
            _log('log', 'Loaded (' + _loadOrder.length + '): ' + (_loadOrder.join(', ') || '(none)'));
            var p = api.pending();
            if (p.length) {
                _log('warn', 'Pending (' + p.length + '):');
                for (var i = 0; i < p.length; i++) {
                    _log('warn', '  ' + p[i].name + ' → needs: ' + p[i].waiting.join(', '));
                }
            } else {
                _log('log', 'No pending modules');
            }
        },

        version: '1.0.0'
    };

    _store('Notum', api, []);
    return api;
})();
