"""
watchdog.py — Periodic liveness checks against the FUSE filesystem.

Runs in its own thread.  On consecutive ping failures it:
  1. Tells the controller to enter fault state (stops motors).
  2. Logs the fault.
  3. Waits for the filesystem to recover (keeps pinging).
  4. Clears the fault state when pings succeed again.

No reconnection logic is needed here — the rp2040-gpio-fs daemon itself
handles serial reconnection.  We just need to stop the motors while it
is offline and resume when it comes back.
"""

import logging
import threading
from configparser import ConfigParser

from gpio_backend import GPIOBackend
from controller import Controller

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(
        self,
        config: ConfigParser,
        backend: GPIOBackend,
        controller: Controller,
    ) -> None:
        self._cfg        = config
        self._hw         = backend
        self._controller = controller
        self._stop_ev    = threading.Event()
        self._thread     = threading.Thread(
            target=self._loop,
            name='watchdog',
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        log.info("watchdog started")

    def stop(self) -> None:
        self._stop_ev.set()
        self._thread.join(timeout=5.0)
        log.info("watchdog stopped")

    # ------------------------------------------------------------------
    # Watchdog loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        failures     = 0
        max_failures = self._cfg.getint('watchdog', 'max_failures')
        faulted      = False

        while not self._stop_ev.is_set():
            interval = self._cfg.getfloat('watchdog', 'ping_interval')

            ok = self._hw.ping()

            if ok:
                if failures > 0:
                    log.info("watchdog ping recovered after %d failure(s)", failures)
                failures = 0

                if faulted:
                    faulted = False
                    self._controller.set_fault(False)
                    log.info("watchdog cleared fault — hardware is back online")

            else:
                failures += 1
                log.warning(
                    "watchdog ping failed (%d/%d)",
                    failures, max_failures,
                )

                if not faulted and failures >= max_failures:
                    faulted = True
                    self._controller.set_fault(True)
                    log.error(
                        "watchdog declaring fault after %d consecutive failures",
                        failures,
                    )

            self._stop_ev.wait(timeout=interval)
