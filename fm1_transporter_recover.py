#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 MvaveFM1Unbricker contributors
"""
fm1_transporter_recover.py - recover a hard-bricked M-VAVE FM-1 through the
FM-1 Transporter.

A hard-bricked FM-1 (typically after an interrupted firmware update) shows no
USB device at all, so fm1_unbrick.py cannot reach it. The FM-1 Transporter
(https://github.com/kurogedelic/FM-1-transporter) is a Seeed XIAO RP2040 wired
to the FM-1's USB D+, D- and GND. At power-on it keys the JieLi WL82 into its
mask-ROM UBOOT and offers the 1 MiB flash to the computer over a USB serial
line protocol (CDC 1, the "data port"). This script speaks that protocol
itself (pyserial only) and automates the recovery: flash the XIAO, wait for
the FM-1, dump the flash twice, explain what is in it, and rewrite only the
4 KiB application sectors that differ from a package.

Safety rules, enforced in code:
  * only whole 4 KiB sectors in 0x4000..0x92FFF are ever written; the boot
    head 0x0000..0x3FFF is compared and reported, never written;
  * no chip erase, no block erase (the Transporter has neither);
  * the chip key (980F) and flash ID (856014) must match an FM-1 exactly,
    with no override;
  * a fresh full read must equal the reference dump over the whole package
    region 0x0000..0x92FFF, or nothing is written;
  * without --write, restore is a dry run;
  * a final full read must equal the expected image.

For a first-time user: `setup` (fetches JieLi's loader, builds the Transporter
firmware from the prebuilt placeholder in transporter/, installs pyserial), then
`wizard --v15 FM-1.fwsc` (every step, with prompts).

Run `python fm1_transporter_recover.py --help` and see README.md.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import shutil
import struct
import subprocess
import sys
import time
import types
import zlib
from pathlib import Path

import fm1_unbrick as fu

TOOL_VERSION = "1.0.0"

FLASH_SIZE = fu.FLASH_SIZE            # 1 MiB SPI NOR
SECTOR = fu.SECTOR_SIZE               # 4 KiB, the only write unit
HEAD_END = fu.PROTECTED_END           # 0x0000..0x3FFF: never written
APP_START = fu.FW_START               # 0x4000
APP_END = fu.FW_END                   # 0x93000: [0, APP_END) is the package region
APP_SECTORS = (APP_END - APP_START) // SECTOR    # 143
DATA_START = APP_END                  # device data (Felucca objects, loader, records)

EXPECTED_INFO = "OK key=980F type=3 id=856014"
EXPECTED_KEY = "%04X" % fu.EXPECTED_CHIP_KEY
EXPECTED_ID = "%06X" % fu.EXPECTED_FLASH_ID

# Staged update loader and update record (Felucca-family OTA)
LOADER_HDR = 32
LOADER_FLAG = 0x41
LOADER_LOAD_ADDR = 0x01C0A800
LOADER_MARKER = b"FELUCCA-LOADER-1"
LOADER_MARKER_SPAN = (0x48, 0x98)     # relative to the outer header
LOADER_SPAN = 0x4F00                  # loader area = [area, area + 0x4F00); record follows
DEFAULT_LOADER_AREA = 0xE0000
RECORD_LEN = 112
RECORD_CRC_END = 80
RECORD_C1, RECORD_C2, RECORD_MAGIC = 0x5A0D, 0x5A01, 0x5441
RECORD_SCAN = (0x93000, 0xFC000)
FELU = b"FELU"
TORN_TAIL = 256
PAGE = 256                            # SPI NOR program page

# Transporter / XIAO RP2040
RP2_VID = 0x2E8A
TRANSPORTER_PID = 0x000A
UF2_MAGIC0 = b"UF2\n"                 # 0x0A324655 little-endian
UF2_MAGIC1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_BLOCK = 512
UF2_PAYLOAD_MAX = 476
UF2_FLAG_NOT_MAIN_FLASH = 0x00000001
RP2_VOLUME = "RPI-RP2"

# The prebuilt Transporter firmware (kurogedelic/FM-1-transporter a632d92, XIAO RP2040) with a
# placeholder where JieLi's wl82loader.bin goes; `setup` splices the real loader in.
SCRIPT_DIR = Path(__file__).resolve().parent
TRANSPORTER_DIR = SCRIPT_DIR / "transporter"
PLACEHOLDER_UF2 = TRANSPORTER_DIR / "fm1_transporter-a632d92-placeholder.uf2"
PLACEHOLDER_PATTERN = TRANSPORTER_DIR / "placeholder_loader.pattern"
DEFAULT_UF2 = TRANSPORTER_DIR / "fm1_transporter.uf2"
PLACEHOLDER_UF2_SHA256 = "363ec4b98071a40a1eadec14574c936c78bd8f2afdf17cc890104e887771b577"
PLACEHOLDER_PATTERN_SHA256 = "25f62babdb1c62753f7e293338c8ee1b6f323aec9805644b794e6581e7c5bc29"
WL82_LOADER_SHA256 = "d41da6126760c9d66660bcc0cac8d27d221806c5e369a8036921efe68dca5376"
TRANSPORTER_UF2_SHA256 = "f275a52a0bd870c72f523ae28a54ac4d81dda9068a271318aa21dbdf95ef55ea"
assert WL82_LOADER_SHA256 == fu.WL82_LOADER_SHA256
BOOTSEL_WAIT = 15.0
REBOOT_WAIT = 20.0
HINT_EVERY = 5.0
WAIT_HINT = ("the FM-1 has not answered the USB_KEY yet: switch it on now / check D+ D- GND / "
             "try rekey")

EXIT_OK, EXIT_FAIL, EXIT_DANGER = fu.EXIT_OK, fu.EXIT_FAIL, fu.EXIT_DANGER

log = logging.getLogger("fm1_transporter")


class TransporterError(fu.UnbrickError):
    """The Transporter did not answer as expected."""


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logging(command: str, log_dir: Path) -> Path:
    """Console (INFO) + logs/transporter-YYYYMMDD-HHMMSS.log (DEBUG). fm1_unbrick's
    logger (package summaries, the typed confirmation) goes to the same places."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = fu.unique_path(log_dir, "transporter-" + fu.timestamp(), ".log")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    fh = logging.FileHandler(str(path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    for lg in (log, fu.log):
        lg.handlers[:] = []
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        lg.addHandler(console)
        lg.addHandler(fh)
    log.debug("fm1_transporter_recover %s, python %s, platform %s, command %r",
              TOOL_VERSION, sys.version.split()[0], sys.platform, command)
    return path


def close_logging():
    handlers = set(log.handlers) | set(fu.log.handlers)
    for lg in (log, fu.log):
        for h in list(lg.handlers):
            lg.removeHandler(h)
    for h in handlers:
        h.close()


def crc32(data) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def hexlist(addrs):
    return " ".join("0x%05X" % a for a in addrs)


# --------------------------------------------------------------------------
# The Transporter line protocol (CDC 1), see src/transporter_proto.c
#
#   ping                -> PONG                (answered at once, even without the FM-1)
#   status              -> OK uboot=U v15=V loader=L loader_running=R
#   uboot               -> OK uboot | OK softkey | ERR no-v15
#   info                -> OK key=980F type=3 id=856014 | ERR ...
#   read ADDR LEN       -> DATA LEN, LEN raw bytes, END CRC32HEX (zlib crc32, %08X) | END FAIL
#   wsec ADDR CRC32HEX  + 4096 raw bytes -> OK | ERR crc XXXXXXXX | ERR range | ERR write
#   rekey               -> OK rekey            (the Transporter reboots into USB_KEY mode)
# Every request except ping/rekey waits until the FM-1 has ACKed the USB_KEY;
# one request at a time, extra lines are dropped while one is pending.
# --------------------------------------------------------------------------

class Transporter:
    """Client for the data port. `s` is a pyserial Serial or a MockTransporter."""

    def __init__(self, s, port, mock=False):
        self.s, self.port, self.mock = s, port, mock

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def _readline(self, timeout):
        self.s.timeout = timeout
        return self.s.readline().decode("ascii", "replace").strip()

    def send(self, line: str, payload: bytes = b""):
        self.s.reset_input_buffer()
        self.s.write(line.encode("ascii") + b"\n" + payload)
        log.debug("> %s%s", line, " + %d bytes" % len(payload) if payload else "")

    def request(self, line: str, timeout=30.0) -> str:
        self.send(line)
        reply = self._readline(timeout)
        log.debug("< %s", reply)
        if not reply:
            raise TransporterError("no reply to %r from the Transporter (%s)" % (line, self.port))
        return reply

    def ping(self) -> bool:
        try:
            self.send("ping")
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if self._readline(0.3) == "PONG":
                    return True
        except Exception as e:      # a vanished port
            log.debug("ping failed: %s", e)
        return False

    def status(self, timeout=30.0) -> dict:
        reply = self.request("status", timeout)
        return parse_status(reply)

    def info(self) -> str:
        return self.request("info", 30.0)

    def read_flash(self, addr: int, length: int, label="Reading") -> bytes:
        if addr < 0 or length <= 0 or addr + length > FLASH_SIZE:
            raise fu.SafetyError("read 0x%X+0x%X is outside the flash" % (addr, length))
        reply = self.request("read %#x %d" % (addr, length), 30.0)
        if reply != "DATA %d" % length:
            raise TransporterError("read 0x%X+0x%X: %s" % (addr, length, reply))
        data = bytearray()
        prog = fu.Progress(label, length)
        self.s.timeout = 10
        try:
            while len(data) < length:
                chunk = self.s.read(min(65536, length - len(data)))
                if not chunk:
                    raise TransporterError("read stalled at 0x%X of 0x%X bytes" % (len(data), length))
                data += chunk
                prog.update(len(chunk))
        finally:
            prog.close()
        end = self._readline(10)
        log.debug("< %s", end)
        want = "END %08X" % crc32(data)
        if end != want:
            raise TransporterError("read 0x%X+0x%X: transfer check failed (%s, local %s)"
                                   % (addr, length, end or "no END line", want))
        return bytes(data)

    def write_sector(self, addr: int, sec: bytes) -> str:
        """wsec: the only write. The gate below mirrors the Transporter firmware."""
        if len(sec) != SECTOR or addr % SECTOR or addr < APP_START or addr >= APP_END:
            raise fu.SafetyError("refusing to write 0x%X+0x%X: only whole 4 KiB sectors in "
                                 "0x%X..0x%X are ever written" % (addr, len(sec), APP_START,
                                                                  APP_END - 1))
        self.send("wsec %#x %08X" % (addr, crc32(sec)), sec)
        reply = self._readline(20)
        log.debug("< %s", reply)
        return reply

    def rekey(self) -> str:
        return self.request("rekey", 5.0)


