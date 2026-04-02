/* ──────────────────────────────────────────────────────────────
   Notum AHI Protocol — JSON-RPC Message Layer
   Copyright © 2026 Notum Robotics (n-r.hr). Licensed under the MIT License.

   Transport-agnostic JSON-RPC 2.0 message handler for Notum AHI.
   Deserializes incoming JSON messages into notumAHI method calls
   and serializes results/events back as JSON responses.

   DEPENDENCY: ahi.js must be loaded first.

   This module does NOT handle transport (WebSocket, PostMessage,
   HTTP, etc.) — see ahi-transport-ws.js for WebSocket binding.

   PROTOCOL:
     Request  → { "jsonrpc": "2.0", "method": "render", "params": {...}, "id": 1 }
     Response ← { "jsonrpc": "2.0", "result": {...}, "id": 1 }
     Event    ← { "jsonrpc": "2.0", "method": "event", "params": {...} }
     Error    ← { "jsonrpc": "2.0", "error": { "code": -32600, "message": "..." }, "id": 1 }
   ────────────────────────────────────────────────────────────── */

var notumAHIProtocol = (function () {
    'use strict';

    /* ═══════════════════════════════════
       Constants
       ═══════════════════════════════════ */

    var JSONRPC = '2.0';

    /* Standard JSON-RPC error codes */
    var ERR_PARSE      = -32700;
    var ERR_INVALID    = -32600;
    var ERR_NOT_FOUND  = -32601;
    var ERR_PARAMS     = -32602;
    var ERR_INTERNAL   = -32603;

    /* ═══════════════════════════════════
       Internal State
       ═══════════════════════════════════ */

    var _sendFn         = null;   // function(jsonString) — set by transport
    var _eventUnsub     = null;   // unsubscribe handle for notumAHI.onEvent

    /* ═══════════════════════════════════
       Method Registry
       Maps method names → handler functions.
       Each handler receives (params) and returns
       a value or a Promise.
       ═══════════════════════════════════ */

    var METHODS = {

        /* ── Core ── */

        'render': function (params) {
            notumAHI.render(params.controls || [], params.config || null);
            return { ok: true, count: (params.controls || []).length };
        },

        'patch': function (params) {
            if (!params.id && params.index === undefined) {
                throw { code: ERR_PARAMS, message: 'patch requires "id" or "index"' };
            }
            notumAHI.patch(params.id || params.index, params.changes || {});
            return { ok: true };
        },

        'insert': function (params) {
            notumAHI.insert(
                params.index !== undefined ? params.index : -1,
                params.control
            );
            return { ok: true };
        },

        'remove': function (params) {
            if (!params.id && params.index === undefined) {
                throw { code: ERR_PARAMS, message: 'remove requires "id" or "index"' };
            }
            notumAHI.remove(params.id || params.index);
            return { ok: true };
        },

        /* ── Dialogs ── */

        'dialog': function (params) {
            /* Returns a Promise — protocol will await it */
            return notumAHI.dialog({
                title:   params.title || '',
                body:    params.body || '',
                buttons: params.buttons || []
            });
        },

        'dismiss': function (params) {
            notumAHI.dismiss(params ? params.value : null);
            return { ok: true };
        },

        /* ── Control State ── */

        'lock': function (params) {
            notumAHI.lock(params.id || params.index);
            return { ok: true };
        },

        'unlock': function (params) {
            notumAHI.unlock(params.id || params.index);
            return { ok: true };
        },

        'read': function (params) {
            if (params && (params.id || params.index !== undefined)) {
                return notumAHI.read(params.id || params.index);
            }
            return notumAHI.read();
        },

        /* ── Notifications ── */

        'toast': function (params) {
            notumAHI.toast(
                params.message || '',
                params.level   || 'info',
                params.duration || undefined
            );
            return { ok: true };
        },

        'notify': function (params) {
            notumAHI.notify({
                icon:     params.icon     || undefined,
                title:    params.title    || '',
                subtitle: params.subtitle || '',
                linger:   params.linger   || undefined
            });
            return { ok: true };
        },

        /* ── Flow (if ahi-flow.js loaded) ── */

        'flow': function (params) {
            if (!notumAHI.flow) {
                throw { code: ERR_NOT_FOUND, message: 'flow engine not loaded (include ahi-flow.js)' };
            }
            return notumAHI.flow(params.steps || []);
        },

        /* ── Lifecycle ── */

        'init': function (params) {
            notumAHI.init(params.selector || null, params.config || {});
            return { ok: true, version: notumAHI.version };
        },

        'destroy': function () {
            notumAHI.destroy();
            return { ok: true };
        },

        /* ── Meta ── */

        'ping': function () {
            return {
                pong: true,
                version: notumAHI.version,
                vendor: 'Notum Robotics',
                url: 'https://n-r.hr'
            };
        }
    };

    /* ═══════════════════════════════════
       Message Processing
       ═══════════════════════════════════ */

    function makeError(id, code, message) {
        return JSON.stringify({
            jsonrpc: JSONRPC,
            error: { code: code, message: message },
            id: id
        });
    }

    function makeResult(id, result) {
        return JSON.stringify({
            jsonrpc: JSONRPC,
            result: result,
            id: id
        });
    }

    function makeEvent(params) {
        return JSON.stringify({
            jsonrpc: JSONRPC,
            method: 'event',
            params: params
        });
    }

    /**
     * Process a raw JSON string (or already-parsed object) from the transport.
     * Handles the message, calls the appropriate notumAHI method, and sends
     * the response via _sendFn.
     */
    function handleMessage(raw) {
        var msg;

        /* Parse if string */
        if (typeof raw === 'string') {
            try {
                msg = JSON.parse(raw);
            } catch (e) {
                send(makeError(null, ERR_PARSE, 'Parse error: ' + e.message));
                return;
            }
        } else {
            msg = raw;
        }

        /* Validate basic structure */
        if (!msg || typeof msg !== 'object') {
            send(makeError(null, ERR_INVALID, 'Invalid request'));
            return;
        }

        /* Batch support — collect responses and send as single JSON array (per JSON-RPC 2.0 spec) */
        if (Array.isArray(msg)) {
            if (msg.length === 0) return;
            var batchResponses = [];
            var origSendFn = _sendFn;
            /* Temporarily intercept send to collect responses */
            _sendFn = function (jsonStr) { batchResponses.push(jsonStr); };
            msg.forEach(function (m) { handleMessage(m); });
            _sendFn = origSendFn;
            if (batchResponses.length > 0) {
                /* Parse individual JSON strings, wrap in array, send as single message */
                var parsed = batchResponses.map(function (s) {
                    try { return JSON.parse(s); } catch (_) { return s; }
                });
                send(JSON.stringify(parsed));
            }
            return;
        }

        var id     = msg.id !== undefined ? msg.id : null;
        var method = msg.method;
        var params = msg.params || {};

        if (!method || typeof method !== 'string') {
            send(makeError(id, ERR_INVALID, 'Missing or invalid method'));
            return;
        }

        /* Prefix stripping: allow "ahi_render" or "ahi.render" as well as "render" */
        var cleanMethod = method.replace(/^ahi[_.]/, '');

        var handler = METHODS[cleanMethod];
        if (!handler) {
            send(makeError(id, ERR_NOT_FOUND, 'Method not found: ' + method));
            return;
        }

        /* Execute */
        try {
            var result = handler(params);

            /* Handle Promise returns (e.g., dialog) */
            if (result && typeof result.then === 'function') {
                result.then(function (val) {
                    if (id !== null) send(makeResult(id, val));
                }).catch(function (err) {
                    if (id !== null) {
                        send(makeError(id, ERR_INTERNAL, err.message || String(err)));
                    }
                });
            } else {
                if (id !== null) send(makeResult(id, result));
            }
        } catch (err) {
            var code = (err && err.code) ? err.code : ERR_INTERNAL;
            var message = (err && err.message) ? err.message : String(err);
            if (id !== null) send(makeError(id, code, message));
        }
    }

    function send(jsonString) {
        if (_sendFn) _sendFn(jsonString);
    }

    /* ═══════════════════════════════════
       Event Forwarding
       Subscribes to notumAHI events and
       forwards them as JSON-RPC notifications.
       ═══════════════════════════════════ */

    function startEventForwarding() {
        if (_eventUnsub) _eventUnsub();
        _eventUnsub = notumAHI.onEvent(function (evt) {
            send(makeEvent(evt));
        });
    }

    function stopEventForwarding() {
        if (_eventUnsub) {
            _eventUnsub();
            _eventUnsub = null;
        }
    }

    /* ═══════════════════════════════════
       Public API
       ═══════════════════════════════════ */

    return {
        /**
         * Bind a send function. The protocol calls this whenever it
         * needs to send a JSON string to the remote agent.
         *
         * @param {Function} fn  function(jsonString)
         */
        bind: function (fn) {
            _sendFn = fn;
            startEventForwarding();
        },

        /**
         * Feed a raw message (string or object) into the protocol.
         * Call this from your transport when data arrives.
         *
         * @param {string|Object} raw  Incoming JSON-RPC message
         */
        receive: handleMessage,

        /**
         * Unbind and stop forwarding events.
         */
        unbind: function () {
            _sendFn = null;
            stopEventForwarding();
        },

        /**
         * Register a custom method handler.
         *
         * @param {string}   name     Method name
         * @param {Function} handler  fn(params) → result or Promise
         */
        registerMethod: function (name, handler) {
            METHODS[name] = handler;
        },

        /** Expose for testing */
        _makeEvent:  makeEvent,
        _makeResult: makeResult,
        _makeError:  makeError
    };
})();

/* ── Global export ────────────────────────────────────────── */
window.notumAHIProtocol = notumAHIProtocol;
