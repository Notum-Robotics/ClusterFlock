/* ──────────────────────────────────────────────────────────────
   Notum AHI Transport — WebSocket Adapter
   Copyright © 2026 Notum Robotics (n-r.hr). Licensed under the MIT License.

   Connects the Notum AHI Protocol layer to a WebSocket server,
   enabling remote agents (MCP servers, tool-calling LLMs, custom
   AI harnesses) to drive the UI over the network.

   DEPENDENCIES: ahi.js, ahi-protocol.js

   USAGE:
     // Connect to an agent's WebSocket server
     notumAHITransportWS.connect('ws://localhost:9100');

     // The agent sends JSON-RPC messages, AHI executes them
     // User events are forwarded back over the same socket

     // Disconnect
     notumAHITransportWS.disconnect();

   ALSO SUPPORTS:
     PostMessage transport (for iframe sandboxing) — see
     notumAHITransportPM below.
   ────────────────────────────────────────────────────────────── */

var notumAHITransportWS = (function () {
    'use strict';

    /* ═══════════════════════════════════
       Internal State
       ═══════════════════════════════════ */

    var _ws              = null;
    var _url             = '';
    var _reconnect       = true;
    var _reconnectDelay  = 2000;
    var _maxReconnect    = 10;
    var _reconnectCount  = 0;
    var _reconnectTimer  = null;
    var _statusCallback  = null;

    /* ═══════════════════════════════════
       Connection Management
       ═══════════════════════════════════ */

    function setStatus(status, detail) {
        if (_statusCallback) {
            _statusCallback({ status: status, detail: detail || '', url: _url });
        }
        /* Also show a toast for visibility */
        if (typeof notumAHI !== 'undefined' && notumAHI.toast) {
            var level = (status === 'connected') ? 'ok' :
                        (status === 'error')     ? 'error' :
                        (status === 'closed')    ? 'warn' : 'info';
            notumAHI.toast('WS: ' + status.toUpperCase() + (detail ? ' — ' + detail : ''), level, 2000);
        }
    }

    /**
     * Connect to a WebSocket server.
     *
     * @param {string}  url      WebSocket URL (e.g. 'ws://localhost:9100')
     * @param {Object}  opts     Optional settings:
     *   reconnect     {boolean}  Auto-reconnect on disconnect (default: true)
     *   reconnectDelay {number}  Delay between attempts in ms (default: 2000)
     *   maxReconnect  {number}   Max reconnect attempts (default: 10)
     *   onStatus      {Function} Callback fn({status, detail, url})
     */
    function connect(url, opts) {
        opts = opts || {};
        _url            = url;
        _reconnect      = opts.reconnect !== false;
        _reconnectDelay = opts.reconnectDelay || 2000;
        _maxReconnect   = opts.maxReconnect || 10;
        _statusCallback = opts.onStatus || null;
        _reconnectCount = 0;

        /* Cancel any pending reconnect from a prior session */
        if (_reconnectTimer) {
            clearTimeout(_reconnectTimer);
            _reconnectTimer = null;
        }

        openSocket();
    }

    function openSocket() {
        if (_ws) {
            /* Suppress the onclose → scheduleReconnect that .close() would trigger */
            _ws.onclose = null;
            try { _ws.close(); } catch (_) {}
            _ws = null;
        }

        setStatus('connecting');

        try {
            _ws = new WebSocket(_url);
        } catch (e) {
            setStatus('error', e.message);
            scheduleReconnect();
            return;
        }

        _ws.onopen = function () {
            _reconnectCount = 0;
            setStatus('connected');

            /* Bind the protocol's send function to this socket */
            notumAHIProtocol.bind(function (jsonString) {
                if (_ws && _ws.readyState === WebSocket.OPEN) {
                    _ws.send(jsonString);
                }
            });

            /* Send a handshake with version info */
            var handshake = JSON.stringify({
                jsonrpc: '2.0',
                method: 'ahi_ready',
                params: {
                    version: notumAHI.version,
                    vendor: 'Notum Robotics',
                    url: 'https://n-r.hr',
                    capabilities: [
                        'render', 'patch', 'insert', 'remove',
                        'dialog', 'dismiss', 'lock', 'unlock',
                        'read', 'toast', 'flow', 'ping'
                    ]
                }
            });
            _ws.send(handshake);
        };

        _ws.onmessage = function (event) {
            if (typeof event.data === 'string') {
                notumAHIProtocol.receive(event.data);
            }
        };

        _ws.onerror = function (event) {
            setStatus('error', 'WebSocket error');
        };

        _ws.onclose = function (event) {
            setStatus('closed', 'code=' + event.code);
            notumAHIProtocol.unbind();
            /* Don't reconnect on clean server-initiated close (1000) */
            if (event.code !== 1000) {
                scheduleReconnect();
            }
        };
    }

    function scheduleReconnect() {
        if (!_reconnect) return;
        if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
        if (_reconnectCount >= _maxReconnect) {
            setStatus('gave_up', 'max reconnect attempts reached');
            return;
        }
        _reconnectCount++;
        /* Exponential backoff: delay × 2^(attempt-1), capped at 30s */
        var delay = Math.min(_reconnectDelay * Math.pow(2, _reconnectCount - 1), 30000);
        setStatus('reconnecting', 'attempt ' + _reconnectCount + '/' + _maxReconnect);
        _reconnectTimer = setTimeout(openSocket, delay);
    }

    /**
     * Disconnect and stop reconnecting.
     */
    function disconnect() {
        _reconnect = false;
        if (_reconnectTimer) {
            clearTimeout(_reconnectTimer);
            _reconnectTimer = null;
        }
        notumAHIProtocol.unbind();
        if (_ws) {
            try { _ws.close(1000, 'Client disconnect'); } catch (_) {}
            _ws = null;
        }
        setStatus('disconnected');
    }

    /**
     * Send a raw JSON-RPC message to the server.
     * Useful for agent-initiated messages.
     */
    function send(msg) {
        var str = (typeof msg === 'string') ? msg : JSON.stringify(msg);
        if (_ws && _ws.readyState === WebSocket.OPEN) {
            _ws.send(str);
        }
    }

    /* ═══════════════════════════════════
       Public API
       ═══════════════════════════════════ */

    return {
        connect:    connect,
        disconnect: disconnect,
        send:       send,
        isConnected: function () {
            return _ws && _ws.readyState === WebSocket.OPEN;
        }
    };
})();

