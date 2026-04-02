"""Agent watchdog — detached supervisor that keeps the agent alive.

Runs as the top-level process.  Spawns the real agent as a child, monitors
its liveness via a health file, and restarts it when unresponsive.

Usage (from any agent directory):
    python3 watchdog.py [--port 1903]

Or from elsewhere:
    python3 watchdog.py --agent-dir /path/to/agent_linux

The watchdog:
  • spawns  `python3 run.py run [--port N]`  as a child process
  • passes CLUSTERFLOCK_HEALTH_FILE env var so link.py knows where to write
  • expects the child to touch the health file every heartbeat
  • if health file goes stale for STALE_SEC → SIGTERM + restart
  • on child exit (crash) → immediate restart
  • signals (SIGTERM, SIGINT) → forwarded to child, then watchdog exits cleanly

Health file: /tmp/clusterflock_agent.alive  (written by link.py)
"""

import os
import signal
import subprocess
import sys
import time

# ── Tunables ─────────────────────────────────────────────────────────────

STALE_SEC = 45          # agent considered dead if no heartbeat this long
CHECK_SEC = 5           # watchdog poll interval
RESTART_DELAY = 3       # wait before restart after kill
MAX_RAPID = 5           # max restarts within RAPID_WINDOW
RAPID_WINDOW = 120      # seconds
BACKOFF_SEC = 30        # pause when rapid-restarting

HEALTH_FILE = "/tmp/clusterflock_agent.alive"


def _health_age():
    """Seconds since health file was last updated.  None if missing."""
    try:
        return time.time() - os.path.getmtime(HEALTH_FILE)
    except OSError:
        return None


def _cleanup():
    try:
        os.unlink(HEALTH_FILE)
    except OSError:
        pass


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] watchdog: {msg}", flush=True)


def run(agent_dir, extra_args):
    """Main loop — spawn agent, monitor, restart on failure or stall."""
    run_py = os.path.join(agent_dir, "run.py")
    if not os.path.isfile(run_py):
        _log(f"ERROR: {run_py} not found")
        sys.exit(1)

    cmd = [sys.executable, "-u", run_py] + extra_args
    _log(f"supervisor for: {' '.join(cmd)}")
    _log(f"health file:    {HEALTH_FILE}")
    _log(f"stale after:    {STALE_SEC}s")

    restart_times = []
    child = None

    def _forward(signum, _frame):
        _log(f"signal {signum} → forwarding to agent")
        if child and child.poll() is None:
            child.send_signal(signum)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        _cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    while True:
        # ── Rate limit restarts ──────────────────────────────────────
        now = time.time()
        restart_times = [t for t in restart_times if now - t < RAPID_WINDOW]
        if len(restart_times) >= MAX_RAPID:
            _log(f"{len(restart_times)} restarts in {RAPID_WINDOW}s — "
                 f"backing off {BACKOFF_SEC}s")
            time.sleep(BACKOFF_SEC)

        # ── Spawn agent ──────────────────────────────────────────────
        _cleanup()
        env = os.environ.copy()
        env["CLUSTERFLOCK_HEALTH_FILE"] = HEALTH_FILE
        child = subprocess.Popen(cmd, cwd=agent_dir, env=env)
        _log(f"agent started (pid {child.pid})")
        restart_times.append(time.time())

        # Grace period for startup (agent needs time to register, import, etc.)
        grace_until = time.time() + STALE_SEC

        # ── Monitor loop ─────────────────────────────────────────────
        needs_restart = False
        while True:
            time.sleep(CHECK_SEC)

            rc = child.poll()
            if rc is not None:
                _log(f"agent exited (code {rc})")
                break

            age = _health_age()
            if age is None:
                if time.time() > grace_until:
                    _log(f"no health file after {STALE_SEC}s — killing")
                    needs_restart = True
                    break
            elif age > STALE_SEC:
                _log(f"agent unresponsive ({age:.0f}s stale) — killing")
                needs_restart = True
                break

        # ── Kill if still running ────────────────────────────────────
        if needs_restart and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
                _log("terminated")
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                _log("killed (SIGKILL)")

        _cleanup()
        _log(f"restarting in {RESTART_DELAY}s...")
        time.sleep(RESTART_DELAY)


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="ClusterFlock Agent Watchdog")
    parser.add_argument("--agent-dir", default=None,
                        help="Path to agent directory (default: same dir as watchdog.py)")
    parser.add_argument("--port", type=int, default=1903,
                        help="Agent listen port (forwarded to run.py)")

    args = parser.parse_args()

    if args.agent_dir:
        agent_dir = os.path.abspath(args.agent_dir)
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        if os.path.isfile(os.path.join(here, "run.py")):
            agent_dir = here
        else:
            print("ERROR: no run.py found next to watchdog.py. "
                  "Use --agent-dir.")
            sys.exit(1)

    extra_args = ["--port", str(args.port)]
    run(agent_dir, extra_args)


if __name__ == "__main__":
    main()
