/* ──────────────────────────────────────────────────────────────
   Notum AHI Flow — Multi-Step Wizard Engine
   Copyright © 2026 Notum Robotics (n-r.hr). Licensed under the MIT License.

   Enables agents to define sequential, multi-step interactions
   as a single declarative call. Supports dialog chains, screen
   renders, branching, interpolation, and back-navigation.

   DEPENDENCY: ahi.js must be loaded first.

   USAGE:
     notumAHI.flow([
       { type: 'dialog', title: 'Select Env',
         buttons: [{ label: 'STAGING', value: 'staging' },
                   { label: 'PROD',    value: 'prod', style: 'danger' }] },
       { type: 'dialog', title: 'Confirm: {{0}}',
         body: 'Deploy to {{0}}?',
         buttons: [{ label: 'BACK', value: '$back' },
                   { label: 'GO',   value: 'go', style: 'primary' }] },
       { type: 'render', controls: [...] }
     ]).then(function (results) { console.log(results); });

   STEP TYPES:
     dialog  — Show a dialog, collect the response
     render  — Replace the screen with controls (non-blocking)
     toast   — Show a notification, advance immediately
     wait    — Pause for a duration (ms) then advance

   SPECIAL VALUES:
     $back   — Navigate to the previous dialog step
     $abort  — Cancel the entire flow, resolve with partial results

   INTERPOLATION:
     {{N}}   — Replaced with the result of step N (0-based)
     {{prev}} — Replaced with the result of the previous step
   ────────────────────────────────────────────────────────────── */

(function () {
    'use strict';

    /**
     * Execute a multi-step flow.
     *
     * @param {Array} steps  Array of step definition objects
     * @returns {Promise<Array>}  Resolves with array of collected values
     */
    function flow(steps) {
        if (!steps || !steps.length) return Promise.resolve([]);

        return new Promise(function (resolve, reject) {
            var results = [];    // collected values, indexed by step
            var current = 0;     // current step index

            function interpolate(str) {
                if (typeof str !== 'string') return str;
                return str.replace(/\{\{(\w+)\}\}/g, function (match, key) {
                    if (key === 'prev') {
                        var prev = results[results.length - 1];
                        return prev !== undefined ? String(prev) : '';
                    }
                    var idx = parseInt(key, 10);
                    if (!isNaN(idx) && idx >= 0 && idx < results.length) {
                        return results[idx] !== undefined ? String(results[idx]) : '';
                    }
                    return match; // leave unresolved placeholders
                });
            }

            function interpolateObj(obj) {
                if (!obj) return obj;
                var out = {};
                for (var k in obj) {
                    var v = obj[k];
                    if (typeof v === 'string') {
                        out[k] = interpolate(v);
                    } else if (Array.isArray(v)) {
                        out[k] = v.map(function (item) {
                            if (typeof item === 'object' && item !== null) {
                                return interpolateObj(item);
                            }
                            if (typeof item === 'string') return interpolate(item);
                            return item;
                        });
                    } else if (typeof v === 'object' && v !== null) {
                        out[k] = interpolateObj(v);
                    } else {
                        out[k] = v;
                    }
                }
                return out;
            }

            function advance() {
                if (current >= steps.length) {
                    resolve(results);
                    return;
                }

                var rawStep = steps[current];
                var step = interpolateObj(rawStep);

                try {
                switch (step.type) {

                    case 'dialog':
                        notumAHI.dialog({
                            title:   step.title || '',
                            body:    step.body || '',
                            buttons: step.buttons || []
                        }).then(function (val) {
                            if (val === '$back') {
                                /* Navigate back: pop the last result and go to previous step */
                                if (current > 0) {
                                    results.pop();
                                    current--;
                                }
                                advance();
                                return;
                            }
                            if (val === '$abort' || val === null) {
                                /* Abort flow with partial results */
                                results.push(val);
                                resolve(results);
                                return;
                            }
                            results.push(val);
                            current++;
                            advance();
                        });
                        break;

                    case 'render':
                        notumAHI.render(step.controls || [], step.config || null);
                        results.push('rendered');
                        current++;
                        /* If this is the last step, resolve immediately.
                           Otherwise, advance after a brief settle */
                        if (current >= steps.length) {
                            resolve(results);
                        } else {
                            setTimeout(advance, step.delay || 100);
                        }
                        break;

                    case 'toast':
                        notumAHI.toast(
                            step.message || '',
                            step.level || 'info',
                            step.duration || 2000
                        );
                        results.push('toasted');
                        current++;
                        setTimeout(advance, step.duration || 2000);
                        break;

                    case 'wait':
                        results.push('waited');
                        current++;
                        setTimeout(advance, step.duration || 1000);
                        break;

                    case 'patch':
                        notumAHI.patch(step.id || step.index, step.changes || {});
                        results.push('patched');
                        current++;
                        advance();
                        break;

                    case 'lock':
                        notumAHI.lock(step.id || step.index);
                        results.push('locked');
                        current++;
                        advance();
                        break;

                    case 'unlock':
                        notumAHI.unlock(step.id || step.index);
                        results.push('unlocked');
                        current++;
                        advance();
                        break;

                    case 'notify':
                        notumAHI.notify({
                            icon:     step.icon     || undefined,
                            title:    step.title    || '',
                            subtitle: step.subtitle || '',
                            linger:   step.linger   || undefined
                        });
                        results.push('notified');
                        current++;
                        advance();
                        break;

                    default:
                        console.warn('[notumAHI.flow] Unknown step type:', step.type);
                        results.push(null);
                        current++;
                        advance();
                        break;
                }
                } catch (err) {
                    console.error('[notumAHI.flow] Step ' + current + ' (' + (step.type || '?') + ') threw:', err);
                    reject(err);
                }
            }

            advance();
        });
    }

    /* ═══════════════════════════════════
       Attach to notumAHI
       ═══════════════════════════════════ */

    if (typeof notumAHI !== 'undefined') {
        notumAHI.flow = flow;
    } else {
        console.warn('[ahi-flow] notumAHI not found — load ahi.js before ahi-flow.js');
    }

})();
