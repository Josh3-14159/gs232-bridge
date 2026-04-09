"""
serial_port.py — PTY creation, symlink management, and command dispatch.

Opens a POSIX PTY pair.  The slave end is symlinked to the path
configured as pty_symlink (e.g. /dev/ttyGS232).  The telescope
software opens that symlink as a normal serial port.

Reads from the master fd, passes each line to gs232_parser, and
dispatches to the controller.  Runs in its own thread.
"""

import errno
import logging
import os
import select
import termios
import threading
from configparser import ConfigParser
from pathlib import Path

from controller import Controller
from gs232_parser import ParseError, format_az, format_el, format_position, parse

log = logging.getLogger(__name__)

_READ_TIMEOUT = 1.0   # seconds — allows clean shutdown checks
_MAX_LINE     = 256   # bytes — guard against runaway input


class SerialPort:
    def __init__(self, config: ConfigParser, controller: Controller) -> None:
        self._cfg        = config
        self._controller = controller
        self._stop_ev    = threading.Event()
        self._master_fd: int | None = None
        self._symlink: Path | None  = None
        self._thread = threading.Thread(
            target=self._loop,
            name='serial_port',
            daemon=True,
        )

    def start(self) -> None:
        self._open_pty()
        self._thread.start()
        log.info("serial port thread started")

    def stop(self) -> None:
        self._stop_ev.set()
        self._thread.join(timeout=3.0)
        self._cleanup()

    # ------------------------------------------------------------------
    # PTY setup
    # ------------------------------------------------------------------

    def _open_pty(self) -> None:
        master_fd, slave_fd = os.openpty()

        # Configure the slave side to match the configured baud rate.
        # The telescope software sets its own baud when it opens the symlink,
        # but we set it here too for consistency.
        baud_str = self._cfg.get('serial', 'baud', fallback='9600')
        baud_map = {
            '9600':   termios.B9600,
            '19200':  termios.B19200,
            '38400':  termios.B38400,
            '57600':  termios.B57600,
            '115200': termios.B115200,
        }
        baud_const = baud_map.get(baud_str, termios.B9600)
        attrs = termios.tcgetattr(slave_fd)
        attrs[4] = baud_const   # ispeed
        attrs[5] = baud_const   # ospeed
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

        # Get slave name before closing slave fd —
        # os.ttyname() on the master returns /dev/pts/ptmx, not the slave path.
        slave_name = os.ttyname(slave_fd)

        # Slave fd can be closed in this process — the PTY stays alive as
        # long as the master fd is open.
        os.close(slave_fd)

        symlink_path = Path(self._cfg.get('serial', 'pty_symlink'))

        # Remove stale symlink if present
        try:
            symlink_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove stale symlink %s: %s", symlink_path, exc)

        symlink_path.symlink_to(slave_name)

        self._master_fd = master_fd
        self._symlink   = symlink_path
        log.info("PTY ready: %s -> %s", symlink_path, slave_name)

    def _cleanup(self) -> None:
        if self._symlink:
            try:
                self._symlink.unlink(missing_ok=True)
            except OSError:
                pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass

    def _reopen_pty(self) -> None:
        # Close the old master fd and open a fresh PTY, keeping the symlink path.
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self._open_pty()
        log.info('PTY recreated, ready for new client connection')

    # ------------------------------------------------------------------
    # Read / dispatch loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        buf = b''

        while not self._stop_ev.is_set():
            try:
                ready, _, _ = select.select([self._master_fd], [], [], _READ_TIMEOUT)
            except (ValueError, OSError) as exc:
                log.error("select error on PTY master: %s", exc)
                break

            if not ready:
                continue

            try:
                chunk = os.read(self._master_fd, 256)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # EIO means the client disconnected (screen quit, SW closed port).
                    # Recreate the PTY so a fresh client can reconnect cleanly.
                    log.info("PTY client disconnected, recreating PTY...")
                    self._stop_ev.wait(timeout=_READ_TIMEOUT)
                    buf = b''
                    try:
                        self._reopen_pty()
                    except OSError as reopen_exc:
                        log.error("PTY reopen failed: %s", reopen_exc)
                        break
                    continue
                log.error("PTY read error: %s", exc)
                break

            buf += chunk

            # Process all complete lines in the buffer
            while b'\n' in buf or b'\r' in buf:
                # Split on either CR or LF; keep remainder
                for sep in (b'\r\n', b'\n', b'\r'):
                    if sep in buf:
                        line, buf = buf.split(sep, 1)
                        self._dispatch(line.decode(errors='replace'))
                        break
                else:
                    break  # no separator found — wait for more data

            # Guard against a client sending garbage with no newlines
            if len(buf) > _MAX_LINE:
                log.warning("input buffer overflow, flushing")
                buf = b''

    def _dispatch(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return

        log.debug("rx: %r", raw)

        try:
            result = parse(raw)
        except ParseError as exc:
            log.debug("parse error: %s", exc)
            return

        cmd = result['cmd']

        if cmd == 'C':
            az, el, valid = self._controller.read_position()
            if valid:
                response = format_position(az, el)
            else:
                response = b"AZ=000 EL=000\r\n"
            self._write(response)

        elif cmd == 'B':
            az, _, valid = self._controller.read_position()
            self._write(format_az(az if valid else 0.0))

        elif cmd == 'A':
            _, el, valid = self._controller.read_position()
            self._write(format_el(el if valid else 0.0))

        elif cmd == 'W':
            self._controller.set_target(result['az'], result['el'])

        elif cmd == 'M':
            self._controller.set_target_az(result['az'])

        elif cmd == 'S':
            self._controller.stop_motion()

    def _write(self, data: bytes) -> None:
        log.debug("tx: %r", data)
        try:
            os.write(self._master_fd, data)
        except OSError as exc:
            log.warning("PTY write error: %s", exc)
