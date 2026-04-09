"""
controller.py — Az/El position state machine and control loop.

Runs in its own thread.  The serial_port thread calls set_target_*()
and read_position() — everything else is internal.

Motor drive logic is a direct port of the original MicroPython move()
function.  A SIGHUP config reload updates tolerance and loop_interval
on the next iteration automatically.
"""

import logging
import threading
import time
from configparser import ConfigParser
from typing import Optional

from gpio_backend import GPIOBackend, GPIOError

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, config: ConfigParser, backend: GPIOBackend) -> None:
        self._cfg     = config
        self._hw      = backend
        self._lock    = threading.Lock()
        self._stop_ev = threading.Event()

        # Targets — updated by serial thread via set_target_*()
        self._target_az: float = config.getfloat('control', 'default_az')
        self._target_el: float = config.getfloat('control', 'default_el')

        # Last known position — updated by control loop, read by serial thread
        self._current_az: float = 0.0
        self._current_el: float = 0.0
        self._position_valid: bool = False

        # Fault flag set by watchdog; clears when watchdog recovers
        self._faulted: bool = False

        self._thread = threading.Thread(
            target=self._loop,
            name='controller',
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        log.info("controller loop started")

    def stop(self) -> None:
        """Signal the loop to stop and wait for clean exit."""
        self._stop_ev.set()
        self._thread.join(timeout=2.0)
        try:
            self._hw.clear_all()
            log.info("controller stopped, all motors cleared")
        except GPIOError as exc:
            log.error("could not clear motors on stop: %s", exc)

    # ------------------------------------------------------------------
    # Thread-safe API for the serial layer
    # ------------------------------------------------------------------

    def set_target(self, az: float, el: float) -> None:
        with self._lock:
            self._target_az = az
            self._target_el = el
        log.info("target set: az=%.1f el=%.1f", az, el)

    def set_target_az(self, az: float) -> None:
        with self._lock:
            self._target_az = az
        log.info("target az set: %.1f", az)

    def stop_motion(self) -> None:
        """GS-232 S command — park target at current position."""
        with self._lock:
            self._target_az = self._current_az
            self._target_el = self._current_el
        log.info("stop command received, target parked at current position")

    def read_position(self) -> tuple[float, float, bool]:
        """
        Returns (az, el, valid).
        valid=False means the hardware hasn't been read yet or is faulted.
        """
        with self._lock:
            return self._current_az, self._current_el, self._position_valid

    # ------------------------------------------------------------------
    # Watchdog interface
    # ------------------------------------------------------------------

    def set_fault(self, faulted: bool) -> None:
        """Called by watchdog to engage/clear the fault state."""
        with self._lock:
            if faulted and not self._faulted:
                log.error("controller entering fault state — motors stopped")
            elif not faulted and self._faulted:
                log.info("controller fault cleared — resuming normal operation")
            self._faulted = faulted
        if faulted:
            try:
                self._hw.clear_all()
            except GPIOError as exc:
                log.error("could not clear motors on fault: %s", exc)

    # ------------------------------------------------------------------
    # Control loop (runs in dedicated thread)
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_ev.is_set():
            interval = self._cfg.getfloat('control', 'loop_interval')

            with self._lock:
                faulted   = self._faulted
                target_az = self._target_az
                target_el = self._target_el

            if faulted:
                self._stop_ev.wait(timeout=interval)
                continue

            try:
                az = self._hw.read_az()
                el = self._hw.read_el()

                with self._lock:
                    self._current_az     = az
                    self._current_el     = el
                    self._position_valid = True

                self._drive(az, el, target_az, target_el)

            except GPIOError as exc:
                log.warning("control loop hardware error: %s", exc)
                # Don't clear motors here — watchdog handles fault declaration
                # after consecutive failures

            self._stop_ev.wait(timeout=interval)

    def _drive(
        self,
        az: float,  el: float,
        tgt_az: float, tgt_el: float,
    ) -> None:
        """
        Direct port of the original MicroPython motor drive logic.
        Each motor direction pin is set high only when the error exceeds
        tolerance; both directions are low (stopped) when within tolerance.
        """
        tol = self._cfg.getfloat('control', 'tolerance')

        cfg = self._cfg['gpio']
        pin_cw  = int(cfg['pin_cw'])
        pin_ccw = int(cfg['pin_ccw'])
        pin_up  = int(cfg['pin_up'])
        pin_dn  = int(cfg['pin_dn'])

        try:
            self._hw.set_pin(pin_cw,  int(tgt_az > az + tol))
            self._hw.set_pin(pin_ccw, int(tgt_az < az - tol))
            self._hw.set_pin(pin_up,  int(tgt_el > el + tol))
            self._hw.set_pin(pin_dn,  int(tgt_el < el - tol))
        except GPIOError as exc:
            log.warning("drive error: %s", exc)
            raise
