"""
main.py — GS-232 bridge entry point.

Wires all modules together and handles:
  SIGHUP  — reload config.ini in place (calibration, tolerances, etc.)
            No restart required. Takes effect on next control loop iteration.
  SIGTERM — clean shutdown: stop motors, remove PTY symlink, exit 0.
  SIGINT  — same as SIGTERM (Ctrl-C during development).

Usage:
    python3 main.py [--config /path/to/config.ini]

Typical systemd invocation (see gs232-bridge.service):
    ExecStart=/usr/bin/python3 /opt/gs232_bridge/main.py
    ExecReload=/bin/kill -HUP $MAINPID
"""

import argparse
import logging
import os
import signal
import sys
import threading
from configparser import ConfigParser
from pathlib import Path

from controller import Controller
from gpio_backend import GPIOBackend
from serial_port import SerialPort
from watchdog import Watchdog

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)-16s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
log = logging.getLogger('main')

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = Path(__file__).parent / 'config.ini'


def load_config(path: Path) -> ConfigParser:
    cfg = ConfigParser()
    if not path.exists():
        log.error("config file not found: %s", path)
        sys.exit(1)
    cfg.read(path)
    log.info("config loaded from %s", path)
    return cfg


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description='GS-232 telescope bridge')
    parser.add_argument(
        '--config', type=Path, default=DEFAULT_CONFIG,
        help=f'Path to config.ini (default: {DEFAULT_CONFIG})',
    )
    parser.add_argument(
        '--debug', action='store_true',
        help='Enable DEBUG-level logging',
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config_path = args.config.resolve()
    cfg = load_config(config_path)

    # Build modules — all share the same ConfigParser instance so a
    # SIGHUP reload updates everyone simultaneously.
    backend    = GPIOBackend(cfg)
    controller = Controller(cfg, backend)
    watchdog   = Watchdog(cfg, backend, controller)
    serial     = SerialPort(cfg, controller)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    shutdown_ev = threading.Event()

    def handle_sighup(signum, frame):
        """Reload config in place — no restart needed."""
        log.info("SIGHUP received — reloading %s", config_path)
        try:
            cfg.read(config_path)
            log.info("config reloaded successfully")
        except Exception as exc:
            log.error("config reload failed: %s", exc)

    def handle_shutdown(signum, frame):
        sig_name = signal.Signals(signum).name
        log.info("%s received — initiating clean shutdown", sig_name)
        shutdown_ev.set()

    signal.signal(signal.SIGHUP,  handle_sighup)
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT,  handle_shutdown)

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    log.info("starting gs232-bridge")

    try:
        serial.start()
        controller.start()
        watchdog.start()
        log.info("all modules running — PTY at %s", cfg.get('serial', 'pty_symlink'))

        # Block main thread until a shutdown signal arrives
        shutdown_ev.wait()

    finally:
        log.info("shutting down...")
        watchdog.stop()
        controller.stop()   # stops motors before serial closes
        serial.stop()
        log.info("gs232-bridge stopped cleanly")


if __name__ == '__main__':
    main()
