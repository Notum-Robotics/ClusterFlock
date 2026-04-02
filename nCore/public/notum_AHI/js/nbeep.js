/* ──────────────────────────────────────────────────────────────
   nDesign Audio Subsystem — nbeep
   Copyright © 2026 Notum Robotics. Licensed under the MIT License.
   Deterministic, seeded, procedurally generated UI sounds.
   ────────────────────────────────────────────────────────────── */

const nDesignAudio = (() => {
    'use strict';

    // ─── 1. Audio Context Singleton ───────────────────────────
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    let ctx = null;            // Lazily created on first interaction
    var outputChain = null;    // High-pass → compressor → destination
    var lastActivityTs = 0;    // Date.now() of last nbeep() call
    var STALE_THRESHOLD = 30000;  // 30 s of inactivity → treat ctx as suspect

    /**
     * Returns true when the current AudioContext should be discarded:
     *   – state is 'interrupted' (Safari-specific zombie state)
     *   – state is 'closed'
     *   – context exists but has been idle longer than STALE_THRESHOLD
     *     AND its state is not 'running' (stuck in suspended/interrupted
     *     limbo where resume() may take seconds).
     * Callers should close() and null-out ctx when this returns true.
     */
    function isCtxStale() {
        if (!ctx) return false;
        var st = ctx.state;
        if (st === 'interrupted' || st === 'closed') return true;
        // If idle for a while and not cleanly running, nuke it —
        // resume() on a long-suspended Safari context is unreliable.
        if (st !== 'running' && lastActivityTs > 0 &&
            (Date.now() - lastActivityTs) > STALE_THRESHOLD) return true;
        return false;
    }

    /** Discard a stale/zombie AudioContext so a fresh one can replace it. */
    function discardCtx() {
        if (!ctx) return;
        try { ctx.close(); } catch (_) {}
        ctx = null;
        outputChain = null;
    }

    /**
     * Build (or return cached) output chain:
     *   high-pass filter (80 Hz) → DynamicsCompressorNode → destination
     * Prevents sub-bass cone excursion pops and clipping from summed voices.
     */
    function getOutputChain() {
        if (outputChain) return outputChain;
        var ac = ctx;  // caller must ensure ctx exists

        // High-pass filter — remove sub-80 Hz energy that small speakers
        // can't reproduce cleanly (Fix 4: cone excursion / DC offset)
        var hp = ac.createBiquadFilter();
        hp.type = 'highpass';
        hp.frequency.value = 80;
        hp.Q.value = 0.7;  // gentle Butterworth-style rolloff

        // Limiter / compressor — tames summed-voice peaks that would
        // otherwise clip the DAC (Fix 3: digital clipping pops)
        var comp = ac.createDynamicsCompressor();
        comp.threshold.value = -6;   // start compressing at -6 dB
        comp.knee.value      = 6;    // soft knee
        comp.ratio.value     = 12;   // aggressive limiting above threshold
        comp.attack.value    = 0.002; // 2 ms — fast enough to catch transients
        comp.release.value   = 0.05;  // 50 ms

        hp.connect(comp);
        comp.connect(ac.destination);

        outputChain = hp;  // entry point of the chain
        return outputChain;
    }

    function getCtx() {
        // Mirror the same zombie / staleness checks that warmUp() performs
        // so every code-path that schedules oscillators gets a healthy ctx.
        if (isCtxStale()) discardCtx();
        if (!ctx) ctx = new AudioCtx();
        if (ctx.state === 'suspended') ctx.resume();
        return ctx;
    }

    /** Returns the node that oscillators should connect to (output chain entry). */
    function getOutput() {
        getCtx();  // ensure ctx is alive
        return getOutputChain();
    }

    /**
     * warmUp()
     * Pre-warms the AudioContext so it's running before the next
     * nbeep() call.  Intended to be called on mousedown / touchstart
     * so the context is already resumed by the time the click event
     * fires and schedules oscillators — eliminating the 50-150 ms
     * latency caused by a suspended→running transition.
     */
    function warmUp() {
        // Safari may zombie the AudioContext after background inactivity —
        // state becomes 'interrupted' or 'closed' but resume() is a no-op.
        // Also discard contexts that have been idle too long in a non-running
        // state — resume() can take 2-3 s on a long-idle Safari tab.
        if (isCtxStale()) discardCtx();
        if (!ctx) ctx = new AudioCtx();
        if (ctx.state === 'suspended') ctx.resume();
    }

    // ─── 1b. Proactive Reset on Tab Visibility Change ────────
    //   When the user returns to an idle tab, immediately discard a
    //   zombie AudioContext so the next mousedown → warmUp() creates
    //   a fresh one instead of trying to revive a dead one.
    if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible' && isCtxStale()) {
                discardCtx();
            }
        });
    }

    // ─── 2. Musical Scale Library ─────────────────────────────
    //   Each scale is an array of frequencies (Hz).
    //   Two octaves (C4–C6) for comfortable UI range.

    const SCALES = {
        pentatonic: {
            label: 'C MAJ PENTATONIC',
            notes: [
                261.63, 293.66, 329.63, 392.00, 440.00,   // C4 D4 E4 G4 A4
                523.25, 587.33, 659.25, 783.99, 880.00,   // C5 D5 E5 G5 A5
                1046.50                                     // C6
            ]
        },
        minor_pentatonic: {
            label: 'C MIN PENTATONIC',
            notes: [
                261.63, 311.13, 349.23, 392.00, 466.16,   // C4 Eb4 F4 G4 Bb4
                523.25, 622.25, 698.46, 783.99, 932.33,   // C5 Eb5 F5 G5 Bb5
                1046.50                                     // C6
            ]
        },
        chromatic: {
            label: 'CHROMATIC',
            notes: [
                261.63, 277.18, 293.66, 311.13, 329.63, 349.23,
                369.99, 392.00, 415.30, 440.00, 466.16, 493.88,
                523.25, 554.37, 587.33, 622.25, 659.25, 698.46,
                739.99, 783.99, 830.61, 880.00, 932.33, 987.77,
                1046.50
            ]
        },
        whole_tone: {
            label: 'WHOLE TONE',
            notes: [
                261.63, 293.66, 329.63, 369.99, 415.30, 466.16,
                523.25, 587.33, 659.25, 739.99, 830.61, 932.33,
                1046.50
            ]
        },
        lydian: {
            label: 'C LYDIAN',
            notes: [
                261.63, 293.66, 329.63, 369.99, 392.00, 440.00, 493.88,
                523.25, 587.33, 659.25, 739.99, 783.99, 880.00, 987.77,
                1046.50
            ]
        },
        dorian: {
            label: 'C DORIAN',
            notes: [
                261.63, 293.66, 311.13, 349.23, 392.00, 440.00, 466.16,
                523.25, 587.33, 622.25, 698.46, 783.99, 880.00, 932.33,
                1046.50
            ]
        }
    };

    // ─── 3. Configuration State ───────────────────────────────

    const config = {
        globalSeed:       2026,
        masterVolume:     0.10,          // 0.0 – 1.0 (default 20%)
        maxDuration:      0.10,          // 0.0 – 1.0 (scales all durations; default 80%)
        useMusicalScale:  true,
        soundMode:        'ncars',       // 'standard' | 'harmonic' | 'ncars' | 'ncars2'
        scale:            'pentatonic',  // key into SCALES
        durationMin:      0.02,          // 20 ms
        durationMax:      0.30,          // 300 ms
        allowedWaveforms: ['sine', 'triangle'],
        fadeDuration:     0.010          // 10 ms anti-pop envelope
    };

    // ─── 4. Cyrb53 Hash ──────────────────────────────────────
    //   Fast 53-bit string hash. NOT cryptographic.
    //   Deterministic: same (str, seed) → same integer, always.

    function cyrb53(str, seed) {
        seed = seed || 0;
        var h1 = 0xdeadbeef ^ seed, h2 = 0x41c6ce57 ^ seed;
        for (var i = 0, ch; i < str.length; i++) {
            ch = str.charCodeAt(i);
            h1 = Math.imul(h1 ^ ch, 2654435761);
            h2 = Math.imul(h2 ^ ch, 1597334677);
        }
        h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
        h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
        return 4294967296 * (2097151 & h2) + (h1 >>> 0);
    }

    // ─── 5. Mulberry32 Seeded PRNG ───────────────────────────
    //   Returns a function; each call yields a new float [0, 1).

    function mulberry32(seed) {
        return function () {
            var t = seed += 0x6D2B79F5;
            t = Math.imul(t ^ t >>> 15, t | 1);
            t ^= t + Math.imul(t ^ t >>> 7, t | 61);
            return ((t ^ t >>> 14) >>> 0) / 4294967296;
        };
    }

    // ─── 6. Active Voice State (mono — one sound at a time) ──

    var activeOsc  = null;   // current OscillatorNode (standard mode)
    var activeGain = null;   // current GainNode (standard mode)
    var activeVoices = [];   // array of {osc, gain} for harmonic mode
    var loopTimer   = null;   // setInterval id for loop mode
    var loopText    = null;   // text being looped (tone A)
    var loopAltText = null;   // alternate text for dual-text loop (tone B)
    var loopPhase   = false;  // false = tone A next, true = tone B next

    /**
     * Kill whatever is currently playing — immediately.
     * Safe to call even when nothing is active.
     */
    function killActive() {
        if (loopTimer !== null) {
            clearInterval(loopTimer);
            loopTimer   = null;
            loopText    = null;
            loopAltText = null;
            loopPhase   = false;
        }
        return killOscillators();
    }

    /**
     * Stop active oscillators/voices without touching the loop timer.
     * Uses a 10 ms gain fade-out before stopping oscillators to avoid
     * speaker-cone discontinuity pops (Fix 1).
     *
     * Key detail: reading `gain.gain.value` during an active automation
     * ramp returns the *last explicitly-set* value, NOT the current
     * interpolated position — so setValueAtTime(gain.gain.value, now)
     * can jump the gain to 0 or full volume, creating the exact
     * waveform discontinuity that causes an audible pop.
     *
     * Solution: use cancelAndHoldAtTime(now) where available (Chrome 57+,
     * Firefox 69+, Safari 14.1+).  This freezes the automation at its
     * true interpolated value at `now` so the subsequent linear ramp
     * starts from the correct amplitude — zero discontinuity.
     * Fallback for older engines: cancelScheduledValues + a conservative
     * setValueAtTime using the nominal masterVolume as a safe ceiling.
     */
    var KILL_FADE = 0.010;  // 10 ms fade-out before hard stop

    function killOscillators() {
        var ac = ctx;  // may be null if nothing was ever played
        var now = ac ? ac.currentTime : 0;

        // ── Fade-out helper: ramp gain → 0, then schedule stop ──
        function fadeAndStop(osc, gain) {
            if (!ac || !gain || !osc) return;
            try {
                var gp = gain.gain;
                if (typeof gp.cancelAndHoldAtTime === 'function') {
                    // Preferred: freezes at the true interpolated value
                    gp.cancelAndHoldAtTime(now);
                } else {
                    // Fallback: cancel future events, pin to a safe ceiling
                    gp.cancelScheduledValues(now);
                    gp.setValueAtTime(Math.min(gp.value || config.masterVolume, config.masterVolume), now);
                }
                gp.linearRampToValueAtTime(0, now + KILL_FADE);
            } catch (_) {}
            try { osc.stop(now + KILL_FADE + 0.002); } catch (_) {}
            // Disconnect after fade completes
            setTimeout(function () {
                try { osc.disconnect(); } catch (_) {}
                try { gain.disconnect(); } catch (_) {}
            }, (KILL_FADE + 0.010) * 1000);
        }

        var hadActive = false;

        if (activeOsc) {
            hadActive = true;
            fadeAndStop(activeOsc, activeGain);
            activeOsc  = null;
            activeGain = null;
        }

        // Fade-out harmonic mode voices
        if (activeVoices.length > 0) hadActive = true;
        activeVoices.forEach(function (v) {
            fadeAndStop(v.osc, v.gain);
        });
        activeVoices = [];

        return hadActive;
    }

    // ─── 7. Tone Parameters (deterministic from text) ────────

    function computeTone(safeText, targetDurSec) {
        var hash   = cyrb53(safeText, config.globalSeed);
        var random = mulberry32(hash);

        var range    = config.durationMax - config.durationMin;
        var duration = config.durationMin + (random() * range);
        duration = Math.max(0.02, duration * config.maxDuration);

        // Override duration if caller requests a specific length
        if (typeof targetDurSec === 'number' && targetDurSec > 0) {
            duration = targetDurSec;
        }

        var waveIdx  = Math.floor(random() * config.allowedWaveforms.length);
        var waveform = config.allowedWaveforms[waveIdx];

        var startFreq, endFreq;

        if (config.useMusicalScale) {
            var scaleObj = SCALES[config.scale] || SCALES.pentatonic;
            var notes    = scaleObj.notes;
            var scaleLen = notes.length;
            var startIdx = Math.floor(random() * (scaleLen - 2));
            startFreq    = notes[startIdx];
            var slideOff = random() > 0.5 ? 1 : 2;
            endFreq      = notes[Math.min(startIdx + slideOff, scaleLen - 1)];
        } else {
            startFreq = 300 + (random() * 1500);
            endFreq   = startFreq + ((random() - 0.5) * 500);
            if (endFreq < 20) endFreq = 20;
        }

        return {
            duration:  duration,
            waveform:  waveform,
            startFreq: startFreq,
            endFreq:   endFreq
        };
    }

    // ─── 7b. Harmonic Tone Parameters (rich multi-voice) ─────
    //   Generates a chord with staggered arpeggio, harmonics,
    //   vibrato, and layered envelopes for a more intricate sound.

    // Chord interval patterns (semitone offsets from root).
    // Each pattern produces a distinct harmonic colour.
    var CHORD_PATTERNS = [
        [0, 4, 7],          // major triad
        [0, 3, 7],          // minor triad
        [0, 7, 12],         // power + octave
        [0, 4, 7, 11],      // major 7th
        [0, 3, 7, 10],      // minor 7th
        [0, 5, 7],          // sus4
        [0, 2, 7],          // sus2
        [0, 4, 7, 14],      // major + 9th
        [0, 3, 10, 14],     // minor 7th + 9th
        [0, 7, 11, 16]      // maj7 spread voicing
    ];

    // Waveform palette per voice layer
    var HARMONIC_WAVES = ['sine', 'triangle', 'sine', 'triangle', 'square'];

    function computeHarmonicTone(safeText, targetDurSec) {
        var hash   = cyrb53(safeText, config.globalSeed);
        var random = mulberry32(hash);

        var scaleObj = SCALES[config.scale] || SCALES.pentatonic;
        var notes    = scaleObj.notes;
        var scaleLen = notes.length;

        // Pick root note
        var rootIdx  = Math.floor(random() * (scaleLen - 3));
        var rootFreq = notes[rootIdx];

        // Choose a chord pattern
        var chordIdx = Math.floor(random() * CHORD_PATTERNS.length);
        var pattern  = CHORD_PATTERNS[chordIdx];

        // Base duration: longer than standard mode for harmonic decay
        var baseDur = 0.12 + (random() * 0.20);  // 120–320 ms
        baseDur = Math.max(0.02, baseDur * config.maxDuration);

        // Override duration if caller requests a specific length
        if (typeof targetDurSec === 'number' && targetDurSec > 0) {
            baseDur = targetDurSec;
        }

        // Arpeggio stagger range: 0–35ms per voice
        var staggerMax = 0.005 + (random() * 0.030);

        // Build voices array
        var voices = [];
        for (var i = 0; i < pattern.length; i++) {
            var semitones = pattern[i];
            var freq = rootFreq * Math.pow(2, semitones / 12);

            // Snap to nearest scale note if within tolerance (keeps tonality)
            var bestDist = Infinity;
            var snapped  = freq;
            for (var n = 0; n < scaleLen; n++) {
                var d = Math.abs(notes[n] - freq);
                if (d < bestDist) { bestDist = d; snapped = notes[n]; }
            }
            // Snap if within ~5% of a scale note, else keep chromatic
            if (bestDist / freq < 0.05) freq = snapped;

            // Clamp to audible UI range
            if (freq < 120) freq *= 2;
            if (freq > 2200) freq /= 2;

            // Voice-specific parameters
            var wave = HARMONIC_WAVES[i % HARMONIC_WAVES.length];

            // Root is loudest; upper voices progressively quieter
            var volScale = (i === 0) ? 1.0 : (0.45 - (i * 0.08));
            if (volScale < 0.12) volScale = 0.12;

            // Slight random duration variation per voice
            var durOff = (random() - 0.3) * 0.08;
            var dur = Math.max(0.06, baseDur + durOff);

            // Stagger delay (arpeggio effect)
            var delay = i * staggerMax * (0.6 + random() * 0.4);

            // Subtle pitch bend: endpoints for micro-slide
            var bendDir    = (random() > 0.5) ? 1 : -1;
            var bendAmount = 1 + (bendDir * random() * 0.015);  // ±1.5%
            var endFreq    = freq * bendAmount;

            // Vibrato (LFO depth & rate)
            var vibratoRate  = 4 + random() * 6;   // 4–10 Hz
            var vibratoDepth = random() * 4;        // 0–4 Hz deviation
            var useVibrato   = (i === 0 && random() > 0.4) || (i > 0 && random() > 0.7);

            voices.push({
                freq:         freq,
                endFreq:      endFreq,
                waveform:     wave,
                volume:       volScale,
                duration:     dur,
                delay:        delay,
                vibratoRate:  useVibrato ? vibratoRate : 0,
                vibratoDepth: useVibrato ? vibratoDepth : 0
            });
        }

        // Optionally add a sub-octave ghost (20% chance)
        if (random() > 0.80) {
            voices.push({
                freq:         rootFreq / 2,
                endFreq:      rootFreq / 2,
                waveform:     'sine',
                volume:       0.15,
                duration:     baseDur * 1.3,
                delay:        0,
                vibratoRate:  0,
                vibratoDepth: 0
            });
        }

        // Optionally add a high shimmer (25% chance)
        if (random() > 0.75) {
            var shimmerFreq = rootFreq * (random() > 0.5 ? 4 : 3);
            if (shimmerFreq > 3000) shimmerFreq /= 2;
            voices.push({
                freq:         shimmerFreq,
                endFreq:      shimmerFreq * (1 + random() * 0.02),
                waveform:     'sine',
                volume:       0.08 + random() * 0.06,
                duration:     baseDur * 0.6,
                delay:        staggerMax * 0.5,
                vibratoRate:  6 + random() * 4,
                vibratoDepth: 2 + random() * 3
            });
        }

        return voices;
    }

    // ─── 7c. Play Harmonic Voices (multi-oscillator) ─────────

    function playHarmonicTone(voices, pitchMultiplier, startDelay) {
        var ac  = getCtx();
        var now = ac.currentTime + (startDelay || 0);
        var pm  = (typeof pitchMultiplier === 'number' && pitchMultiplier > 0) ? pitchMultiplier : 1.0;

        voices.forEach(function (v) {
            var osc  = ac.createOscillator();
            var gain = ac.createGain();
            var start = now + v.delay;
            var end   = start + v.duration;
            var fade  = Math.min(config.fadeDuration, v.duration * 0.15);

            var vFreq    = Math.max(20, Math.min(5000, v.freq * pm));
            var vEndFreq = Math.max(20, Math.min(5000, v.endFreq * pm));

            osc.type = v.waveform;
            osc.frequency.setValueAtTime(vFreq, start);
            if (vEndFreq !== vFreq) {
                osc.frequency.exponentialRampToValueAtTime(
                    Math.max(vEndFreq, 20), end
                );
            }

            // Vibrato via LFO
            if (v.vibratoRate > 0 && v.vibratoDepth > 0) {
                var lfo     = ac.createOscillator();
                var lfoGain = ac.createGain();
                lfo.type = 'sine';
                lfo.frequency.setValueAtTime(v.vibratoRate, start);
                lfoGain.gain.setValueAtTime(v.vibratoDepth, start);
                lfo.connect(lfoGain);
                lfoGain.connect(osc.frequency);
                lfo.start(start);
                lfo.stop(end);
            }

            // Shaped envelope: fast attack, hold, smooth release
            var vol = Math.max(config.masterVolume * v.volume, 0.001);
            gain.gain.setValueAtTime(0.001, start);
            gain.gain.linearRampToValueAtTime(vol, start + fade);
            // Hold briefly then decay
            var holdEnd = start + v.duration * 0.35;
            gain.gain.setValueAtTime(vol, Math.min(holdEnd, end - fade * 2));
            gain.gain.exponentialRampToValueAtTime(0.001, end);
            gain.gain.linearRampToValueAtTime(0, end + 0.002);

            osc.connect(gain);
            gain.connect(getOutput());

            osc.start(start);
            osc.stop(end + 0.005);

            activeVoices.push({ osc: osc, gain: gain });

            osc.onended = function () {
                activeVoices = activeVoices.filter(function (entry) {
                    return entry.osc !== osc;
                });
                osc.disconnect();
                gain.disconnect();
            };
        });
    }

    // ─── 7d. nCARS — LCARS-style Computer Beeps ──────────────
    //   Emulates the iconic Star Trek LCARS interface sounds:
    //   clean sine tones at exact musical pitches, often in
    //   rapid 2-3 note sequences with crisp attack/release.

    // Canonical LCARS frequency palette (Hz)
    // Based on the major pentatonic + key intervals used in the shows
    var LCARS_FREQS = [
        523.25,  // C5
        587.33,  // D5
        659.25,  // E5
        783.99,  // G5
        880.00,  // A5
        987.77,  // B5
        1046.50, // C6
        1174.66, // D6
        1318.51, // E6
        1567.98, // G6
        1760.00, // A6
    ];

    // Note sequence patterns (index offsets from a random root position)
    var LCARS_PATTERNS = [
        [0],                  // single chirp
        [0, 2],              // two-tone rising
        [2, 0],              // two-tone falling
        [0, 2, 4],           // ascending triple
        [4, 2, 0],           // descending triple
        [0, 3],              // wide interval (alert feel)
        [0, 1, 3],           // stepwise cluster
        [3, 0, 1],           // drop then nudge up
        [0, 4, 2],           // leap and settle
        [0, 0],              // double tap (same pitch)
        [0, 2, 0],           // up-down chirp
        [1, 3, 1, 0],       // four-note acknowledgement
    ];

    function computeNcarsTone(safeText, targetDurSec) {
        var hash   = cyrb53(safeText, config.globalSeed);
        var random = mulberry32(hash);

        var freqs   = LCARS_FREQS;
        var freqLen = freqs.length;

        // Pick a pattern
        var patIdx  = Math.floor(random() * LCARS_PATTERNS.length);
        var pattern = LCARS_PATTERNS[patIdx];

        // Pick a root position in the frequency table
        var maxRoot = freqLen - 5;  // leave room for offsets
        if (maxRoot < 0) maxRoot = 0;
        var rootIdx = Math.floor(random() * (maxRoot + 1));

        // Note duration: short and crisp, 40–100ms per note
        var noteDur = 0.04 + (random() * 0.06);
        noteDur = Math.max(0.02, noteDur * config.maxDuration);

        // Gap between notes: 10–40ms
        var noteGap = (0.01 + (random() * 0.03)) * config.maxDuration;

        // ── Musical composition for long beeps (>500ms) ──
        //
        // Design principles for pleasant procedural audio:
        //  1. Consonant intervals — pentatonic scale never clashes
        //  2. Melodic contour — arch shape (rise → peak → resolve)
        //  3. Call & response — two-bar phrases that answer each other
        //  4. Tension/resolution — move away from home, then return
        //  5. Rhythmic cells — grouped beats (not random durations)
        //  6. Dynamic arc — swell in, sustain, gentle fadeout
        //  7. Strategic silence — rests give the ear space to breathe
        //  8. Motif with variation — recognition without repetition
        //  9. Register movement — travel through low/mid/high
        // 10. Landing notes — phrases end on consonant scale degrees
        //
        if (typeof targetDurSec === 'number' && targetDurSec > 0) {
            var baseTime = pattern.length * (noteDur + noteGap);

            if (targetDurSec > 0.5) {
                var maxNoteIdx = freqLen - 1;  // absolute freq table index
                var composed = [];  // [{idx, dur, gap, vol, bend}]

                // ── Seed rhythm cells: groupings that feel natural ──
                var RHYTHMS = [
                    [1.0, 1.0],                         // even pair
                    [1.0, 0.5, 0.5],                    // long-short-short
                    [0.5, 0.5, 1.0],                    // short-short-long
                    [1.5, 0.5],                          // dotted-short
                    [1.0, 1.0, 0.5, 0.5],               // even-even-quick-quick
                    [0.5, 1.0, 0.5],                     // anacrusis feel
                    [1.0, 0.5, 1.0, 0.5],               // lilting
                    [2.0],                                // sustained single
                    [0.75, 0.75, 0.5],                   // triplet feel
                    [1.0, 0.5, 0.5, 1.0],               // swing
                ];

                // ── Melodic shapes: relative pitch contours ──
                // Values are scale-step movements from phrase root
                var CONTOURS = [
                    [0, 2, 4, 2],          // arch (rise & fall)
                    [4, 2, 0, 2],          // valley (dip & return)
                    [0, 1, 2, 3],          // gentle ascending
                    [3, 2, 1, 0],          // gentle descending
                    [0, 4, 3, 1],          // leap then step down
                    [0, 2, 1, 3],          // zigzag up
                    [4, 0, 2, 0],          // pedal point alternation
                    [0, 3, 2, 4, 0],       // exploratory with resolution
                    [0, 0, 2, 4],          // repeated root then rise
                    [2, 4, 2, 0],          // descending from mid
                    [0, 1, 0, -1, 0],      // ornamental turn
                    [0, 2, 4, 6, 4],       // big arch
                ];

                // Choose a home note (tonal center) — lower in the scale
                // for warmth, leaving room to move upward
                var home = rootIdx + Math.floor(random() * 3);
                if (home > maxNoteIdx - 6) home = maxNoteIdx - 6;
                if (home < 0) home = 0;

                // Choose density: how many notes per second this piece targets
                var density = 6 + random() * 10;  // 6–16 notes/sec
                var estTotal = Math.ceil(targetDurSec * density);
                estTotal = Math.min(estTotal, 400);

                // ── Macro structure: overall shape of the piece ──
                // Divide into sections with distinct characters
                var numSections = Math.max(2, Math.floor(targetDurSec / 0.8));
                numSections = Math.min(numSections, 8);
                var notesPerSection = Math.ceil(estTotal / numSections);

                // Pre-determine the register (pitch center) arc for each section
                // Classic arch: start near home, rise to peak in ~60%, return
                var sectionRoots = [];
                for (var si = 0; si < numSections; si++) {
                    var arcPos = si / Math.max(numSections - 1, 1);
                    // Sinusoidal arch peaking at ~55%
                    var arc = Math.sin(arcPos * Math.PI * 0.95);
                    var range = Math.min(6, maxNoteIdx - home);
                    var sRoot = home + Math.round(arc * range);
                    // Add slight random offset for organic feel
                    sRoot += Math.floor(random() * 3) - 1;
                    if (sRoot < 0) sRoot = 0;
                    if (sRoot > maxNoteIdx - 4) sRoot = maxNoteIdx - 4;
                    sectionRoots.push(sRoot);
                }

                // Pre-select a motif (2–3 notes) that will recur for recognition
                var motif = [];
                var motifContour = CONTOURS[Math.floor(random() * CONTOURS.length)];
                for (var mi = 0; mi < Math.min(3, motifContour.length); mi++) {
                    motif.push(motifContour[mi]);
                }

                // ── Generate each section ──
                for (var sec = 0; sec < numSections && composed.length < estTotal; sec++) {
                    var secRoot = sectionRoots[sec];
                    var secProgress = sec / Math.max(numSections - 1, 1);

                    // Section energy: intensity envelope (soft→strong→gentle)
                    var secEnergy;
                    if (secProgress < 0.35) secEnergy = 0.55 + secProgress * 1.2;
                    else if (secProgress < 0.70) secEnergy = 0.90 + (secProgress - 0.35) * 0.3;
                    else secEnergy = 1.0 - (secProgress - 0.70) * 1.5;
                    secEnergy = Math.max(0.35, Math.min(1.0, secEnergy));

                    // Pick a rhythm cell for this section
                    var rCell = RHYTHMS[Math.floor(random() * RHYTHMS.length)];

                    // Pick a contour for this section
                    var contour = CONTOURS[Math.floor(random() * CONTOURS.length)];

                    // How many phrases in this section?
                    var phrasesInSec = 1 + Math.floor(random() * 2.5);
                    var notesInSec = Math.min(notesPerSection, estTotal - composed.length);

                    for (var phr = 0; phr < phrasesInSec && composed.length < estTotal; phr++) {
                        // Every ~3rd phrase: insert motif callback (recognition)
                        var useMotif = (sec > 0 && phr === 0 && random() > 0.55);

                        // Phrase-opening rest (breathing space) — except first phrase
                        if (composed.length > 0 && random() > 0.3) {
                            var restLen = 0.4 + random() * 1.2; // 40–160% of noteDur as rest
                            composed.push({
                                idx: -1, dur: noteDur * restLen,
                                gap: 0, vol: 0, bend: 1.0
                            });
                        }

                        // Determine notes for this phrase
                        var phraseContour = useMotif
                            ? motif
                            : contour.slice(0, rCell.length + Math.floor(random() * 2));
                        if (phraseContour.length < 1) phraseContour = [0];

                        // Slight transpose variation within the section
                        var phraseShift = Math.floor(random() * 3) - 1;

                        for (var ni = 0; ni < phraseContour.length && composed.length < estTotal; ni++) {
                            var step = phraseContour[ni] + phraseShift;
                            var noteIdx = secRoot + step;
                            // Wrap melodically (fold back into range, not hard clamp)
                            if (noteIdx < 0) noteIdx = (-noteIdx) % (maxNoteIdx + 1);
                            if (noteIdx > maxNoteIdx) noteIdx = maxNoteIdx - (noteIdx - maxNoteIdx) % Math.max(maxNoteIdx, 1);
                            if (noteIdx < 0) noteIdx = 0;
                            if (noteIdx > maxNoteIdx) noteIdx = maxNoteIdx;

                            // Rhythm from the cell (cyclically)
                            var rMul = rCell[ni % rCell.length];

                            // Gap: tight within phrase, wider at phrase end
                            var isLast = (ni === phraseContour.length - 1);
                            var gMul = isLast ? (1.5 + random() * 1.0) : (0.6 + random() * 0.5);

                            // Volume: first note of phrase accented, last
                            // note slightly softer (phrase trailing off)
                            var nVol;
                            if (ni === 0) nVol = 0.85 + random() * 0.15;        // accent
                            else if (isLast) nVol = 0.45 + random() * 0.20;     // gentle ending
                            else nVol = 0.55 + random() * 0.30;                 // middle
                            nVol *= secEnergy;

                            // Subtle pitch glide ±0.2%
                            var bDir = (random() > 0.5) ? 1 : -1;
                            var bAmt = 1 + (bDir * random() * 0.002);

                            composed.push({
                                idx: noteIdx, dur: noteDur * rMul * (0.85 + random() * 0.3),
                                gap: noteGap * gMul, vol: nVol, bend: bAmt
                            });
                        }

                        // Last note of last phrase in last section resolves to home
                        if (sec === numSections - 1 && phr === phrasesInSec - 1 && composed.length > 0) {
                            var last = composed[composed.length - 1];
                            if (last.idx >= 0) {
                                // Resolve: step to home note, slightly longer, louder
                                var resolveIdx = home;
                                composed.push({
                                    idx: resolveIdx, dur: noteDur * 2.0,
                                    gap: noteGap * 0.5, vol: 0.80 * secEnergy, bend: 1.0
                                });
                            }
                        }
                    }
                }

                // ── Build voices from composed notes ──
                var voices = [];
                var time = 0;

                for (var ci = 0; ci < composed.length; ci++) {
                    var c = composed[ci];

                    // Rest (silence)
                    if (c.idx === -1) {
                        time += c.dur;
                        if (time >= targetDurSec) break;
                        continue;
                    }

                    var cIdx = Math.max(0, Math.min(c.idx, maxNoteIdx));
                    var cFreq = freqs[cIdx];
                    var cDur = Math.max(0.015, c.dur);

                    // Overall piece envelope (gentle swell shape)
                    var piecePos = time / targetDurSec;
                    var pieceEnv = 1.0;
                    if (piecePos < 0.06) pieceEnv = 0.3 + (piecePos / 0.06) * 0.7;
                    else if (piecePos > 0.90) pieceEnv = 0.2 + ((1.0 - piecePos) / 0.10) * 0.8;
                    var cVol = Math.min(1.0, c.vol * pieceEnv);

                    voices.push({
                        freq:         cFreq,
                        endFreq:      cFreq * c.bend,
                        waveform:     'sine',
                        volume:       cVol,
                        duration:     cDur,
                        delay:        time,
                        vibratoRate:  0,
                        vibratoDepth: 0
                    });

                    time += cDur + Math.max(0, c.gap);
                    if (time >= targetDurSec) break;
                }

                return voices;

            } else {
                // Short override (≤500ms): scale note duration to fit
                var scaleFactor = targetDurSec / Math.max(baseTime, 0.01);
                noteDur = noteDur * Math.max(scaleFactor, 0.5);
                noteGap = noteGap * Math.max(scaleFactor, 0.5);
            }
        }

        // Build voice array for short beeps / default duration
        // (Long beeps >500ms return early from the composer above)
        var voices = [];
        var time = 0;

        for (var i = 0; i < pattern.length; i++) {
            var idx = rootIdx + pattern[i];
            if (idx >= freqLen) idx = freqLen - 1;
            if (idx < 0) idx = 0;

            var freq = freqs[idx];

            // Subtle pitch bend (±0.3%) for organic feel
            var bendDir = (random() > 0.5) ? 1 : -1;
            var bend = 1 + (bendDir * random() * 0.003);

            // Volume: accent first note
            var volBase = (i === 0) ? (0.85 + random() * 0.15) : (0.55 + random() * 0.35);

            voices.push({
                freq:         freq,
                endFreq:      freq * bend,
                waveform:     'sine',
                volume:       volBase,
                duration:     noteDur,
                delay:        time,
                vibratoRate:  0,
                vibratoDepth: 0
            });

            time += noteDur + noteGap;

            // Trim at target duration
            if (typeof targetDurSec === 'number' && targetDurSec > 0 && time >= targetDurSec) break;
        }

        return voices;
    }

    // ─── 7e. nCARS 2 — High-Register LCARS with Extended Variety ──
    //   Brighter, more articulate variant. Uses a wider frequency
    //   palette spanning C6–C8, richer pattern library with dynamics
    //   variation, occasional dual-layer notes, and micro-rests.

    var LCARS2_FREQS = [
        1046.50, // C6
        1174.66, // D6
        1318.51, // E6
        1396.91, // F6
        1567.98, // G6
        1760.00, // A6
        1975.53, // B6
        2093.00, // C7
        2349.32, // D7
        2637.02, // E7
        2793.83, // F7
        3135.96, // G7
        3520.00, // A7
        3951.07, // B7
        4186.01, // C8
    ];

    var LCARS2_PATTERNS = [
        [0],                     // single pip
        [0, 3],                  // wide rising
        [4, 0],                  // wide falling
        [0, 1, 3],              // ascending cluster
        [5, 3, 0],              // descending sweep
        [0, 4, 2],              // leap and settle
        [0, 0, 3],              // double tap then rise
        [2, 0, 2, 4],           // oscillate up
        [0, 6],                 // octave jump
        [6, 3, 0],              // octave cascade down
        [0, 2, 4, 6],           // bright ascending run
        [5, 3, 1, 0],           // chromatic descent
        [0, 3, 0],              // ping-pong
        [0, -1, 2, 5],          // dip then climb (negative = stay at 0)
        [3, 3, 0, 0],           // paired doubles
        [0, 5, 3, 5, 0],        // five-note acknowledge
        [0, 2, 0, 2, 4],        // stutter rise
        [4, 2, 4],              // bounce
        [0, 1],                 // tight semitone chirp
        [6, 0, 3],              // drop then mid
    ];

    // Dynamics profiles: [attackVol, sustainVol] as multipliers
    var LCARS2_DYNAMICS = [
        [1.0, 0.8],   // standard
        [0.6, 1.0],   // swell
        [1.0, 0.4],   // accent-decay
        [0.8, 0.8],   // flat/even
        [1.0, 0.6],   // punch
    ];

    function computeNcars2Tone(safeText, targetDurSec) {
        var hash   = cyrb53(safeText, config.globalSeed);
        var random = mulberry32(hash);

        var freqs   = LCARS2_FREQS;
        var freqLen = freqs.length;

        // Pick a pattern
        var patIdx  = Math.floor(random() * LCARS2_PATTERNS.length);
        var pattern = LCARS2_PATTERNS[patIdx];

        // Pick a root position — leave headroom for pattern offsets
        var maxOff = 0;
        for (var p = 0; p < pattern.length; p++) {
            if (pattern[p] > maxOff) maxOff = pattern[p];
        }
        var maxRoot = freqLen - 1 - maxOff;
        if (maxRoot < 0) maxRoot = 0;
        var rootIdx = Math.floor(random() * (maxRoot + 1));

        // Dynamics profile
        var dynIdx = Math.floor(random() * LCARS2_DYNAMICS.length);
        var dynamics = LCARS2_DYNAMICS[dynIdx];

        // Per-sequence timing: note duration 25–80ms, gap 8–30ms
        var noteDur = 0.025 + (random() * 0.055);
        noteDur = Math.max(0.02, noteDur * config.maxDuration);
        var noteGap = (0.008 + (random() * 0.022)) * config.maxDuration;

        // Occasional micro-rest insertion (longer gap before one note)
        var microRestIdx = (random() > 0.6 && pattern.length > 2)
            ? (1 + Math.floor(random() * (pattern.length - 1)))
            : -1;

        // ── Musical composition for long beeps (>500ms) ──
        // Same compositional principles as nCARS but adapted for the
        // brighter LCARS2 register: faster runs, wider leaps, sparkle
        // harmonics, and an airier feel with more breathing room.
        if (typeof targetDurSec === 'number' && targetDurSec > 0) {
            var baseTime2 = pattern.length * (noteDur + noteGap);

            if (targetDurSec > 0.5) {
                var maxNoteIdx2 = freqLen - 1;
                var composed2 = [];

                // Rhythm cells — brighter mode favours quicker, more ornamental rhythms
                var RHYTHMS2 = [
                    [0.7, 0.7, 1.0],                    // quick-quick-held
                    [1.0, 0.4, 0.4, 0.4],               // swing then triplet
                    [0.5, 0.5, 0.5, 1.5],               // three quick + sustained
                    [1.2, 0.4, 0.8],                     // dotted emphasis
                    [0.6, 0.6, 0.6, 0.6],               // even quick four
                    [1.8],                                // held single
                    [0.4, 1.0, 0.4],                     // grace-held-grace
                    [0.5, 1.0, 0.5, 1.0],               // alternating feel
                    [0.3, 0.3, 0.3, 0.3, 1.0],          // rapid run into held
                    [1.0, 0.6, 1.0, 0.4],               // layered groove
                ];

                // Contours — wider intervals for sparkle, more expressive leaps
                var CONTOURS2 = [
                    [0, 3, 6, 3],              // wide arch (octave span)
                    [6, 3, 0, 3],              // valley sweep
                    [0, 1, 3, 6],              // climbing bright
                    [6, 4, 2, 0],              // cascading descent
                    [0, 6, 4, 2],              // leap then step down
                    [0, 2, 0, 4, 2],           // zigzag with wide leap
                    [3, 0, 6, 3],              // bounce through registers
                    [0, 4, 2, 6, 0],           // explore and return home
                    [0, 0, 3, 6],              // pedal then burst upward
                    [6, 6, 3, 0],              // double high then cascade
                    [0, 2, 4, 6, 8],           // long ascending run
                    [0, -1, 0, 3, 6],          // dip then soar (ornamental)
                ];

                // Tonal center — mid-range of LCARS2 table for room both ways
                var home2 = rootIdx + Math.floor(random() * 4);
                if (home2 > maxNoteIdx2 - 8) home2 = maxNoteIdx2 - 8;
                if (home2 < 0) home2 = 0;

                // Density: LCARS2 is more articulate, slightly faster
                var density2 = 8 + random() * 14;  // 8–22 notes/sec
                var estTotal2 = Math.ceil(targetDurSec * density2);
                estTotal2 = Math.min(estTotal2, 500);

                // Section structure
                var numSec2 = Math.max(2, Math.floor(targetDurSec / 0.7));
                numSec2 = Math.min(numSec2, 10);
                var notesPerSec2 = Math.ceil(estTotal2 / numSec2);

                // Register arc — LCARS2 uses a brighter, more dramatic arc
                var secRoots2 = [];
                for (var s2i = 0; s2i < numSec2; s2i++) {
                    var arcPos2 = s2i / Math.max(numSec2 - 1, 1);
                    // Asymmetric arch peaking at ~60%, brighter peak
                    var arc2 = Math.sin(arcPos2 * Math.PI * 0.9);
                    var range2 = Math.min(8, maxNoteIdx2 - home2);
                    var sRoot2 = home2 + Math.round(arc2 * range2);
                    sRoot2 += Math.floor(random() * 3) - 1;
                    if (sRoot2 < 0) sRoot2 = 0;
                    if (sRoot2 > maxNoteIdx2 - 6) sRoot2 = maxNoteIdx2 - 6;
                    secRoots2.push(sRoot2);
                }

                // Recurring motif for recognition
                var motif2 = [];
                var mc2 = CONTOURS2[Math.floor(random() * CONTOURS2.length)];
                for (var m2i = 0; m2i < Math.min(4, mc2.length); m2i++) {
                    motif2.push(mc2[m2i]);
                }

                // Generate sections
                for (var sc2 = 0; sc2 < numSec2 && composed2.length < estTotal2; sc2++) {
                    var scRoot2 = secRoots2[sc2];
                    var scProg2 = sc2 / Math.max(numSec2 - 1, 1);

                    // Section energy curve
                    var scEnergy2;
                    if (scProg2 < 0.30) scEnergy2 = 0.50 + scProg2 * 1.5;
                    else if (scProg2 < 0.65) scEnergy2 = 0.95 + (scProg2 - 0.30) * 0.15;
                    else scEnergy2 = 1.0 - (scProg2 - 0.65) * 1.8;
                    scEnergy2 = Math.max(0.30, Math.min(1.0, scEnergy2));

                    var rCell2 = RHYTHMS2[Math.floor(random() * RHYTHMS2.length)];
                    var contour2 = CONTOURS2[Math.floor(random() * CONTOURS2.length)];

                    var phrases2 = 1 + Math.floor(random() * 3);

                    for (var ph2 = 0; ph2 < phrases2 && composed2.length < estTotal2; ph2++) {
                        var useMotif2 = (sc2 > 0 && ph2 === 0 && random() > 0.50);

                        // Breathing space between phrases
                        if (composed2.length > 0 && random() > 0.25) {
                            var rest2 = 0.3 + random() * 1.5;
                            composed2.push({
                                idx: -1, dur: noteDur * rest2,
                                gap: 0, vol: 0, bend: 1.0, ghost: false
                            });
                        }

                        var phContour2 = useMotif2
                            ? motif2
                            : contour2.slice(0, rCell2.length + Math.floor(random() * 3));
                        if (phContour2.length < 1) phContour2 = [0];

                        var phShift2 = Math.floor(random() * 4) - 2;

                        for (var n2i = 0; n2i < phContour2.length && composed2.length < estTotal2; n2i++) {
                            var step2 = phContour2[n2i] + phShift2;
                            var nIdx2 = scRoot2 + step2;
                            if (nIdx2 < 0) nIdx2 = (-nIdx2) % (maxNoteIdx2 + 1);
                            if (nIdx2 > maxNoteIdx2) nIdx2 = maxNoteIdx2 - (nIdx2 - maxNoteIdx2) % Math.max(maxNoteIdx2, 1);
                            if (nIdx2 < 0) nIdx2 = 0;
                            if (nIdx2 > maxNoteIdx2) nIdx2 = maxNoteIdx2;

                            var rM2 = rCell2[n2i % rCell2.length];
                            var isLast2 = (n2i === phContour2.length - 1);
                            var gM2 = isLast2 ? (1.4 + random() * 1.2) : (0.5 + random() * 0.6);

                            var nVol2;
                            if (n2i === 0) nVol2 = 0.82 + random() * 0.18;
                            else if (isLast2) nVol2 = 0.40 + random() * 0.25;
                            else nVol2 = 0.50 + random() * 0.35;
                            nVol2 *= scEnergy2;

                            var bD2 = (random() > 0.5) ? 1 : -1;
                            var bA2 = 1 + (bD2 * random() * 0.003);

                            // Decide if this note gets an octave ghost
                            var addGhost = (random() > 0.78 && freqs[nIdx2] * 2 <= 4200);

                            composed2.push({
                                idx: nIdx2, dur: noteDur * rM2 * (0.80 + random() * 0.4),
                                gap: noteGap * gM2, vol: nVol2, bend: bA2, ghost: addGhost
                            });
                        }

                        // Resolve to home on final section
                        if (sc2 === numSec2 - 1 && ph2 === phrases2 - 1 && composed2.length > 0) {
                            var lst2 = composed2[composed2.length - 1];
                            if (lst2.idx >= 0) {
                                composed2.push({
                                    idx: home2, dur: noteDur * 2.5,
                                    gap: noteGap * 0.5, vol: 0.75 * scEnergy2,
                                    bend: 1.0, ghost: false
                                });
                            }
                        }
                    }
                }

                // Build voices from composed notes
                var voices = [];
                var time = 0;

                for (var c2i = 0; c2i < composed2.length; c2i++) {
                    var cn = composed2[c2i];

                    if (cn.idx === -1) {
                        time += cn.dur;
                        if (time >= targetDurSec) break;
                        continue;
                    }

                    var cIdx2 = Math.max(0, Math.min(cn.idx, maxNoteIdx2));
                    var cFreq2 = freqs[cIdx2];
                    var cDur2 = Math.max(0.015, cn.dur);

                    // Piece-level envelope
                    var pp2 = time / targetDurSec;
                    var pe2 = 1.0;
                    if (pp2 < 0.05) pe2 = 0.25 + (pp2 / 0.05) * 0.75;
                    else if (pp2 > 0.92) pe2 = 0.15 + ((1.0 - pp2) / 0.08) * 0.85;
                    var cVol2 = Math.min(1.0, cn.vol * pe2);

                    voices.push({
                        freq:         cFreq2,
                        endFreq:      cFreq2 * cn.bend,
                        waveform:     'sine',
                        volume:       cVol2,
                        duration:     cDur2,
                        delay:        time,
                        vibratoRate:  0,
                        vibratoDepth: 0
                    });

                    // Octave ghost for shimmer
                    if (cn.ghost) {
                        voices.push({
                            freq:         cFreq2 * 2,
                            endFreq:      cFreq2 * 2 * cn.bend,
                            waveform:     'sine',
                            volume:       cVol2 * 0.12,
                            duration:     cDur2 * 0.65,
                            delay:        time,
                            vibratoRate:  0,
                            vibratoDepth: 0
                        });
                    }

                    time += cDur2 + Math.max(0, cn.gap);
                    if (time >= targetDurSec) break;
                }

                return voices;

            } else {
                var scaleFactor2 = targetDurSec / Math.max(baseTime2, 0.01);
                noteDur = noteDur * Math.max(scaleFactor2, 0.5);
                noteGap = noteGap * Math.max(scaleFactor2, 0.5);
            }
        }

        // Short beeps / default duration — simple pattern playback
        var voices = [];
        var time = 0;

        for (var i = 0; i < pattern.length; i++) {
            // Micro-rest for short beeps
            if (i === microRestIdx) {
                time += 0.03 + (random() * 0.03);
            }

            var idx = rootIdx + Math.max(0, pattern[i]);
            if (idx >= freqLen) idx = freqLen - 1;
            var freq = freqs[idx];

            var durJitter = 1.0 + ((random() - 0.5) * 0.30);
            var dur = Math.max(0.02, noteDur * durJitter);

            var volBase = (i === 0) ? dynamics[0] : dynamics[1];
            volBase *= (0.9 + random() * 0.2);
            if (volBase > 1.0) volBase = 1.0;

            var bendDir = (random() > 0.5) ? 1 : -1;
            var bend = 1 + (bendDir * random() * 0.005);

            voices.push({
                freq:         freq,
                endFreq:      freq * bend,
                waveform:     'sine',
                volume:       volBase,
                duration:     dur,
                delay:        time,
                vibratoRate:  0,
                vibratoDepth: 0
            });

            // Dual-layer octave ghost (20% chance)
            if (random() > 0.80 && freq * 2 <= 4200) {
                voices.push({
                    freq:         freq * 2,
                    endFreq:      freq * 2 * bend,
                    waveform:     'sine',
                    volume:       volBase * 0.15,
                    duration:     dur * 0.7,
                    delay:        time,
                    vibratoRate:  0,
                    vibratoDepth: 0
                });
            }

            time += dur + noteGap;
            if (typeof targetDurSec === 'number' && targetDurSec > 0 && time >= targetDurSec) break;
        }

        return voices;
    }

    // ─── 8. Play a single tone (internal) ────────────────────

    function playTone(tone, pitchMultiplier, startDelay) {
        var ac   = getCtx();
        var now  = ac.currentTime + (startDelay || 0);
        var fade = config.fadeDuration;
        var pm   = (typeof pitchMultiplier === 'number' && pitchMultiplier > 0) ? pitchMultiplier : 1.0;

        var osc  = ac.createOscillator();
        var gain = ac.createGain();

        var sf = Math.max(20, Math.min(5000, tone.startFreq * pm));
        var ef = Math.max(20, Math.min(5000, tone.endFreq * pm));

        osc.type = tone.waveform;
        osc.frequency.setValueAtTime(sf, now);
        osc.frequency.exponentialRampToValueAtTime(ef, now + tone.duration);

        // Fix 2: Clamp hold-point so it never precedes the attack end
        // (avoids overlapping automation events on very short durations)
        var vol = config.masterVolume;
        var attackEnd = now + fade;
        var holdPoint = Math.max(attackEnd, now + tone.duration - fade);
        gain.gain.setValueAtTime(0, now);
        gain.gain.linearRampToValueAtTime(vol, attackEnd);
        gain.gain.setValueAtTime(vol, holdPoint);
        gain.gain.linearRampToValueAtTime(0, now + tone.duration);

        osc.connect(gain);
        gain.connect(getOutput());

        // Track as active voice
        activeOsc  = osc;
        activeGain = gain;

        osc.start(now);
        osc.stop(now + tone.duration);

        osc.onended = function () {
            // Only clear refs if this is still the active voice
            if (activeOsc === osc) {
                activeOsc  = null;
                activeGain = null;
            }
            osc.disconnect();
            gain.disconnect();
        };
    }

    // ─── 9. Core Execution Function ──────────────────────────

    /**
     * nbeep(text, loop, pitchMultiplier, durationMs)
     * Triggers a procedurally generated UI sound from a string identifier.
     * Only one sound plays at a time — calling nbeep() always kills the
     * previous sound immediately (no stacking, no overlap).
     *
     * @param {string}  text  Identifier (max 512 chars). Same text + same
     *                        globalSeed = bit-accurate identical waveform.
     * @param {boolean|string} loop
     *        false (default): single shot.
     *        true:  repeats the tone from `text` indefinitely.
     *        string: dual-text alternating loop — hashes `text` and
     *                `loop` separately and cycles A → B → A → B.
     * @param {number} pitchMultiplier
     *        Optional pitch scaling factor (default 1.0).
     *        Values < 1.0 lower the pitch, > 1.0 raise it.
     *        Used by sliders to map value position to pitch:
     *        50% → 1.0 (normal), 0% → 0.5 (octave down), 100% → 2.0 (octave up).
     * @param {number} durationMs
     *        Optional per-call duration in milliseconds. Overrides the
     *        globally configured duration. When > 500ms, complexity is
     *        automatically scaled up so the beep sounds rich, not droney.
     *        Defaults to null (use global config).
     */
    function nbeep(text, loop, pitchMultiplier, durationMs) {
        if (!text || typeof text !== 'string') return;

        // Record activity so staleness detection stays calibrated
        lastActivityTs = Date.now();

        var pm = (typeof pitchMultiplier === 'number' && pitchMultiplier > 0) ? pitchMultiplier : 1.0;
        var targetDurSec = (typeof durationMs === 'number' && durationMs > 0) ? durationMs / 1000 : null;

        // Always kill previous sound first — if something was playing,
        // delay the new sound by KILL_FADE so the ramp-down completes
        // before the new attack begins (prevents speaker-cone pops).
        var hadActive = killActive();
        var startDelay = hadActive ? KILL_FADE : 0;

        var safeText = text.substring(0, 512);
        var safeAlt  = (typeof loop === 'string') ? loop.substring(0, 512) : null;
        var doLoop   = !!loop;
        var mode = config.soundMode || 'standard';

        if (mode === 'ncars2') {
            // ── nCARS 2 mode: high-register LCARS with extended variety ──
            var nc2Voices = computeNcars2Tone(safeText, targetDurSec);
            playHarmonicTone(nc2Voices, pm, startDelay);

            if (doLoop) {
                loopText    = safeText;
                loopAltText = safeAlt;
                loopPhase   = false;
                var nc2MaxDur = 0;
                nc2Voices.forEach(function (v) {
                    var d = v.delay + v.duration;
                    if (d > nc2MaxDur) nc2MaxDur = d;
                });
                if (safeAlt) {
                    var altV = computeNcars2Tone(safeAlt);
                    altV.forEach(function (v) {
                        var d = v.delay + v.duration;
                        if (d > nc2MaxDur) nc2MaxDur = d;
                    });
                }
                var nc2Interval = Math.round((nc2MaxDur + 0.15) * 1000);
                loopTimer = setInterval(function () {
                    var loopDelay = killOscillators() ? KILL_FADE : 0;
                    var t = (loopAltText && (loopPhase = !loopPhase)) ? loopAltText : loopText;
                    var nv2 = computeNcars2Tone(t);
                    playHarmonicTone(nv2, pm, loopDelay);
                }, nc2Interval);
            }
        } else if (mode === 'ncars') {
            // ── nCARS mode: LCARS-style computer beeps ──
            var ncVoices = computeNcarsTone(safeText, targetDurSec);
            playHarmonicTone(ncVoices, pm, startDelay);

            if (doLoop) {
                loopText    = safeText;
                loopAltText = safeAlt;
                loopPhase   = false;
                var ncMaxDur = 0;
                ncVoices.forEach(function (v) {
                    var d = v.delay + v.duration;
                    if (d > ncMaxDur) ncMaxDur = d;
                });
                if (safeAlt) {
                    var altNv = computeNcarsTone(safeAlt);
                    altNv.forEach(function (v) {
                        var d = v.delay + v.duration;
                        if (d > ncMaxDur) ncMaxDur = d;
                    });
                }
                var ncInterval = Math.round((ncMaxDur + 0.15) * 1000);
                loopTimer = setInterval(function () {
                    var loopDelay = killOscillators() ? KILL_FADE : 0;
                    var t = (loopAltText && (loopPhase = !loopPhase)) ? loopAltText : loopText;
                    var nv = computeNcarsTone(t);
                    playHarmonicTone(nv, pm, loopDelay);
                }, ncInterval);
            }
        } else if (mode === 'harmonic' && config.useMusicalScale) {
            // ── Harmonic mode: rich multi-voice chords ──
            var voices = computeHarmonicTone(safeText, targetDurSec);
            playHarmonicTone(voices, pm, startDelay);

            if (doLoop) {
                loopText    = safeText;
                loopAltText = safeAlt;
                loopPhase   = false;
                // Longest voice duration + 100ms gap
                var maxDur = 0;
                voices.forEach(function (v) {
                    var d = v.delay + v.duration;
                    if (d > maxDur) maxDur = d;
                });
                if (safeAlt) {
                    var altHv = computeHarmonicTone(safeAlt);
                    altHv.forEach(function (v) {
                        var d = v.delay + v.duration;
                        if (d > maxDur) maxDur = d;
                    });
                }
                var interval = Math.round((maxDur + 0.10) * 1000);
                loopTimer = setInterval(function () {
                    var loopDelay = killOscillators() ? KILL_FADE : 0;
                    var t = (loopAltText && (loopPhase = !loopPhase)) ? loopAltText : loopText;
                    var v = computeHarmonicTone(t);
                    playHarmonicTone(v, pm, loopDelay);
                }, interval);
            }
        } else {
            // ── Standard mode: single tone ──
            var tone = computeTone(safeText, targetDurSec);
            playTone(tone, pm, startDelay);

            if (doLoop) {
                loopText    = safeText;
                loopAltText = safeAlt;
                loopPhase   = false;
                var interval = Math.round((tone.duration + 0.08) * 1000);
                if (safeAlt) {
                    var altTone = computeTone(safeAlt);
                    var altInt  = Math.round((altTone.duration + 0.08) * 1000);
                    if (altInt > interval) interval = altInt;
                }
                loopTimer = setInterval(function () {
                    var loopDelay = killOscillators() ? KILL_FADE : 0;
                    var tx = (loopAltText && (loopPhase = !loopPhase)) ? loopAltText : loopText;
                    var t = computeTone(tx);
                    playTone(t, pm, loopDelay);
                }, interval);
            }
        }
    }

    // ─── 10. Public API ──────────────────────────────────────

    return {
        nbeep:      nbeep,
        killActive: killActive,
        warmUp:     warmUp,
        config:     config,
        SCALES:     SCALES,
        cyrb53:     cyrb53
    };
})();

// Global convenience aliases
window.nbeep        = nDesignAudio.nbeep;
window.nDesignAudio = nDesignAudio;
