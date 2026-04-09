"""
gpio_backend.py — FUSE filesystem I/O and encoder calibration.

All hardware interaction goes through this module.  The rest of the
bridge only ever calls read_az(), read_el(), set_pin(), and clear_all().
Calibration constants are loaded from a Config object and can be reloaded
at runtime without touching this module.
"""

import math
import os
import logging
from configparser import ConfigParser
from pathlib import Path

log = logging.getLogger(__name__)


class GPIOError(OSError):
    """Raised when a FUSE filesystem operation fails."""
    pass


class GPIOBackend:
    """
    Wraps /mnt/rp2040/gpio/gpioN/{mode,value} files.

    All calibration constants are read from the live Config object on
    every call, so a SIGHUP reload takes effect immediately — no restart
    needed.
    """

    def __init__(self, config: ConfigParser) -> None:
        self._cfg = config
        self._initialised: set[int] = set()

    # ------------------------------------------------------------------
    # Public API used by the controller
    # ------------------------------------------------------------------

    def read_az(self) -> float:
        """Return current azimuth in degrees."""
        pin = self._cfg.getint('gpio', 'pin_az_enc')
        raw = self._read_adc(pin)
        return self._enc2az(raw)

    def read_el(self) -> float:
        """Return current elevation in degrees."""
        pin = self._cfg.getint('gpio', 'pin_el_enc')
        raw = self._read_adc(pin)
        return self._enc2el(raw)

    def set_pin(self, pin: int, value: int) -> None:
        """Drive an output pin high (1) or low (0)."""
        self._ensure_output(pin)
        self._write(pin, 'value', str(value))

    def clear_all(self) -> None:
        """
        Drive all four motor direction pins low.
        Called on stop command, watchdog fault, and clean shutdown.
        """
        for key in ('pin_cw', 'pin_ccw', 'pin_up', 'pin_dn'):
            pin = self._cfg.getint('gpio', key)
            try:
                self.set_pin(pin, 0)
            except GPIOError as exc:
                log.warning("clear_all: could not clear pin %d: %s", pin, exc)

    def ping(self) -> bool:
        """
        Lightweight liveness check: set az encoder to ADC mode and read
        it.  Returns True if the FUSE filesystem responds within the
        normal path, False on any I/O error.
        """
        try:
            pin = self._cfg.getint('gpio', 'pin_az_enc')
            self._read_adc(pin)
            return True
        except GPIOError:
            return False

    # ------------------------------------------------------------------
    # Calibration math — constants pulled from config on every call
    # so they update immediately after SIGHUP
    # ------------------------------------------------------------------

    def _enc2az(self, enc: float) -> float:
        c = self._cfg['calibration']
        az_min     = float(c['az_min'])
        az_max     = float(c['az_max'])
        az_min_ref = float(c['az_min_ref'])
        az_range   = float(c['az_range'])
        enc -= az_min_ref
        return enc * az_range / (az_max - az_min)

    def _enc2el(self, enc: float) -> float:
        c = self._cfg['calibration']
        el_flip_offset = float(c['el_flip_offset'])
        el_top         = float(c['el_top'])
        el_bot         = float(c['el_bot'])
        el_arm         = float(c['el_arm'])
        el_len_min     = float(c['el_len_min'])
        b              = float(c['el_b'])
        cc             = float(c['el_c'])
        el_offset      = float(c['el_offset'])

        enc = 4096 - enc
        enc -= el_flip_offset
        length = enc * el_arm / (el_top - el_bot) + el_len_min

        cos_arg = (b**2 + cc**2 - 60**2 - length**2) / (2 * b * cc)
        # Clamp to [-1, 1] to guard against floating-point noise at limits
        cos_arg = max(-1.0, min(1.0, cos_arg))
        ang = math.acos(cos_arg)
        return 180.0 - el_offset - math.degrees(ang)

    # ------------------------------------------------------------------
    # FUSE filesystem helpers
    # ------------------------------------------------------------------

    def _gpio_path(self, pin: int, filename: str) -> Path:
        mount = self._cfg.get('gpio', 'fuse_mount')
        return Path(mount) / 'gpio' / f'gpio{pin}' / filename

    def _write(self, pin: int, filename: str, value: str) -> None:
        path = self._gpio_path(pin, filename)
        try:
            path.write_text(value + '\n')
        except OSError as exc:
            raise GPIOError(f"write {path} <- {value!r}: {exc}") from exc

    def _read(self, pin: int, filename: str) -> str:
        path = self._gpio_path(pin, filename)
        try:
            return path.read_text().strip()
        except OSError as exc:
            raise GPIOError(f"read {path}: {exc}") from exc

    def _read_adc(self, pin: int) -> float:
        """Set pin to ADC mode (idempotent) and return raw 12-bit count."""
        self._ensure_adc(pin)
        raw_str = self._read(pin, 'value')
        # FUSE returns "raw_12bit volts" e.g. "2048 1.6504"
        try:
            raw_count = float(raw_str.split()[0])
        except (ValueError, IndexError) as exc:
            raise GPIOError(f"unexpected ADC response: {raw_str!r}") from exc
        return raw_count

    def _ensure_adc(self, pin: int) -> None:
        """Write mode=adc once per session; skip if already set."""
        key = (pin, 'adc')
        if key not in self._initialised:
            self._write(pin, 'mode', 'adc')
            self._initialised.add(key)

    def _ensure_output(self, pin: int) -> None:
        """Write mode=out once per session; skip if already set."""
        key = (pin, 'out')
        if key not in self._initialised:
            self._write(pin, 'mode', 'out')
            self._initialised.add(key)