/* ─────────────────────────────────────────────────────────────
   Notum AHI Transport — PostMessage Adapter (iframe sandboxing)
   Copyright © 2026 Notum Robotics (n-r.hr). Licensed under the MIT License.
   ───────────────────────────────────────────────────────────── */

var notumAHITransportPM = (function () {
    'use strict';

    var _origin   = '*';
    var _target   = null;  // window or iframe.contentWindow
    var _listener = null;

    /**
     * Start listening for postMessage commands.
     *
     * @param {Object} opts
     *   target  {Window}  Target window to send responses to (default: parent)
     *   origin  {string}  Allowed message origin (REQUIRED for security; '*' accepted but warns)
     */
    function listen(opts) {
        opts = opts || {};
        _origin = opts.origin || '*';
        if (_origin === '*') {
            console.warn('[notumAHI-PM] PostMessage origin is "*" — any window can send commands. ' +
                         'Set opts.origin to restrict to a specific origin for production use.');
        }
        _target = opts.target || window.parent;

        notumAHIProtocol.bind(function (jsonString) {
            if (_target) _target.postMessage(jsonString, _origin);
        });

        _listener = function (event) {
            if (_origin !== '*' && event.origin !== _origin) return;
            if (typeof event.data === 'string') {
                notumAHIProtocol.receive(event.data);
            } else if (typeof event.data === 'object' && event.data.jsonrpc) {
                notumAHIProtocol.receive(event.data);
            }
        };

        window.addEventListener('message', _listener);
    }

    /**
     * Stop listening.
     */
    function stop() {
        if (_listener) {
            window.removeEventListener('message', _listener);
            _listener = null;
        }
        notumAHIProtocol.unbind();
    }

    return {
        listen: listen,
        stop:   stop
    };
})();