def parse_status(reply: str) -> dict:
    if not reply.startswith("OK "):
        raise TransporterError("status: %s" % reply)
    try:
        return dict(kv.split("=", 1) for kv in reply.split()[1:])
    except ValueError:
        raise TransporterError("status: cannot parse %r" % reply)


def parse_info(reply: str) -> dict:
    if not reply.startswith("OK "):
        raise TransporterError("info: %s" % reply)
    return dict(kv.split("=", 1) for kv in reply.split()[1:] if "=" in kv)


# --------------------------------------------------------------------------
# Mock Transporter: same line protocol over an in-memory 1 MiB NOR
# --------------------------------------------------------------------------

class MockTransporter:
    """Stands in for a Transporter's data port (a pyserial-like object).

    The flash is a bytearray with NOR semantics: `wsec` erases the 4 KiB sector
    to 0xFF, then programs it (bits can only be cleared). Like the firmware it
    refuses sectors outside [0x4000, 0x93000). Fault injection: present=False
    (the FM-1 never ACKs the USB_KEY: only ping answers), chip_key / flash_id,
    flaky_read=N (the N-th read returns one changed byte, consistently with its
    CRC, like an unstable flash read), wsec_errors={addr: "ERR write"}.
    """

    port = "mock://fm1-transporter"

    def __init__(self, flash=None, chip_key=fu.EXPECTED_CHIP_KEY, flash_id=fu.EXPECTED_FLASH_ID,
                 present=True, uboot=True, flaky_read=None, wsec_errors=None):
        if flash is None:
            flash = fu.synthetic_flash()
        if len(flash) != FLASH_SIZE:
            raise ValueError("mock flash must be exactly 0x%X bytes" % FLASH_SIZE)
        self.flash = bytearray(flash)
        self.key, self.id = chip_key, flash_id
        self.present, self.uboot = present, uboot
        self.flaky_read = flaky_read
        self.wsec_errors = dict(wsec_errors or {})
        self.timeout = 1.0
        self.reads = 0
        self.writes = []            # addresses programmed by wsec
        self.requests = []
        self._in = bytearray()      # host -> mock
        self._out = bytearray()     # mock -> host
        self._pending = None        # request waiting for the FM-1
        self._binary = None         # (line, expected payload length)
        self.closed = False

    # pyserial surface
    def write(self, data):
        self._in += data
        self._process()
        return len(data)

    def reset_input_buffer(self):
        self._out.clear()

    @property
    def in_waiting(self):
        return len(self._out)

    def read(self, n=1):
        if not self._out:
            time.sleep(0.005)
            return b""
        chunk = bytes(self._out[:n])
        del self._out[:n]
        return chunk

    def readline(self):
        i = self._out.find(b"\n")
        if i < 0:
            time.sleep(0.005)       # stands for the serial timeout
            return b""
        line = bytes(self._out[:i + 1])
        del self._out[:i + 1]
        return line

    def close(self):
        self.closed = True

    # firmware behaviour
    def _emit(self, text):
        self._out += text.encode("ascii") + b"\n"

    def _process(self):
        while True:
            if self._binary:
                line, n = self._binary
                if len(self._in) < n:
                    return
                payload = bytes(self._in[:n])
                del self._in[:n]
                self._binary = None
                self._request(line, payload)
                continue
            i = self._in.find(b"\n")
            if i < 0:
                return
            line = self._in[:i].decode("ascii", "replace").strip("\r")
            del self._in[:i + 1]
            if line == "ping":
                self._emit("PONG")
            elif line == "rekey":
                self._emit("OK rekey")
            elif line.startswith("wsec "):
                self._binary = (line, SECTOR)
            else:
                self._request(line, b"")

    def _request(self, line, payload):
        self.requests.append(line)
        if not self.present:
            # core 1 is still keying; the request waits (and later ones are dropped)
            if self._pending is None:
                self._pending = line
            return
        argv = line.split()
        if not argv:
            return
        cmd = argv[0]
        if cmd == "status":
            self._emit("OK uboot=%d v15=0 loader=1 loader_running=0" % int(self.uboot))
        elif not self.uboot:
            self._emit("ERR no-uboot")
        elif cmd == "info":
            self._emit("OK key=%04X type=3 id=%06X" % (self.key, self.id))
        elif cmd == "read" and len(argv) == 3:
            self._read(int(argv[1], 0), int(argv[2], 0))
        elif cmd == "wsec" and len(argv) == 3:
            self._wsec(int(argv[1], 0), int(argv[2], 16), payload)
        else:
            self._emit("ERR unknown")

    def _read(self, addr, n):
        if n == 0 or addr >= FLASH_SIZE or n > FLASH_SIZE - addr:
            self._emit("ERR range")
            return
        self.reads += 1
        data = bytearray(self.flash[addr:addr + n])
        if self.flaky_read is not None and self.reads == self.flaky_read:
            data[n // 2] ^= 0x5A
        self._emit("DATA %d" % n)
        self._out += data
        self._emit("END %08X" % crc32(data))

    def _wsec(self, addr, crc, sec):
        got = crc32(sec)
        if got != crc:
            self._emit("ERR crc %08X" % got)
            return
        if addr < APP_START or addr >= APP_END or addr % SECTOR:
            self._emit("ERR range")
            return
        self.flash[addr:addr + SECTOR] = b"\xff" * SECTOR      # erase
        if addr in self.wsec_errors:
            self._emit(self.wsec_errors[addr])
            return
        for i, b in enumerate(sec):                              # program
            self.flash[addr + i] &= b
        self.writes.append(addr)
        self._emit("OK")

    def lowest_written(self):
        return min(self.writes, default=None)


# --------------------------------------------------------------------------
# Finding and opening the data port
# --------------------------------------------------------------------------

def _serial():
    try:
        import serial
        import serial.tools.list_ports  # noqa: F401
    except ImportError:
        raise fu.UnbrickError("pyserial is not installed: run `python -m pip install pyserial`")
    return serial


def rp2_ports():
    """Serial ports of RP2040 boards (VID 0x2E8A), as pyserial ListPortInfo."""
    serial = _serial()
    return [p for p in serial.tools.list_ports.comports() if p.vid == RP2_VID]


def candidate_ports():
    found = []
    try:
        found += [p.device for p in rp2_ports()]
    except fu.UnbrickError:
        raise
    except Exception as e:
        log.debug("list_ports failed: %s", e)
    if sys.platform == "darwin":
        found += sorted(glob.glob("/dev/cu.usbmodem*"))
    elif sys.platform.startswith("linux"):
        found += sorted(glob.glob("/dev/ttyACM*"))
    seen, out = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def open_port(path):
    serial = _serial()
    try:
        s = serial.Serial(path, 115200, timeout=0.3)
    except (serial.SerialException, OSError) as e:
        log.debug("cannot open %s: %s", path, e)
        return None
    return Transporter(s, path)


def probe_port(explicit=None):
    """The first port that answers ping with PONG (like fm1t.find_port), or None."""
    for path in ([explicit] if explicit else candidate_ports()):
        t = open_port(path)
        if t is None:
            continue
        if t.ping():
            log.debug("data port %s answers PONG", path)
            t.s.timeout = 5
            return t
        t.close()
    return None


def connect(args):
    """The data port (or the mock with --dry-run), waiting up to --wait seconds."""
    if args.dry_run:
        flash = None
        if args.mock_flash:
            flash = Path(args.mock_flash).read_bytes()
            if len(flash) != FLASH_SIZE:
                raise fu.UnbrickError("--mock-flash must be a 0x%X-byte dump" % FLASH_SIZE)
        m = MockTransporter(flash=flash,
                            chip_key=args.mock_chip_key if args.mock_chip_key is not None
                            else fu.EXPECTED_CHIP_KEY,
                            present=not args.mock_absent)
        log.info("DRY RUN: using a MOCK Transporter%s (no serial port is opened, nothing is "
                 "written to hardware)", " seeded from %s" % args.mock_flash
                 if args.mock_flash else "")
        return Transporter(m, m.port, mock=True)
    deadline = time.monotonic() + max(args.wait, 0)
    next_hint = time.monotonic() + HINT_EVERY
    while True:
        t = probe_port(args.port)
        if t is not None:
            log.info("Transporter data port %s", t.port)
            return t
        if time.monotonic() >= deadline:
            raise fu.UnbrickError(
                "no FM-1 Transporter data port answers ping%s. Check that the XIAO is plugged "
                "in and runs the Transporter firmware (`flash-xiao`)."
                % (" on %s" % args.port if args.port else ""))
        if time.monotonic() >= next_hint:
            log.info("  waiting for the Transporter data port ...")
            next_hint += HINT_EVERY
        time.sleep(0.5)


def wait_status(t: Transporter, wait: float) -> dict:
    """Send one `status` and wait up to `wait` s for its answer: it only comes
    once the FM-1 has ACKed the USB_KEY (extra requests would be dropped)."""
    t.send("status")
    start = time.monotonic()
    deadline = start + max(wait, 0)
    next_hint = start + HINT_EVERY
    log.info("Waiting up to %d s for the FM-1 (status) ...", wait)
    while True:
        reply = t._readline(0.5)
        if reply:
            log.debug("< %s", reply)
            if reply.startswith("OK uboot="):
                return parse_status(reply)
            if reply.startswith("ERR"):
                raise TransporterError("status: %s" % reply)
            continue                # e.g. a stray PONG
        now = time.monotonic()
        if now >= deadline:
            raise fu.UnbrickError("no answer to status after %d s: %s" % (wait, WAIT_HINT))
        if now >= next_hint:
            log.info("  %d s: %s", int(now - start), WAIT_HINT)
            next_hint += HINT_EVERY


def ensure_uboot(t: Transporter, st: dict, timeout=20.0) -> dict:
    """UBOOT is required for info/read/wsec; on stock V15 use the soft key."""
    if st.get("uboot") == "1":
        return st
    if st.get("v15") != "1":
        raise fu.UnbrickError("the Transporter reports no UBOOT and no stock V15. Power-cycle "
                              "the FM-1; if it has no working firmware run `rekey`, then switch "
                              "the FM-1 off and on.")
    reply = t.request("uboot")
    log.info("Soft key: %s", reply)
    if not reply.startswith("OK"):
        raise TransporterError("uboot: %s" % reply)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        st = t.status()
        if st.get("uboot") == "1":
            log.info("FM-1 is in UBOOT")
            return st
    raise fu.UnbrickError("soft key sent but no UBOOT appeared")


def check_info(t: Transporter) -> str:
    """Chip key 980F, type 3, flash 856014, exactly; no override."""
    reply = t.info()
    log.info("Info        %s", reply)
    fields = parse_info(reply)
    problems = []
    if fields.get("key") != EXPECTED_KEY:
        problems.append("chip key %s is not %s" % (fields.get("key"), EXPECTED_KEY))
    if fields.get("id") != EXPECTED_ID:
        problems.append("flash id %s is not %s" % (fields.get("id"), EXPECTED_ID))
    if not problems and reply != EXPECTED_INFO:
        problems.append("reply is not %r" % EXPECTED_INFO)
    if problems:
        raise fu.SafetyError("this does not look like an FM-1: %s. Nothing was written."
                             % "; ".join(problems))
    log.info("Chip key %s and flash %s (1 MiB): this is an FM-1 in UBOOT", EXPECTED_KEY,
             EXPECTED_ID)
    return reply


def ready(t: Transporter, args) -> dict:
    st = wait_status(t, args.wait)
    log.info("Status      %s", " ".join("%s=%s" % kv for kv in st.items()))
    return ensure_uboot(t, st)


# --------------------------------------------------------------------------
# Packages and labels
# --------------------------------------------------------------------------

def load_labelled(v15_path=None, package_paths=(), extra=None):
    """[(label, Package)] for the analysis: --v15 is "V15", others by file stem."""
    out, seen = [], set()

    def add(label, pkg):
        key = str(pkg.path.resolve())
        if key in seen:
            return
        seen.add(key)
        names = [lb for lb, _ in out]
        base, n = label, 2
        while label in names:
            label = "%s-%d" % (base, n)
            n += 1
        out.append((label, pkg))

    v15 = None
    if v15_path:
        v15 = fu.Package.load(v15_path)
        add("V15", v15)
    for p in package_paths or ():
        add(Path(p).stem, fu.Package.load(p))
    if extra is not None:
        if v15 is None and extra.kind == "official-v15":
            v15 = extra
            add("V15", extra)
        else:
            add(extra.path.stem, extra)
    return out, v15


def package_region(pkg) -> bytes:
    """The package's flash image over [0, 0x93000): raw flash from address 0."""
    return pkg.flash[:APP_END]


# --------------------------------------------------------------------------
# Analysis of a full dump
# --------------------------------------------------------------------------

def _u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def _u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def _cstr(b):
    return b.split(b"\0", 1)[0].decode("ascii", "replace")


def _erased(b):
    return b.count(0xFF) == len(b)


def classify_sector(data, addr, labelled):
    sec = data[addr:addr + SECTOR]
    labels = [lb for lb, pkg in labelled if pkg.flash[addr:addr + SECTOR] == sec]
    if labels:
        return " + ".join(labels)
    if _erased(sec):
        return "ERASED"
    tail = ff_tail(sec)
    if tail >= TORN_TAIL:
        # Programming stops on a page boundary; image padding (e.g. the last app
        # sector, whose data ends at +0x52A or +0xDD4) does not.
        return "TORN" if (SECTOR - tail) % PAGE == 0 else "other (0xFF padding)"
    return "other"


def ff_tail(sec):
    return len(sec) - len(sec.rstrip(b"\xff"))


def runs_of(items):
    """[(start, end_exclusive, value)] of consecutive sectors with the same value."""
    runs = []
    for addr, value in items:
        if runs and runs[-1][2] == value and runs[-1][1] == addr:
            runs[-1][1] = addr + SECTOR
        else:
            runs.append([addr, addr + SECTOR, value])
    return [tuple(r) for r in runs]


def check_loader(data, area):
    """The staged update loader at `area` (32-byte outer header + body)."""
    r = {"area": area, "problems": []}
    h = data[area:area + LOADER_HDR]
    r["crc_ok"] = fu._crc16(h[2:LOADER_HDR]) == _u16(h, 0)
    r["dcrc"] = _u16(h, 2)
    r["hdr_area"] = _u32(h, 4)
    r["length"] = _u32(h, 8)
    r["flag"] = h[12]
    r["name"] = _cstr(h[16:32])
    body = area + LOADER_HDR
    end = body + r["length"]
    r["end"] = end
    r["within"] = end <= area + LOADER_SPAN
    r["load_addr"] = _u32(data, body + 8) if body + 12 <= FLASH_SIZE else None
    lo, hi = LOADER_MARKER_SPAN
    r["marker"] = LOADER_MARKER in data[area + lo:area + hi]
    r["trailing_erased"] = r["within"] and _erased(data[end:area + LOADER_SPAN])
    if not r["crc_ok"]:
        r["problems"].append("outer header CRC fails")
    if r["hdr_area"] != area:
        r["problems"].append("header area 0x%X != 0x%X" % (r["hdr_area"], area))
    if r["flag"] != LOADER_FLAG:
        r["problems"].append("flag 0x%02X != 0x41" % r["flag"])
    if not r["within"]:
        r["problems"].append("body runs past 0x%05X" % (area + LOADER_SPAN - 1))
    if r["load_addr"] != LOADER_LOAD_ADDR:
        r["problems"].append("inner load address %s != 0x%08X"
                             % ("0x%08X" % r["load_addr"] if r["load_addr"] is not None
                                else "none", LOADER_LOAD_ADDR))
    if not r["marker"]:
        r["problems"].append("marker %s missing" % LOADER_MARKER.decode())
    if r["within"] and not r["trailing_erased"]:
        r["problems"].append("bytes after the body are not erased")
    r["ok"] = not r["problems"]
    return r


def find_records(data):
    out = []
    for p in range(RECORD_SCAN[0] + SECTOR - 256, RECORD_SCAN[1], SECTOR):
        rec = data[p:p + RECORD_LEN]
        if _u16(rec, 6) != RECORD_MAGIC:
            continue
        crc_ok = fu._crc16(rec[2:RECORD_CRC_END]) == _u16(rec, 0)
        consts = _u16(rec, 2) == RECORD_C1 and _u16(rec, 4) == RECORD_C2
        out.append({"addr": p, "crc_ok": crc_ok, "consts_ok": consts,
                    "valid": crc_ok and consts, "identity": _cstr(rec[8:72]),
                    "area": _u32(rec, 72)})
    return out


def find_loader_headers(data):
    out = []
    for a in range(DATA_START, FLASH_SIZE, SECTOR):
        h = data[a:a + LOADER_HDR]
        if h[12] == LOADER_FLAG and h[16:22] == b"LOADER":
            out.append((a, fu._crc16(h[2:LOADER_HDR]) == _u16(h, 0), _u32(h, 8), _cstr(h[16:32])))
    return out


class Analysis:
    def __init__(self):
        self.lines = []
        self.runs = []
        self.torn = []
        self.records = []
        self.loaders = []
        self.head_equals_v15 = None
        self.verdicts = []
        self.mixed = False

    def add(self, line=""):
        self.lines.append(line)

    @property
    def text(self):
        return "\n".join(self.lines) + "\n"


def analyse_dump(data: bytes, labelled, v15=None, name="dump") -> Analysis:
    if len(data) != FLASH_SIZE:
        raise fu.UnbrickError("%s is 0x%X bytes, a full dump is 0x%X" % (name, len(data),
                                                                          FLASH_SIZE))
    a = Analysis()
    a.add("FM-1 flash analysis (fm1_transporter_recover %s)" % TOOL_VERSION)
    a.add("  dump     %s" % name)
    a.add("  sha256   %s   crc32 %08X" % (fu.sha256(data), crc32(data)))
    for label, pkg in labelled:
        a.add("  package  %-10s %s  %s = %s" % (label, pkg.path.name, pkg.identity or "(none)",
                                                pkg.description))
    if v15 is not None and v15.kind != "official-v15":
        a.add("  NOTE: the --v15 file is NOT the hash-verified official V15 (%s)" % v15.kind)

    # (a) head
    a.add("")
    a.add("(a) Head 0x00000..0x03FFF (SPL; never written)")
    head = data[:HEAD_END]
    a.add("  sha256 %s" % fu.sha256(head))
    if v15 is not None:
        a.head_equals_v15 = head == v15.head
        if a.head_equals_v15:
            a.add("  head == V15")
        else:
            bad = [x for x in range(0, HEAD_END, SECTOR) if head[x:x + SECTOR] != v15.head[x:x + SECTOR]]
            a.add("  head DIFFERS from V15 in sectors %s (this tool never writes the head)"
                  % hexlist(bad))
    else:
        a.add("  (no --v15 given: not compared)")
    for label, pkg in labelled:
        if pkg is not v15:
            a.add("  head %s %s's head" % ("==" if head == pkg.head else "differs from", label))

    # (b) application
    a.add("")
    a.add("(b) Application 0x04000..0x92FFF (%d sectors of 4 KiB)" % APP_SECTORS)
    items = [(x, classify_sector(data, x, labelled)) for x in range(APP_START, APP_END, SECTOR)]
    a.runs = runs_of(items)
    for start, end, value in a.runs:
        n = (end - start) // SECTOR
        a.add("  0x%05X..0x%05X  %3d sector%s  %s" % (start, end - 1, n, "" if n == 1 else "s",
                                                       value))
    a.torn = [x for x, v in items if v == "TORN"]
    erased = [x for x, v in items if v == "ERASED"]
    a.add("  " + ("no torn sector" if not a.torn else
                  "TORN sectors (programmed up to a page boundary, then >= %d bytes of 0xFF): %s"
                  % (TORN_TAIL, hexlist(a.torn))))
    for x, v in items:
        if v.startswith("other (0xFF padding"):
            a.add("  0x%05X ends in 0xFF from +0x%03X (not a page boundary: image padding, not "
                  "counted as torn)" % (x, SECTOR - ff_tail(data[x:x + SECTOR])))
    a.add("  erased sectors: %d" % len(erased))
    best = None
    for label, pkg in labelled:
        same = [x for x in range(APP_START, APP_END, SECTOR)
                if pkg.flash[x:x + SECTOR] == data[x:x + SECTOR]]
        last = "0x%05X" % same[-1] if same else "none"
        a.add("  %-10s %3d/%d sectors equal, last sector equal to %s: %s"
              % (label, len(same), APP_SECTORS, label, last))
        if best is None or len(same) > best[1]:
            best = (label, len(same))
    exact = [lb for lb, pkg in labelled if pkg.flash[APP_START:APP_END] == data[APP_START:APP_END]]
    distinct = {v for _, v in items if v != "ERASED"}
    if exact:
        a.add("  app == %s exactly" % " == ".join(exact))
    elif best and best[1] > 0 and len(distinct) > 1:
        a.mixed = True
    elif not labelled:
        a.add("  (no --package given: sectors not attributed)")

    # (c) loader area, (d) records
    a.records = find_records(data)
    areas = [r["area"] for r in a.records if r["valid"]] or [DEFAULT_LOADER_AREA]
    a.add("")
    a.add("(c) Staged update loader")
    for area in dict.fromkeys(areas):
        if not (DATA_START <= area <= FLASH_SIZE - LOADER_SPAN - 256):
            a.add("  area 0x%X is outside the data area" % area)
            continue
        r = check_loader(data, area)
        a.loaders.append(r)
        a.add("  area 0x%05X..0x%05X" % (area, area + LOADER_SPAN - 1))
        a.add("    outer header CRC   %s" % ("valid" if r["crc_ok"] else "INVALID"))
        a.add("    length 0x%X (%d), name %r, flag 0x%02X, dcrc 0x%04X"
              % (r["length"], r["length"], r["name"], r["flag"], r["dcrc"]))
        a.add("    body 0x%05X..0x%05X %s" % (area + LOADER_HDR, r["end"] - 1,
                                              "within the area" if r["within"]
                                              else "RUNS PAST the area"))
        a.add("    inner load address %s" % ("0x%08X" % r["load_addr"]
                                             if r["load_addr"] is not None else "none"))
        a.add("    marker %s %s" % (LOADER_MARKER.decode(), "present" if r["marker"] else "MISSING"))
        a.add("    trailing bytes     %s" % ("erased" if r["trailing_erased"] else "NOT erased"))
        a.add("    => loader %s" % ("intact" if r["ok"] else "DAMAGED: " + "; ".join(r["problems"])))
    others = [h for h in find_loader_headers(data) if h[0] not in areas]
    for addr, ok, ln, nm in others:
        a.add("  another loader header at 0x%05X: %r, length 0x%X, CRC %s (not referenced by a "
              "valid record)" % (addr, nm, ln, "valid" if ok else "INVALID"))

    a.add("")
    a.add("(d) Update records (4K - 256 in 0x%05X..0x%05X, magic 0x5441)" % RECORD_SCAN)
    if not a.records:
        a.add("  none")
    for r in a.records:
        a.add("  0x%05X  CRC %s%s, identity %r, area 0x%05X%s"
              % (r["addr"], "valid" if r["crc_ok"] else "INVALID",
                 "" if r["consts_ok"] else " (constants 0x5A0D/0x5A01 wrong)",
                 r["identity"], r["area"], "  => valid" if r["valid"] else "  => not valid"))

    # (e) data areas
    a.add("")
    a.add("(e) Device data 0x93000..0xFFFFF")
    for x in range(DATA_START, FLASH_SIZE, SECTOR):
        if data[x:x + 4] == FELU:
            a.add("  FELU object at 0x%05X: type 0x%X, seq %d, len 0x%X"
                  % (x, _u32(data, x + 4), _u32(data, x + 8), _u32(data, x + 12)))
    er = runs_of([(x, True) for x in range(DATA_START, FLASH_SIZE, SECTOR)
                  if _erased(data[x:x + SECTOR])])
    for start, end, _ in er:
        a.add("  erased 0x%05X..0x%05X (%d sector%s)" % (start, end - 1, (end - start) // SECTOR, "" if end - start == SECTOR else "s"))

    # (f) verdict
    a.add("")
    a.add("(f) Verdict")
    valid = [r for r in a.records if r["valid"]]
    if not valid:
        a.verdicts.append("no record")
        bad = [r for r in a.records if not r["valid"]]
        a.add("  no record" + (" (%d record(s) with an invalid CRC)" % len(bad) if bad else ""))
    else:
        lmap = {r["area"]: r for r in a.loaders}
        for r in valid:
            lr = lmap.get(r["area"])
            if lr is not None and lr["ok"]:
                a.verdicts.append("resume state intact (loader + record valid)")
                a.add("  resume state intact (loader + record valid): record 0x%05X %r -> loader "
                      "0x%05X. If the unit still showed no USB device at power-on, the chip did "
                      "not act on this record (the SPL does not honour the flash record on a "
                      "cold boot, as seen 2026-10-09): the app must be rewritten." %
                      (r["addr"], r["identity"], r["area"]))
            else:
                a.verdicts.append("loader damaged")
                a.add("  loader damaged: record 0x%05X points at 0x%05X, %s"
                      % (r["addr"], r["area"], "; ".join(lr["problems"]) if lr else "no loader"))
    if a.head_equals_v15 is not None:
        a.add("  head %s V15" % ("==" if a.head_equals_v15 else "DIFFERS from"))
    if exact:
        a.add("  app is a single source: %s" % " == ".join(exact))
    elif a.mixed:
        parts = ", ".join("0x%05X..0x%05X %s" % (s, e - 1, v) for s, e, v in a.runs)
        a.add("  app is a MIX of two (or more) sources: %s" % parts)
    a.add("  %s" % ("no torn sector" if not a.torn else "%d TORN sector(s): %s"
                                                        % (len(a.torn), hexlist(a.torn))))
    return a


def emit_report(text):
    for line in text.rstrip("\n").split("\n"):
        log.info(line)


# --------------------------------------------------------------------------
# Dump
# --------------------------------------------------------------------------

def double_read(t: Transporter):
    log.info("Reading the whole flash (0x0..0x%X), twice", FLASH_SIZE - 1)
    a = t.read_flash(0, FLASH_SIZE, "Dump read 1/2")
    log.info("  read 1 sha256 %s", fu.sha256(a))
    b = t.read_flash(0, FLASH_SIZE, "Dump read 2/2")
    log.info("  read 2 sha256 %s", fu.sha256(b))
    if a != b:
        diffs = [x for x in range(0, FLASH_SIZE, SECTOR) if a[x:x + SECTOR] != b[x:x + SECTOR]]
        raise fu.UnbrickError("the two reads DIFFER (sectors %s). Nothing was saved or written; "
                              "check the three wires and try again." % hexlist(diffs[:16]))
    return a


def save_dump(data, outdir: Path, mock: bool) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    stem = "fm1-transporter-dump-" + fu.timestamp() + ("-dryrun" if mock else "")
    path = fu.unique_path(outdir, stem, ".bin")
    with open(str(path), "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if path.read_bytes() != data:
        raise fu.UnbrickError("the dump file %s does not read back correctly" % path)
    log.info("Dump OK     %s", path)
    log.info("  sha256 %s  crc32 %08X (both reads identical)", fu.sha256(data), crc32(data))
    return path


def dump_and_analyse(t, outdir, labelled, v15):
    data = double_read(t)
    path = save_dump(data, Path(outdir), t.mock)
    rep = analyse_dump(data, labelled, v15, name=str(path))
    txt = path.with_suffix(".txt")
    txt.write_text(rep.text, encoding="utf-8")
    emit_report(rep.text)
    log.info("Analysis saved to %s", txt)
    return path, data, rep


# --------------------------------------------------------------------------
# Restore (fm1t.py `write` policy)
# --------------------------------------------------------------------------

DONE_TEXT = """
Done: the application area is written and verified. Now:
  1. Switch the FM-1 off.
  2. Unplug the three Transporter wires (D+, D-, GND) from the FM-1.
  3. Switch the FM-1 on. Stock V15 shows a live screen.
"""

DANGER_TEXT = """
!!! The write could not be completed or verified. !!!
  * Do NOT switch the FM-1 off and do NOT unplug the Transporter: it stays in UBOOT.
  * The head 0x0000-0x3FFF was not touched.
  * Re-run `recover PACKAGE` (it dumps again and writes only the sectors that still
    differ), or `dump DIR` and then `restore PACKAGE --ref <new dump> --write`.
  * Your reference dump: %s
"""


class Plan:
    def __init__(self, pkg, ref, ref_path):
        self.pkg, self.ref, self.ref_path = pkg, ref, ref_path
        self.img = package_region(pkg)
        if len(ref) != FLASH_SIZE:
            raise fu.UnbrickError("--ref must be a full 0x%X-byte dump (%s is 0x%X)"
                                  % (FLASH_SIZE, ref_path, len(ref)))
        if len(self.img) != APP_END or len(self.img) % SECTOR:
            raise fu.UnbrickError("the package flash image does not cover 0x0..0x%X" % (APP_END - 1))
        self.diff = [x for x in range(0, APP_END, SECTOR)
                     if self.img[x:x + SECTOR] != ref[x:x + SECTOR]]
        self.head_diff = [x for x in self.diff if x < HEAD_END]
        self.write = [x for x in self.diff if APP_START <= x < APP_END]

    def report(self):
        pkg = self.pkg
        log.info("Package flash image 0x0..0x%X sha256 %s...; %d sector%s differ from --ref%s",
                 APP_END - 1, fu.sha256(self.img)[:16], len(self.diff),
                 "" if len(self.diff) == 1 else "s",
                 ": " + hexlist(self.diff) if self.diff else "")
        if pkg.fl_len > APP_END:
            log.info("  (the package's flash image continues past 0x%X; that part is ignored)",
                     APP_END - 1)
        if self.head_diff:
            log.warning("NOTE: the package's head differs from the device's in sectors %s. The "
                        "head 0x0000..0x3FFF is NEVER written; only the application sectors are.",
                        hexlist(self.head_diff))
        else:
            log.info("Head 0x0000..0x3FFF: package == --ref")
        log.info("Sectors to write in 0x%X..0x%X: %d", APP_START, APP_END - 1, len(self.write))


def load_restore_package(path, verify_v15):
    pkg = fu.Package.load(path)
    pkg.log_summary()
    pkg.check_writable(False)
    if verify_v15 and pkg.kind != "official-v15":
        raise fu.SafetyError("--verify-v15 given but %s is not the verified official V15"
                             % pkg.path.name)
    if verify_v15:
        log.info("V15 hash verified.")
    return pkg


def restore_session(t: Transporter, plan: Plan, args, write: bool, confirm=None):
    """In one Transporter session: identity, fresh read == --ref, then (write) the
    differing sectors and the final verify. Returns an exit code. `confirm`
    (args, lines, phrase) replaces fu.confirm (the wizard asks through its own input)."""
    check_info(t)
    log.info("Fresh full read ...")
    now = t.read_flash(0, FLASH_SIZE, "Fresh read")
    bad = [x for x in range(0, APP_END, SECTOR) if now[x:x + SECTOR] != plan.ref[x:x + SECTOR]]
    if bad:
        raise fu.SafetyError("the flash differs from --ref in package-region sectors %s; not "
                             "writing. Take a new dump and use it as --ref." % hexlist(bad))
    extra = [x for x in range(APP_END, FLASH_SIZE, SECTOR)
             if now[x:x + SECTOR] != plan.ref[x:x + SECTOR]]
    log.info("Package region 0x0..0x%X equals --ref; device-data sectors changed since --ref: %s",
             APP_END - 1, hexlist(extra) if extra else "none")
    if not write:
        log.info("DRY RUN: nothing written (add --write to write %d sector%s)",
                 len(plan.write), "" if len(plan.write) == 1 else "s")
        return EXIT_OK
    if not plan.write:
        log.info("Nothing to write: the application area already holds this package.")
        log.info(DONE_TEXT)
        return EXIT_OK

    (confirm or fu.confirm)(args, [
        "",
        "About to WRITE the FM-1 flash through the Transporter:",
        "  port      %s%s" % (t.port, "  [MOCK]" if t.mock else ""),
        "  package   %s" % plan.pkg.path.name,
        "  identity  %s = %s" % (plan.pkg.identity or "(none)", plan.pkg.description),
        "  sectors   %d of 4 KiB in 0x%X..0x%X (head 0x0000-0x3FFF never written)"
        % (len(plan.write), APP_START, APP_END - 1),
        "  --ref     %s" % plan.ref_path,
        "",
    ], "WRITE")

    expect = now[:HEAD_END] + plan.img[HEAD_END:APP_END] + now[APP_END:]
    log.info("Writing %d sectors. Do not switch anything off now.", len(plan.write))
    prog = fu.Progress("Writing", len(plan.write))
    try:
        for x in plan.write:
            reply = t.write_sector(x, plan.img[x:x + SECTOR])
            log.debug("sector 0x%05X: %s", x, reply or "no reply")
            if reply != "OK":
                prog.close()
                log.error("Sector 0x%05X: %s", x, reply or "no reply")
                log.error(DANGER_TEXT % plan.ref_path)
                return EXIT_DANGER
            prog.update(1)
        prog.close()
        log.info("%d sectors written", len(plan.write))
        log.info("Final full read ...")
        final = t.read_flash(0, FLASH_SIZE, "Final read")
    except KeyboardInterrupt:
        log.error("Interrupted during the write.")
        log.error(DANGER_TEXT % plan.ref_path)
        return EXIT_DANGER
    except Exception as e:
        log.error("Write failed: %s", e)
        log.error(DANGER_TEXT % plan.ref_path)
        return EXIT_DANGER
    if final != expect:
        bad = [x for x in range(0, FLASH_SIZE, SECTOR) if final[x:x + SECTOR] != expect[x:x + SECTOR]]
        log.error("Final read DIFFERS from the expected image in sectors %s", hexlist(bad))
        log.error(DANGER_TEXT % plan.ref_path)
        return EXIT_DANGER
    log.info("Final full read EQUALS the expected image (sha256 %s)", fu.sha256(final))
    log.info(DONE_TEXT)
    return EXIT_OK


# --------------------------------------------------------------------------
# flash-xiao
# --------------------------------------------------------------------------

def check_uf2_bytes(data: bytes, name="the UF2 file") -> bytes:
    """Cheap whole-file check: a multiple of 512 bytes, the first block's three magics."""
    if (len(data) < UF2_BLOCK or len(data) % UF2_BLOCK or data[:4] != UF2_MAGIC0
            or _u32(data, 4) != UF2_MAGIC1 or _u32(data, 508) != UF2_MAGIC_END):
        raise fu.SafetyError("%s does not carry the UF2 magic (UF2\\n, 0x9E5D5157, 0x0AB16F30); "
                             "refusing" % name)
    return data


def check_uf2(path: Path) -> bytes:
    if path.suffix.lower() != ".uf2":
        raise fu.SafetyError("%s is not a .uf2 file; refusing" % path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise fu.UnbrickError("cannot read %s: %s" % (path, e))
    return check_uf2_bytes(data, str(path))


def parse_uf2(data: bytes, name="the UF2 file"):
    """Strictly parse a flash UF2: every block's magics, block numbers 0..n-1 of n,
    payload sizes 1..476, main-flash blocks only, consecutive target addresses.
    Returns [(block, payload_size)]; the block is the original 512 bytes."""
    check_uf2_bytes(data, name)
    n = len(data) // UF2_BLOCK
    out, next_addr = [], None
    for i in range(n):
        blk = data[i * UF2_BLOCK:(i + 1) * UF2_BLOCK]
        m0, m1, flags, addr, size, num, total = struct.unpack_from("<4sIIIIII", blk, 0)
        where = "%s block %d" % (name, i)
        if m0 != UF2_MAGIC0 or m1 != UF2_MAGIC1 or _u32(blk, 508) != UF2_MAGIC_END:
            raise fu.SafetyError("%s: bad UF2 magic" % where)
        if flags & UF2_FLAG_NOT_MAIN_FLASH:
            raise fu.SafetyError("%s is not a main-flash block (flags 0x%08X)" % (where, flags))
        if not 0 < size <= UF2_PAYLOAD_MAX:
            raise fu.SafetyError("%s: payload size %d" % (where, size))
        if num != i or total != n:
            raise fu.SafetyError("%s says it is block %d of %d" % (where, num, total))
        if next_addr is not None and addr != next_addr:
            raise fu.SafetyError("%s: address 0x%08X does not follow 0x%08X (not consecutive)"
                                 % (where, addr, next_addr))
        next_addr = addr + size
        out.append((blk, size))
    return out


def splice_uf2(placeholder_uf2: bytes, pattern: bytes, loader: bytes) -> bytes:
    """Replace the one occurrence of `pattern` in the UF2's flat image (its payloads
    joined in order) with `loader` (same length) and re-emit the same blocks: same
    headers, same addresses, same padding; only payload bytes change."""
    if not pattern:
        raise fu.SafetyError("the placeholder pattern is empty")
    if len(loader) != len(pattern):
        raise fu.SafetyError("the loader is %d bytes but the placeholder is %d; refusing"
                             % (len(loader), len(pattern)))
    blocks = parse_uf2(placeholder_uf2, "the placeholder firmware")
    flat = b"".join(blk[32:32 + size] for blk, size in blocks)
    at = flat.find(pattern)
    if at < 0:
        raise fu.SafetyError("the placeholder is not in the firmware image; refusing")
    if flat.find(pattern, at + 1) >= 0:
        raise fu.SafetyError("the placeholder occurs more than once in the firmware image; "
                             "refusing")
    flat = flat[:at] + loader + flat[at + len(loader):]
    out, pos = bytearray(), 0
    for blk, size in blocks:
        out += blk[:32] + flat[pos:pos + size] + blk[32 + size:]
        pos += size
    out = bytes(out)
    assert len(out) == len(placeholder_uf2) and pos == len(flat)
    parse_uf2(out, "the spliced firmware")
    return out


def rp2_volume_candidates():
    if sys.platform == "darwin":
        return [Path("/Volumes") / RP2_VOLUME]
    if sys.platform == "win32":
        return [Path("%s:\\" % c) for c in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return [Path("/media") / user / RP2_VOLUME, Path("/run/media") / user / RP2_VOLUME,
            Path("/media") / RP2_VOLUME, Path("/mnt") / RP2_VOLUME]


def find_rp2_volumes():
    found = []
    for p in rp2_volume_candidates():
        info = p / "INFO_UF2.TXT"
        try:
            if info.is_file() and RP2_VOLUME in info.read_text(errors="replace"):
                found.append(p)
        except OSError:
            continue
    return found


def wait_for(fn, timeout, step=0.5):
    deadline = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() >= deadline:
            return v
        time.sleep(step)


def touch_1200(port):
    serial = _serial()
    log.info("1200-baud touch on %s (reboots the RP2040 into its UF2 disk)", port)
    try:
        s = serial.Serial(port, 1200)
        s.close()
    except (serial.SerialException, OSError) as e:
        log.debug("1200-baud touch: %s (normal when the board resets at once)", e)


def cmd_flash_xiao(args):
    uf2 = Path(args.uf2)
    data = check_uf2(uf2)
    log.info("UF2         %s (%d bytes, %d blocks), sha256 %s", uf2, len(data), len(data) // 512,
             fu.sha256(data))
    if args.dry_run:
        log.info("DRY RUN: would find %s (or 1200-baud-touch the XIAO's port), copy %s to it, "
                 "and wait for the Transporter data port", RP2_VOLUME, uf2.name)
        return EXIT_OK
    vols = find_rp2_volumes()
    if not vols:
        if args.port:
            ports = [args.port]
        else:
            infos = rp2_ports()
            boards = {(p.serial_number or p.device) for p in infos}
            if len(boards) > 1:
                raise fu.SafetyError("%d RP2040 boards connected; refusing to guess. Unplug the "
                                     "others or pass --port:\n%s" % (len(boards), "\n".join(
                                         "  %s  %s" % (p.device, p.description) for p in infos)))
            ports = [p.device for p in infos]
        if not ports:
            raise fu.UnbrickError("no %s disk and no RP2040 serial port (USB VID 2E8A) found. "
                                  "Hold BOOT on the XIAO while plugging it in, then run this "
                                  "again." % RP2_VOLUME)
        touch_1200(ports[0])
        log.info("Waiting up to %d s for the %s disk ...", BOOTSEL_WAIT, RP2_VOLUME)
        vols = wait_for(find_rp2_volumes, BOOTSEL_WAIT)
        if not vols:
            raise fu.UnbrickError("no %s disk appeared within %d s. Hold BOOT on the XIAO while "
                                  "plugging it in, then run this again." % (RP2_VOLUME,
                                                                            BOOTSEL_WAIT))
    if len(vols) > 1:
        raise fu.SafetyError("several %s disks: %s; refusing to guess" % (
            RP2_VOLUME, ", ".join(map(str, vols))))
    vol = vols[0]
    dest = vol / uf2.name
    log.info("Copying %s to %s", uf2.name, dest)
    try:
        shutil.copyfile(str(uf2), str(dest))
    except OSError as e:
        # The RP2040 reboots as soon as the last block arrives; the OS may complain.
        log.info("  copy ended with %s (normal if the board rebooted at once)", e)
    if wait_for(lambda: not (vol / "INFO_UF2.TXT").exists(), REBOOT_WAIT):
        log.info("%s disk gone: the XIAO is rebooting into the new firmware", RP2_VOLUME)
    else:
        log.warning("the %s disk is still there after %d s: the copy may not have been "
                    "accepted", RP2_VOLUME, REBOOT_WAIT)
    log.info("Waiting up to %d s for a port that answers ping ...", REBOOT_WAIT)
    t = wait_for(lambda: probe_port(None), REBOOT_WAIT, step=1.0)
    try:
        ports = rp2_ports()
    except Exception:
        ports = []
    for p in ports:
        log.info("  port %s  %04X:%04X  %s", p.device, p.vid or 0, p.pid or 0, p.description)
    if not t:
        log.error("No port answers ping: the Transporter firmware is not running.")
        return EXIT_FAIL
    log.info("Transporter data port: %s (the other CDC port is the console)", t.port)
    t.close()
    return EXIT_OK


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def with_transporter(args, fn, transporter=None):
    t = transporter or connect(args)
    try:
        return fn(t)
    finally:
        t.close()


def cmd_status(args, transporter=None):
    def run(t):
        st = wait_status(t, args.wait)
        log.info("OK %s", " ".join("%s=%s" % kv for kv in st.items()))
        return EXIT_OK
    return with_transporter(args, run, transporter)


def cmd_info(args, transporter=None):
    def run(t):
        ready(t, args)
        check_info(t)
        return EXIT_OK
    return with_transporter(args, run, transporter)


def cmd_rekey(args, transporter=None):
    def run(t):
        log.info(t.rekey())
        log.info("The Transporter reboots into USB_KEY mode: switch the FM-1 off and on now.")
        return EXIT_OK
    return with_transporter(args, run, transporter)


def cmd_dump(args, transporter=None):
    labelled, v15 = load_labelled(args.v15, args.package)

    def run(t):
        ready(t, args)
        check_info(t)
        dump_and_analyse(t, args.dir, labelled, v15)
        return EXIT_OK
    return with_transporter(args, run, transporter)


def cmd_analyse(args):
    labelled, v15 = load_labelled(args.v15, args.package)
    path = Path(args.dump)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise fu.UnbrickError("cannot read %s: %s" % (path, e))
    rep = analyse_dump(data, labelled, v15, name=str(path))
    emit_report(rep.text)
    return EXIT_OK


def cmd_restore(args, transporter=None):
    log.info("Step 1/4: package")
    pkg = load_restore_package(args.package, args.verify_v15)
    log.info("Step 2/4: reference dump")
    ref_path = Path(args.ref)
    try:
        ref = ref_path.read_bytes()
    except OSError as e:
        raise fu.UnbrickError("cannot read --ref %s: %s" % (ref_path, e))
    plan = Plan(pkg, ref, ref_path)
    plan.report()

    def run(t):
        log.info("Step 3/4: Transporter")
        ready(t, args)
        log.info("Step 4/4: %s", "write + verify" if args.write else "dry run")
        return restore_session(t, plan, args, args.write)
    return with_transporter(args, run, transporter)


def cmd_recover(args, transporter=None):
    log.info("Step 1/5: package")
    pkg = load_restore_package(args.package, args.verify_v15)
    labelled, v15 = load_labelled(args.v15, args.extra_package, extra=pkg)

    def run(t):
        log.info("Step 2/5: Transporter and FM-1 identity")
        ready(t, args)
        check_info(t)
        log.info("Step 3/5: dump (twice) + analysis")
        path, data, _ = dump_and_analyse(t, args.out, labelled, v15)
        log.info("Step 4/5: restore plan (dry run)")
        plan = Plan(pkg, data, path)
        plan.report()
        log.info("Step 5/5: confirmation, write + verify")
        return restore_session(t, plan, args, True)
    return with_transporter(args, run, transporter)


# --------------------------------------------------------------------------
# setup: jl-uboot-tool's loader spliced into the prebuilt placeholder firmware
# --------------------------------------------------------------------------

RERUN_SETUP = "Run `python3 fm1_transporter_recover.py setup` again"


def read_checked(path: Path, want: str, what: str, fix: str) -> bytes:
    """The file's bytes if its sha256 is `want`; else a plain-language UnbrickError."""
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise fu.UnbrickError("cannot read %s (%s): %s. %s" % (what, path, e.strerror or e, fix))
    got = fu.sha256(data)
    if got != want:
        raise fu.UnbrickError("%s (%s) is not the expected file: its sha256 is %s, expected %s. %s"
                              % (what, path, got, want, fix))
    log.info("  %-34s sha256 %s  OK", what, got)
    return data


def build_transporter_uf2(jl_dir: Path) -> bytes:
    """Check the three inputs, splice, check the result: the firmware to flash."""
    placeholder = read_checked(PLACEHOLDER_UF2, PLACEHOLDER_UF2_SHA256, "placeholder firmware",
                               "This repository's transporter/ folder is damaged: download "
                               "the repository again.")
    pattern = read_checked(PLACEHOLDER_PATTERN, PLACEHOLDER_PATTERN_SHA256, "placeholder pattern",
                           "This repository's transporter/ folder is damaged: download the "
                           "repository again.")
    loader = read_checked(Path(jl_dir) / fu.WL82_LOADER_REL, WL82_LOADER_SHA256,
                          "JieLi loader wl82loader.bin",
                          ("Run `setup` first (it downloads jl-uboot-tool once into %s)."
                           if not Path(jl_dir).exists() else
                           "Delete the folder %s and run setup again (it downloads jl-uboot-tool "
                           "once).") % jl_dir)
    out = splice_uf2(placeholder, pattern, loader)
    got = fu.sha256(out)
    if got != TRANSPORTER_UF2_SHA256:
        raise fu.UnbrickError("the spliced firmware has sha256 %s, expected %s; nothing was "
                              "written. Download the repository again." % (got,
                                                                           TRANSPORTER_UF2_SHA256))
    log.info("  %-34s sha256 %s  OK", "spliced Transporter firmware", got)
    return out


class _DropMacNote(logging.Filter):
    """fm1_unbrick's setup ends with a note that its own USB route needs Windows or
    Linux, a pip note and "Setup complete."; none applies to the Transporter
    route, so they are hidden from the console (the log file keeps them)."""

    HIDE = (fu.MACOS_MESSAGE, "Skipping pip", "Setup complete.")

    def filter(self, record):
        msg = record.getMessage()
        return not any(h in msg for h in self.HIDE)


def fetch_jlub(jl_dir: Path):
    """fm1_unbrick's setup (git clone or GitHub zip at the pinned commit, or just a
    check of an existing folder); its pip step is skipped: only the loader is used."""
    ns = types.SimpleNamespace(jl_dir=str(jl_dir), source=None, dry_run=False, no_pip=True)
    filt = _DropMacNote()
    console = [h for h in fu.log.handlers if not isinstance(h, logging.FileHandler)]
    for h in console:
        h.addFilter(filt)
    try:
        fu.cmd_setup(ns)
    except fu.UnbrickError as e:
        raise fu.UnbrickError("jl-uboot-tool check failed: %s. If the problem stays, delete the "
                              "folder %s and run setup again (needs the internet once)."
                              % (e, jl_dir))
    except Exception as e:      # network, disk
        raise fu.UnbrickError("could not download jl-uboot-tool (%s). Check the internet "
                              "connection and run setup again." % e)
    finally:
        for h in console:
            h.removeFilter(filt)


def pyserial_ok() -> bool:
    try:
        import serial  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
    except ImportError:
        return False
    return True


def ask_yes_no(input_fn, prompt) -> bool:
    try:
        answer = input_fn(prompt)
    except EOFError:
        return False
    log.debug("answer %r", answer)
    return answer.strip().lower() in ("y", "yes")


def ensure_pyserial(args, input_fn, run=None):
    run = run or subprocess.run
    if pyserial_ok():
        log.info("  pyserial is installed")
        return
    cmd = [sys.executable, "-m", "pip", "install", "pyserial"]
    log.info("pyserial (the serial-port library this tool needs) is not installed.")
    if not args.yes and not ask_yes_no(input_fn, "Install it now with `%s`? [y/N] "
                                       % " ".join(cmd)):
        raise fu.UnbrickError("pyserial is needed. Install it with `%s` (or run setup again and "
                              "answer y)." % " ".join(cmd))
    log.info("Running %s", " ".join(cmd))
    try:
        res = run(cmd)
        rc = res.returncode
    except OSError as e:
        rc = str(e)
    if rc != 0 or not pyserial_ok():
        raise fu.UnbrickError("installing pyserial failed (%s). Check the internet connection, "
                              "then run `%s` yourself." % (rc, " ".join(cmd)))
    log.info("  pyserial installed")


def write_atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def cmd_setup(args, input_fn=input):
    jl_dir, out = Path(args.jl_dir), Path(args.uf2)
    if args.check:
        log.info("Checking every fingerprint (nothing is downloaded or written):")
        data = build_transporter_uf2(jl_dir)
        if out.is_file():
            got = fu.sha256(out.read_bytes())
            if got != fu.sha256(data):
                raise fu.UnbrickError("%s has sha256 %s, expected %s. %s to rebuild it."
                                      % (out, got, TRANSPORTER_UF2_SHA256, RERUN_SETUP))
            log.info("  %-34s sha256 %s  OK", str(out), got)
        else:
            log.info("  %s is not built yet: run setup", out)
        log.info("All fingerprints match.")
        return EXIT_OK

    log.info("Step 1/4: jl-uboot-tool at commit %s (only its wl82 loader is used)",
             fu.JLUB_COMMIT[:7])
    fetch_jlub(jl_dir)
    log.info("Step 2/4: checking the fingerprints of the firmware pieces")
    data = build_transporter_uf2(jl_dir)
    log.info("Step 3/4: the Transporter firmware file")
    if out.is_file() and out.read_bytes() == data:
        log.info("  %s is already built and correct", out)
    else:
        write_atomic(out, data)
        if fu.sha256(out.read_bytes()) != TRANSPORTER_UF2_SHA256:
            raise fu.UnbrickError("%s does not read back correctly (disk problem?). %s."
                                  % (out, RERUN_SETUP))
        log.info("  wrote %s", out)
    log.info("Transporter firmware: %s", out.resolve())
    log.info("  sha256 %s", TRANSPORTER_UF2_SHA256)
    log.info("Step 4/4: pyserial")
    ensure_pyserial(args, input_fn)
    log.info("Setup complete. Next: python3 fm1_transporter_recover.py wizard --v15 FM-1.fwsc")
    return EXIT_OK


# --------------------------------------------------------------------------
# wizard: the whole recovery, one step at a time, for someone doing it once
# --------------------------------------------------------------------------

V15_URL = "https://www.m-vave.com/download"

WIRING_TEXT = """\
  Wire the XIAO to the FM-1's USB-C pigtail (three wires):

    XIAO pin      pigtail wire   FM-1 USB
    D6 (GP0)      green          D+
    D7 (GP1)      white          D-
    GND           black          GND
    (nothing)     red            VBUS 5 V: NOT connected, tape it off

  Where the pins are: D6 and D7 are the two pins FARTHEST from the XIAO's USB-C
  socket, one on each edge. GND is the SECOND pin from the USB-C end on the 5 V
  side. Picture: transporter/xiao-wiring.svg (or TRANSPORTER-GUIDE.md).

  * The FM-1 must be switched OFF while you wire it.
  * Charge the FM-1 fully first: it runs on its battery during all of this.
  * Swapping D+ and D- damages nothing; the FM-1 is simply not found."""

FLASH_ADVICE = """\
  What to try:
  * Use a USB-C cable that carries data (a charge-only cable shows nothing).
  * Unplug the XIAO, hold its small B (BOOT) button, plug it in again, then let
    go: a disk called RPI-RP2 should appear on this computer.
  * Keep only one XIAO / Raspberry Pi Pico board plugged in.
  * Linux: if the port cannot be opened, run `sudo usermod -aG dialout $USER`,
    log out and back in."""

CONNECT_ADVICE = """\
  What to try:
  * Check the three wires: D6 green to D+, D7 white to D-, GND black to GND;
    the red wire must NOT be connected.
  * Switch the FM-1 OFF, unplug the XIAO from the computer and plug it back in,
    then switch the FM-1 ON again.
  * Make sure the FM-1's battery is charged.
  * If it still does not answer, quit (q) and run
    `python3 fm1_transporter_recover.py rekey`, then switch the FM-1 off and on."""

DUMP_ADVICE = """\
  What to try:
  * The two reads did not agree or the transfer stopped: press the wires
    firmly onto the pins, keep them away from the computer's power supply.
  * Then switch the FM-1 off, unplug and replug the XIAO, switch the FM-1 on."""

FINAL_TEXT = """\
  1. Switch the FM-1 OFF.
  2. Remove the three wires (and the pigtail) from the FM-1.
  3. Switch the FM-1 ON: it boots stock V15.
  4. Plug it into the computer normally and install whatever firmware you
     want, on a full battery, with the computer awake (no sleep) until the
     installer says it is done."""


class WizardQuit(fu.UnbrickError):
    """The person chose to stop (or there is no keyboard to answer on)."""


def clean_path(text: str) -> str:
    """A path typed or dragged into a terminal: quotes and macOS/Linux '\\ ' escapes removed."""
    p = text.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "'\"":
        p = p[1:-1]
    if os.sep == "/":
        p = p.replace("\\ ", " ")
    return os.path.expanduser(p)


class Wizard:
    STEPS = 6

    def __init__(self, args, input_fn=input, transporter=None):
        self.args, self.input_fn = args, input_fn
        self.t = transporter
        self.own_t = transporter is None
        self.v15 = None
        self.writing = False

    # talking to the person ---------------------------------------------------

    def say(self, text=""):
        for line in text.split("\n"):
            log.info(line)

    def step(self, n, title, about):
        log.info("")
        log.info("=" * 72)
        log.info("Step %d of %d: %s", n, self.STEPS, title)
        log.info("=" * 72)
        self.say(about)

    def ask(self, prompt):
        """The answer, or None when there is no keyboard (end of input)."""
        log.debug("prompt %r", prompt)
        try:
            answer = self.input_fn(prompt)
        except EOFError:
            log.debug("no input (EOF)")
            return None
        log.debug("answer %r", answer)
        return answer

    def pause(self, prompt="Press Enter when done"):
        answer = self.ask("%s (q quits): " % prompt)
        if answer is None:
            if self.args.yes:
                log.info("(no keyboard input; --yes: continuing)")
                return ""
            raise WizardQuit("no keyboard input to answer on; run the wizard in a terminal")
        if answer.strip().lower() in ("q", "quit", "exit"):
            raise WizardQuit("stopped at your request")
        return answer.strip()

    def attempt(self, what, fn, advice):
        """Run fn until it succeeds; after a failure explain and offer a retry."""
        while True:
            try:
                return fn()
            except WizardQuit:
                raise
            except fu.UnbrickError as e:
                problem = str(e)
            except Exception as e:      # e.g. a serial port that vanished
                log.debug("%s failed", what, exc_info=True)
                problem = "%s: %s" % (type(e).__name__, e)
            log.error("")
            log.error("Problem while %s: %s", what, problem)
            self.say(advice)
            answer = self.ask("Press Enter to try this step again (q quits): ")
            if answer is None or answer.strip().lower() in ("q", "quit", "exit"):
                raise WizardQuit("stopped after: %s" % problem)

    def confirm(self, args, lines, phrase):
        """restore_session's typed confirmation, asked through the wizard's input."""
        for line in lines:
            log.info(line)
        if args.yes:
            log.info("Confirmation skipped (--yes)")
        else:
            answer = self.ask("Type %s (capital letters) to write the FM-1 now; anything else "
                              "cancels: " % phrase)
            if answer is None or answer.strip() != phrase:
                raise WizardQuit("you did not type %s, so nothing was written. The FM-1 is "
                                 "unchanged; run the wizard again when you are ready" % phrase)
        self.writing = True

    # steps --------------------------------------------------------------------

    def get_v15(self):
        path = self.args.v15
        while True:
            if not path:
                self.say("  The wizard needs the official V15 firmware file, FM-1.fwsc.\n"
                         "  Download it from %s (M-VAVE does not allow it to be\n"
                         "  shared, so it is not included here)." % V15_URL)
                answer = self.ask("Path to FM-1.fwsc (you can drag the file into this window), "
                                  "q quits: ")
                if answer is None:
                    raise WizardQuit("no V15 file given; start the wizard again with "
                                     "--v15 /path/to/FM-1.fwsc")
                if answer.strip().lower() in ("q", "quit", "exit"):
                    raise WizardQuit("stopped at your request")
                path = clean_path(answer)
                if not path:
                    continue
            log.info("Checking %s ...", path)
            try:
                pkg = fu.Package.load(path)
            except Exception as e:
                log.error("  That file cannot be used: %s", e)
                log.error("  Give the path of FM-1.fwsc as downloaded from %s.", V15_URL)
                path = None
                continue
            if pkg.kind != "official-v15":
                log.error("  %s is %s, not the official V15.", pkg.path.name, pkg.description)
                log.error("  Download FM-1.fwsc again from %s and give its path.", V15_URL)
                path = None
                continue
            pkg.check_writable(False)
            log.info("  Official V15 verified: firmware sha256 %s", pkg.firmware_sha256)
            return pkg

    def step0_checks(self):
        a = self.args
        self.step(0, "checks", "  Checking that setup was run, that the Transporter firmware file "
                               "is intact,\n  that pyserial is installed, and that you have the "
                               "official V15 file.")
        uf2 = Path(a.uf2)
        if not uf2.is_file():
            msg = ("the Transporter firmware %s is missing. Run `python3 fm1_transporter_recover.py "
                   "setup` first (it needs the internet once), then start the wizard again" % uf2)
            if not a.dry_run:
                raise fu.UnbrickError(msg)
            log.warning("  DRY RUN: %s (a real run stops here)", msg)
        else:
            got = fu.sha256(uf2.read_bytes())
            if got != TRANSPORTER_UF2_SHA256:
                raise fu.UnbrickError("the Transporter firmware %s is not the expected file "
                                      "(sha256 %s). %s, then start the wizard again"
                                      % (uf2, got, RERUN_SETUP))
            log.info("  Transporter firmware %s  sha256 %s  OK", uf2, got)
        if pyserial_ok():
            log.info("  pyserial is installed")
        elif a.dry_run:
            log.info("  pyserial is not installed (not needed for a dry run)")
        else:
            raise fu.UnbrickError("pyserial is not installed. Run `python3 "
                                  "fm1_transporter_recover.py setup`, then start the wizard again")
        self.v15 = self.get_v15()

    def step1_flash(self):
        a = self.args
        self.step(1, "put the Transporter firmware on the XIAO",
                  "  Plug the XIAO RP2040 into this computer with a USB-C DATA cable.\n"
                  "  Nothing else is wired to it yet, and the FM-1 is not involved in this "
                  "step.\n  The wizard then copies the Transporter firmware onto the XIAO.")
        ns = argparse.Namespace(uf2=str(a.uf2), dry_run=a.dry_run, port=a.port)
        if a.dry_run:
            log.info("DRY RUN: skipping flash-xiao (no hardware is touched)")
            if Path(a.uf2).is_file():
                cmd_flash_xiao(ns)
            return
        answer = self.pause("Press Enter when the XIAO is plugged in (or type skip if it already "
                            "runs the Transporter firmware)")
        if answer.lower() == "skip":
            log.info("Skipping the flash: the XIAO already runs the Transporter firmware.")
            return

        def go():
            log.info("Flashing the XIAO ...")
            if cmd_flash_xiao(ns) != EXIT_OK:
                raise fu.UnbrickError("the XIAO did not come back with the Transporter firmware")
            log.info("The XIAO runs the Transporter firmware.")
        self.attempt("flashing the XIAO", go, FLASH_ADVICE)

    def step2_connect(self):
        a = self.args
        self.step(2, "wire the XIAO to the FM-1 and switch the FM-1 on", WIRING_TEXT)
        self.pause("Press Enter when the three wires are connected and the FM-1 is still OFF")
        self.say("  Now unplug the XIAO from the computer and plug it back in (this restarts\n"
                 "  the Transporter so it is ready), then switch the FM-1 ON.\n"
                 "  The FM-1's screen stays dark: that is expected.")
        self.pause("Press Enter right after you have switched the FM-1 on")

        def go():
            if self.t is None:
                log.info("Looking for the Transporter (up to %d s) ...", a.wait)
                self.t = connect(a)
            log.info("Waiting for the FM-1 to answer ...")
            try:
                ready(self.t, a)
                check_info(self.t)
            except BaseException:
                if self.own_t:
                    self.t.close()
                    self.t = None
                raise
        self.attempt("connecting to the FM-1", go, CONNECT_ADVICE)

    def step3_dump(self):
        a = self.args
        self.step(3, "back up the FM-1 and look at what is in it",
                  "  Reading the whole 1 MiB flash twice (about a minute) and saving it in %s.\n"
                  "  Nothing is written. Keep this file: it is the backup of your FM-1 as it "
                  "is now." % a.out)
        labelled, v15 = load_labelled(None, (), extra=self.v15)

        def go():
            return dump_and_analyse(self.t, a.out, labelled, v15)
        path, data, rep = self.attempt("reading the FM-1", go, DUMP_ADVICE)
        verdict = rep.lines[rep.lines.index("(f) Verdict") + 1:]
        log.info("")
        log.info("In short (the backup is %s):", path)
        for line in verdict:
            log.info(line)
        return path, data

    def step4_plan(self, path, data):
        self.step(4, "plan the repair (nothing is written)",
                  "  Comparing the backup with V15 sector by sector, then checking that the "
                  "FM-1\n  still holds exactly what was backed up. Nothing is written in this "
                  "step.")
        plan = Plan(self.v15, data, path)
        plan.report()
        restore_session(self.t, plan, self.args, False)
        return plan

    def step5_write(self, plan):
        self.step(5, "write V15 to the FM-1",
                  "  This is the only step that changes the FM-1: %d sectors of 4 KiB of the\n"
                  "  application area are rewritten with V15, then the whole flash is read\n"
                  "  back and compared. The first 16 KiB (the boot area) are never written.\n"
                  "  Do not switch anything off, do not unplug anything, keep the computer awake."
                  % len(plan.write))
        rc = restore_session(self.t, plan, self.args, True, confirm=self.confirm)
        if rc == EXIT_DANGER:
            self.say("\n  The write did not finish. Leave the FM-1 switched ON and the wires "
                     "connected,\n  then start the wizard again: it backs up again and writes "
                     "only what still\n  differs.")
        return rc

    def step6_done(self):
        self.step(6, "finish", FINAL_TEXT)

    def run(self):
        a = self.args
        if a.dry_run:
            log.info("DRY RUN: every step runs against a MOCK Transporter; no hardware is touched.")
        try:
            self.step0_checks()
            self.step1_flash()
            self.step2_connect()
            path, data = self.step3_dump()
            plan = self.step4_plan(path, data)
            rc = self.step5_write(plan)
            if rc != EXIT_OK:
                return rc
            self.step6_done()
            return EXIT_OK
        except WizardQuit as e:
            log.error("")
            log.error("Stopped: %s.", e)
            if not self.writing:
                log.error("Nothing was written to the FM-1. Start the wizard again whenever you "
                          "are ready.")
            return EXIT_FAIL
        except fu.UnbrickError as e:
            log.error("")
            log.error("Problem: %s", e)
            log.error("%s Fix the problem above, then start the wizard again; it repeats "
                      "every check." % ("" if self.writing else
                                        "Nothing was written to the FM-1."))
            return e.code
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.debug("unexpected error", exc_info=True)
            log.error("")
            log.error("Unexpected problem: %s: %s", type(e).__name__, e)
            log.error("%sThe log file below has the details; start the wizard again, and if it "
                      "happens again, open an issue with that log." %
                      ("" if self.writing else "Nothing was written to the FM-1. "))
            return EXIT_FAIL
        finally:
            if self.t is not None and self.own_t:
                self.t.close()


def cmd_wizard(args, transporter=None, input_fn=input):
    return Wizard(args, input_fn, transporter).run()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def int0(s):
    return int(s, 0)


def add_common(p, top):
    """Options accepted before or after the subcommand (SUPPRESS keeps the
    subparser from overwriting a value given before it)."""
    d = (lambda v: v) if top else (lambda v: argparse.SUPPRESS)
    p.add_argument("--port", default=d(None),
                   help="Transporter data port (default: probe for the one answering ping)")
    p.add_argument("--wait", type=float, default=d(60.0),
                   help="seconds to wait for the data port and the FM-1 (default 60)")
    p.add_argument("--dry-run", action="store_true", default=d(False),
                   help="use a MOCK Transporter in-process; never open a serial port")
    p.add_argument("--mock-flash", default=d(None),
                   help="dry run: seed the mock's 1 MiB flash from this dump")
    p.add_argument("--mock-absent", action="store_true", default=d(False),
                   help=argparse.SUPPRESS)
    p.add_argument("--mock-chip-key", type=int0, default=d(None), help=argparse.SUPPRESS)
    p.add_argument("--log-dir", default=d("logs"), help="log folder (default: ./logs)")


def build_parser():
    ap = argparse.ArgumentParser(
        prog="fm1_transporter_recover.py",
        description="Recover a hard-bricked M-VAVE FM-1 through the FM-1 Transporter "
                    "(XIAO RP2040 on the FM-1's USB D+/D-/GND).")
    ap.add_argument("--version", action="version", version=TOOL_VERSION)
    add_common(ap, True)
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def sp(name, help_):
        p = sub.add_parser(name, help=help_)
        add_common(p, False)
        return p

    p = sp("setup", "fetch JieLi's loader (jl-uboot-tool, pinned), build "
                    "transporter/fm1_transporter.uf2, install pyserial")
    p.add_argument("--jl-dir", default=str(fu.DEFAULT_JL_DIR),
                   help="jl-uboot-tool folder (default: %(default)s)")
    p.add_argument("--uf2", default=str(DEFAULT_UF2),
                   help="where to write the Transporter firmware (default: %(default)s)")
    p.add_argument("--yes", action="store_true", help="install pyserial without asking")
    p.add_argument("--check", action="store_true",
                   help="only verify the four sha256s (placeholder, pattern, loader, result)")
    p = sp("wizard", "the whole recovery, step by step (run `setup` first)")
    p.add_argument("--v15", help="the official FM-1.fwsc (V15); asked for if missing")
    p.add_argument("--out", default="backups", help="dump folder (default: ./backups)")
    p.add_argument("--uf2", default=str(DEFAULT_UF2),
                   help="the Transporter firmware (default: %(default)s)")
    p.add_argument("--yes", action="store_true",
                   help="skip the typed WRITE confirmation (and keyboard pauses without input)")

    p = sp("flash-xiao", "flash the Transporter firmware (.uf2) onto the XIAO RP2040")
    p.add_argument("uf2")
    sp("status", "wait for the FM-1 and print the Transporter status")
    sp("info", "check chip key 980F and flash id 856014")
    sp("rekey", "reboot the Transporter into USB_KEY mode")

    def labels(p):
        p.add_argument("--package", action="append", default=[],
                       help="label app sectors that equal this .fwsc (repeatable)")
        p.add_argument("--v15", help="the official FM-1.fwsc (V15): head comparison, label V15")

    p = sp("dump", "read the whole flash twice into DIR and analyse it")
    p.add_argument("dir")
    labels(p)
    p = sp("analyse", "offline report on a full dump")
    p.add_argument("dump")
    labels(p)
    p = sp("restore", "write the differing app sectors of PACKAGE (dry run without --write)")
    p.add_argument("package")
    p.add_argument("--ref", required=True, help="full dump; a fresh read must equal it")
    p.add_argument("--write", action="store_true", help="actually write (default: dry run)")
    p.add_argument("--yes", action="store_true", help="skip the typed WRITE confirmation")
    p.add_argument("--verify-v15", action="store_true",
                   help="refuse anything but the verified official V15")
    p = sp("recover", "info + dump + analyse + restore plan + confirmation + write + verify")
    p.add_argument("package")
    p.add_argument("--out", default="backups", help="dump folder (default: ./backups)")
    p.add_argument("--package", dest="extra_package", action="append", default=[],
                   help="extra .fwsc to label app sectors in the analysis (repeatable)")
    p.add_argument("--v15", help="the official FM-1.fwsc (V15) for the head comparison")
    p.add_argument("--yes", action="store_true", help="skip the typed WRITE confirmation")
    p.add_argument("--verify-v15", action="store_true",
                   help="refuse anything but the verified official V15")
    return ap


def main(argv=None, transporter=None, input_fn=None):
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.command, Path(args.log_dir))
    log.debug("args %r", vars(args))
    input_fn = input_fn or input
    try:
        cmd = args.command
        if cmd == "setup":
            return cmd_setup(args, input_fn)
        if cmd == "wizard":
            return cmd_wizard(args, transporter, input_fn)
        if cmd == "flash-xiao":
            return cmd_flash_xiao(args)
        if cmd == "analyse":
            return cmd_analyse(args)
        handler = {"status": cmd_status, "info": cmd_info, "rekey": cmd_rekey,
                   "dump": cmd_dump, "restore": cmd_restore, "recover": cmd_recover}[cmd]
        return handler(args, transporter)
    except fu.UnbrickError as e:
        log.error("ERROR: %s", e)
        return e.code
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return EXIT_FAIL
    finally:
        log.info("Log: %s", log_path)
        close_logging()


if __name__ == "__main__":
    sys.exit(main())
