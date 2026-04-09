# gs232-bridge

A Python daemon that presents a virtual GS-232 serial port to telescope
control software on a Linux PC, backed by an RP2040-Zero running the
[rp2040-gpio-fs](https://github.com/Josh3-14159/rp2040-gpio-fs) FUSE
firmware.

```
Telescope software  (Stellarium / ASCOM / custom)
        │  /dev/ttyGS232  (PTY symlink)
        ▼
  gs232-bridge  (this repo)
        │  /mnt/rp2040/gpio/...  (FUSE filesystem)
        ▼
  RP2040-Zero firmware  (rp2040-gpio-fs)
        │  GPIO
        ▼
  Az + El motor drivers  (CW / CCW / UP / DN relay outputs)
```

## Prerequisites

- Linux with Python 3.10+
- [rp2040-gpio-fs](https://github.com/Josh3-14159/rp2040-gpio-fs) firmware
  flashed and FUSE daemon mounted at `/mnt/rp2040`
- `sudo` access to install the systemd service

## Install

```bash
git clone https://github.com/<your-user>/gs232-bridge
cd gs232-bridge
sudo ./install.sh          # installs for current user
# or
sudo ./install.sh someuser # installs for a specific user
```

The script:
- Copies files to `/opt/gs232_bridge/`
- Installs `gs232-bridge@.service` into systemd
- Preserves an existing `config.ini` on upgrade (diffs it against the new default)
- Enables and starts the service

## Configuration

All tunable values live in `/opt/gs232_bridge/config.ini`.

| Section | Key | Description |
|---|---|---|
| `[serial]` | `pty_symlink` | Path telescope software connects to |
| `[gpio]` | `fuse_mount` | rp2040-gpio-fs mount point |
| `[gpio]` | `pin_*` | GPIO pin assignments |
| `[calibration]` | `az_*` / `el_*` | 12-bit ADC calibration constants |
| `[control]` | `tolerance` | Dead-band in degrees before motor stops |
| `[control]` | `loop_interval` | Control loop period (seconds) |
| `[watchdog]` | `ping_interval` | Seconds between FUSE liveness checks |
| `[watchdog]` | `max_failures` | Consecutive failures before fault |

**Live reload — no restart required:**

```bash
nano /opt/gs232_bridge/config.ini
systemctl reload gs232-bridge@$USER
```

Takes effect on the next control loop iteration (~100ms).

## Service management

```bash
systemctl status  gs232-bridge@$USER
systemctl stop    gs232-bridge@$USER
systemctl start   gs232-bridge@$USER
systemctl reload  gs232-bridge@$USER   # reload config (SIGHUP)
journalctl -fu    gs232-bridge@$USER   # live logs
```

## Module layout

```
gs232_bridge/
├── main.py          Entry point — wires modules, handles SIGHUP / SIGTERM
├── gs232_parser.py  Stateless GS-232 command parser
├── serial_port.py   PTY creation, symlink, command dispatch
├── controller.py    Az/El state machine and 100 ms control loop
├── gpio_backend.py  FUSE filesystem I/O and encoder calibration math
├── watchdog.py      Periodic ping, fault declaration, auto-recovery
├── config.ini       All tunables (preserved on upgrade)
├── install.sh       Install / upgrade script
└── gs232-bridge@.service  systemd template unit
```

## GS-232 commands supported

| Command | Action |
|---|---|
| `C` / `C2` | Report current AZ and EL |
| `B` | Report AZ only |
| `A` | Report EL only |
| `W aaa eee` | Go to azimuth aaa, elevation eee |
| `M aaa` | Go to azimuth aaa only |
| `S` | Stop (park target at current position) |

## Calibration notes

All ADC constants are **12-bit** (0–4095), matching the rp2040-gpio-fs
firmware.  The original MicroPython values used `read_u16()` (0–65535);
divide those by 16 to convert.

The elevation encoder uses a law-of-cosines calculation for a linear
actuator — see `_enc2el()` in `gpio_backend.py` for the geometry.

## Watchdog behaviour

The watchdog pings the FUSE filesystem every `ping_interval` seconds.
After `max_failures` consecutive failures it stops all motors and
declares a fault.  Recovery is automatic — when pings succeed again the
fault clears and normal operation resumes without any manual
intervention.
