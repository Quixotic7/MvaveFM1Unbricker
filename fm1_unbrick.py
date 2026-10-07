#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 MvaveFM1Unbricker contributors
"""
fm1_unbrick.py - restore a soft-bricked M-VAVE FM-1 over plain USB.

A soft-bricked FM-1 shows a black screen, is "not found" by the firmware
installers, and appears on the computer as a USB disk called
"WL82 UBOOT1.00": the JieLi WL82 (AC791N) chip's mask-ROM boot mode.
This script automates the community recovery procedure: it talks to that
boot mode through kagaimiq's jl-uboot-tool (MIT), backs up the whole flash
twice, extracts the firmware region from an FM-1 .fwsc package and writes
it at 0x4000, then reads it back and compares every byte.

Safety rules, enforced in code:
  * nothing is ever erased or written below 0x4000 (the boot head);
  * "erase chip" is never used;
  * a verified double backup is taken before any write;
  * the chip key (0x980F) and flash ID (0x856014) must match an FM-1.

Run `python fm1_unbrick.py --help` and see README.md.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import types
import urllib.request
import zipfile
from pathlib import Path

TOOL_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Flash layout of the FM-1 (1 MiB SPI NOR flash)
# --------------------------------------------------------------------------

FLASH_SIZE = 0x100000
SECTOR_SIZE = 0x1000           # smallest erase unit
BLOCK_SIZE = 0x10000           # large erase unit
PROTECTED_END = 0x4000         # 0x0000..0x3FFF = boot head: NEVER erased or written
FW_START = 0x4000              # firmware region: app, SDK files, loader
FW_LEN = 0x8F000
FW_END = FW_START + FW_LEN     # 0x93000 (exclusive)

EXPECTED_CHIP_KEY = 0x980F
EXPECTED_FLASH_ID = 0x856014   # 1 MiB SPI NOR, as reported by the FM-1

DEVICE_VENDOR = "WL82"
DEVICE_PRODUCT = "UBOOT1.00"
DEVICE_MODEL = "WL82 UBOOT1.00"

# Official M-VAVE V15. V15_LOGICAL_SHA256 is over logical[0x4000:0x93000] of the genuine
# FM-1.fwsc (NOT the flash region: the flash image starts at the UFW flash.bin offset, 0x400);
# it only identifies the genuine file. V15_FLASH_FW_SHA256 is the flash.bin region
# 0x4000..0x92FFF hash.
V15_IDENTITY = "FM-1_015"
V15_LOGICAL_SHA256 = "6edf3c37fb5bbbc33607c89375ee024d5477c17914d72221c8c68e58a8255686"
V15_FLASH_FW_SHA256 = None  # the flash.bin region hash: fill in from the genuine FM-1.fwsc (python3 fm1_unbrick.py extract FM-1.fwsc out.bin --verify-v15 prints it)

# .fwsc packaging: the first FWSC_MARKED_BLOCKS blocks of FWSC_BLOCK data
# bytes are each followed by one marker byte. The markers spell the package
# identity: char i = (marker_i - i - 1) & 0xFF; FWSC_NO_CHAR = no character.
FWSC_BLOCK = 0x2F
FWSC_MARKED_BLOCKS = 20
FWSC_NO_CHAR = 0x7D

# jl-uboot-tool, pinned.
JLUB_REPO = "https://github.com/kagaimiq/jl-uboot-tool"
JLUB_COMMIT = "adb3f18889e88ac512ce0a3c4d8cc3d3cb30696a"
JLUB_ZIP_URL = JLUB_REPO + "/archive/" + JLUB_COMMIT + ".zip"
JLUB_MARKER = ".fm1-unbrick-commit"
WL82_LOADER_REL = "data/loaderblobs/usb/wl82loader.bin"
WL82_LOADER_SHA256 = "d41da6126760c9d66660bcc0cac8d27d221806c5e369a8036921efe68dca5376"
LOADER_TARGET_SPI_NOR = 1      # jl-uboot-tool's default loader argument

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_JL_DIR = SCRIPT_DIR / "jl-uboot-tool"

MACOS_MESSAGE = ("not supported: jl-uboot-tool has no macOS SCSI back end; "
                 "use a Windows or Linux machine (a Raspberry Pi works)")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNSUPPORTED = 2
EXIT_DANGER = 3                # the firmware region may be incomplete: do not power off

log = logging.getLogger("fm1_unbrick")


class UnbrickError(Exception):
    """A refusal or failure with a message meant for the user."""

    def __init__(self, message, code=EXIT_FAIL):
        super().__init__(message)
        self.code = code


class SafetyError(UnbrickError):
    """An operation that the safety rules forbid."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def sha256(data) -> str:
    return hashlib.sha256(data).hexdigest()


def timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    path = directory / (stem + suffix)
    n = 2
    while path.exists():
        path = directory / ("%s-%d%s" % (stem, n, suffix))
        n += 1
    return path


def platform_supported() -> bool:
    return sys.platform == "win32" or sys.platform.startswith("linux")


class Progress:
    """A one-line progress display on stderr (only when it is a terminal)."""

    def __init__(self, label, total):
        self.label, self.total, self.done = label, max(total, 1), 0
        self.tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        self.last = -1

    def update(self, n):
        self.done += n
        pct = self.done * 100 // self.total
        if self.tty and pct != self.last:
            self.last = pct
            sys.stderr.write("\r  %-24s %3d%%" % (self.label, pct))
            sys.stderr.flush()

    def close(self):
        if self.tty:
            sys.stderr.write("\r  %-24s done\n" % self.label)
            sys.stderr.flush()


def setup_logging(command: str, log_dir: Path) -> Path:
    log.handlers[:] = []
    log.setLevel(logging.DEBUG)
    log.propagate = False
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(log_dir, "unbrick-" + timestamp(), ".log")
    fh = logging.FileHandler(str(path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)
    log.debug("fm1_unbrick %s, python %s, platform %s, command %r",
              TOOL_VERSION, sys.version.split()[0], sys.platform, command)
    return path


def close_logging():
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


# --------------------------------------------------------------------------
# .fwsc packages
# --------------------------------------------------------------------------

def parse_fwsc(data: bytes):
    """Return (identity, logical_image) of an FM-1 .fwsc package.

    The first FWSC_MARKED_BLOCKS blocks are FWSC_BLOCK data bytes plus one
    marker byte; everything after them is image data as-is.
    """
    stride = FWSC_BLOCK + 1
    head_len = stride * FWSC_MARKED_BLOCKS
    if len(data) < head_len:
        raise UnbrickError("not an FM-1 .fwsc package: only %d bytes" % len(data))
    image = bytearray()
    chars = []
    for i in range(FWSC_MARKED_BLOCKS):
        block = data[i * stride:(i + 1) * stride]
        image += block[:FWSC_BLOCK]
        marker = block[FWSC_BLOCK]
        if marker != FWSC_NO_CHAR:
            chars.append(chr((marker - i - 1) & 0xFF))
    image += data[head_len:]
    return "".join(chars), bytes(image)


def _crc16(data, c=0):
    """firmware/src/ota.c ota_crc16: poly 0x1021, init 0"""
    for b in data:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else c << 1
        c &= 0xFFFF
    return c


def _jl_enc(buf):
    """firmware/src/ota.c ota_jl_enc: UFW header cipher, key 0xFFFF (its own inverse)"""
    out, k = bytearray(buf), 0xFFFF
    for i in range(len(out)):
        out[i] ^= k & 0xFF
        k = ((k << 1) ^ (0x1021 if k & 0x8000 else 0)) & 0xFFFF
    return bytes(out)


def _u16(p, o):
    return p[o] | p[o + 1] << 8


def _u32(p, o):
    return _u16(p, o) | _u16(p, o + 2) << 16


def ufw_flash(image: bytes):
    """Return (fl_off, fl_len) of the UFW type-0 entry (flash.bin) of a logical image.

    Port of firmware/src/ota.c ota_ufw: the 0x40-byte header and the 0x50-byte
    entries are ciphered; the entry-list CRC is over the still-ciphered entries.
    Flash address X is logical[fl_off + X].
    """
    if len(image) < 0x400:
        raise UnbrickError("not a UFW package (shorter than its 0x400-byte header)")
    hdr = _jl_enc(image[:0x40])
    if _crc16(hdr[2:0x40]) != _u16(hdr, 0):
        raise UnbrickError("UFW header CRC fails: damaged or not an FM-1 package")
    nent = _u16(hdr, 8)
    if nent == 0 or nent > 11:
        raise UnbrickError("UFW header lists %d entries" % nent)
    if _crc16(image[0x40:0x40 + nent * 0x50]) != _u16(hdr, 2):
        raise UnbrickError("UFW entry list CRC fails: damaged package")
    fl = None
    for i in range(nent):
        e = _jl_enc(image[0x40 + i * 0x50:0x90 + i * 0x50])
        if _u16(e, 0) == 0:
            fl = (_u32(e, 8), _u32(e, 12))
    if fl is None:
        raise UnbrickError("no flash.bin (type 0) entry in the UFW header")
    off, length = fl
    if off + length > len(image):
        raise UnbrickError("flash.bin (0x%X bytes at 0x%X) runs past the package (0x%X bytes)"
                           % (length, off, len(image)))
    if length < FW_END:
        raise UnbrickError("flash.bin is only 0x%X bytes, it must reach 0x%X; this is not a "
                           "complete FM-1 package" % (length, FW_END))
    return fl


IDENTITY_RE = re.compile(r"^FM-1_(\d{3})$")


def classify(identity: str, logical_sha: str):
    """Return (kind, description) of a package. logical_sha is sha256 of
    logical[0x4000:0x93000] (identification of the genuine V15 file only)."""
    m = IDENTITY_RE.match(identity)
    if logical_sha == V15_LOGICAL_SHA256:
        return "official-v15", "official M-VAVE V15 (sha256 verified)"
    if identity == V15_IDENTITY:
        return "damaged-v15", ("claims to be official V15 but the firmware hash does "
                               "NOT match: damaged or not genuine")
    if not m:
        return "unknown", "unrecognised package identity %r" % identity
    num = m.group(1)
    if num.startswith("0"):
        return "official-other", ("official-style M-VAVE package %s (no known hash: "
                                  "cannot be verified)" % identity)
    if identity == "FM-1_920":
        return "felucca-family", "ChoralRoot (Felucca-family firmware)"
    return "felucca-family", "third-party Felucca-family firmware %s" % identity


class Package:
    def __init__(self, path, data):
        self.path = Path(path)
        self.file_sha256 = sha256(data)
        self.identity, self.image = parse_fwsc(data)
        if len(self.image) < FW_END:
            raise UnbrickError(
                "%s: the flash image is only 0x%X bytes, it must reach 0x%X; "
                "this is not a complete FM-1 package" % (self.path.name, len(self.image), FW_END))
        try:
            self.fl_off, self.fl_len = ufw_flash(self.image)
        except UnbrickError as e:
            raise UnbrickError("%s: %s" % (self.path.name, e))
        self.flash = self.image[self.fl_off:self.fl_off + self.fl_len]
        self.head = self.flash[:PROTECTED_END]
        self.firmware = self.flash[FW_START:FW_END]
        self.firmware_sha256 = sha256(self.firmware)
        self.head_sha256 = sha256(self.head)
        # identification only: the slice the V15 hash was originally computed over
        self.logical_sha256 = sha256(self.image[FW_START:FW_END])
        self.kind, self.description = classify(self.identity, self.logical_sha256)
        if (self.kind == "official-v15" and V15_FLASH_FW_SHA256 is not None
                and self.firmware_sha256 != V15_FLASH_FW_SHA256):
            self.kind, self.description = "damaged-v15", (
                "identifies as official V15 but the flash region hash does NOT match")

    @classmethod
    def load(cls, path):
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as e:
            raise UnbrickError("cannot read package %s: %s" % (path, e))
        return cls(path, data)

    def log_summary(self):
        log.info("Package     %s", self.path)
        log.info("  file sha256      %s", self.file_sha256)
        log.info("  identity         %s  = %s", self.identity or "(none)", self.description)
        log.info("  firmware region  0x%X..0x%X (0x%X bytes)", FW_START, FW_END - 1, FW_LEN)
        log.info("  flash.bin        offset 0x%X in the logical image, 0x%X bytes", self.fl_off,
                 self.fl_len)
        log.info("  firmware sha256  %s", self.firmware_sha256)
        if self.kind == "official-v15" and V15_FLASH_FW_SHA256 is None:
            log.info("  (V15 flash-region hash not yet pinned in this tool; the file is "
                     "identified by its logical hash %s)", V15_LOGICAL_SHA256)
        log.debug("  package head sha256 %s", self.head_sha256)

    def check_writable(self, i_know=False):
        """Refuse packages that must not be written."""
        if self.firmware.count(0xFF) == len(self.firmware) or not any(self.firmware):
            raise SafetyError("the firmware region of %s is blank; refusing" % self.path.name)
        if self.kind == "damaged-v15":
            raise SafetyError(
                "%s says it is official V15 but its hash does not match the genuine file "
                "(logical %s, flash region %s). "
                "Download FM-1.fwsc again from M-VAVE; refusing to write it."
                % (self.path.name, self.logical_sha256, self.firmware_sha256))
        if self.kind == "unknown":
            if not i_know:
                raise SafetyError("%s has no recognisable FM-1 identity (%r); refusing "
                                  "(--i-know overrides)" % (self.path.name, self.identity))
            log.warning("WARNING: unrecognised package written because of --i-know")
        if self.kind == "official-other":
            log.warning("NOTE: this package cannot be hash-verified; official V15 is the "
                        "recommended path.")


# --------------------------------------------------------------------------
# Device discovery (never opens or touches any disk)
# --------------------------------------------------------------------------

WIN_DISK_QUERY = ("Get-CimInstance Win32_DiskDrive | "
                  "Select-Object Index,Model,InterfaceType,PNPDeviceID,Size | "
                  "ConvertTo-Json -Compress")
WIN_PATH_RE = re.compile(r"^\\\\\.\\PhysicalDrive\d+$", re.IGNORECASE)
LINUX_PATH_RE = re.compile(r"^/dev/sg\d+$")


def parse_windows_disks(json_text: str):
    """Candidates from `Get-CimInstance Win32_DiskDrive | ... | ConvertTo-Json`."""
    text = (json_text or "").strip()
    if not text:
        return []
    items = json.loads(text)
    if isinstance(items, dict):
        items = [items]
    found = []
    for d in items:
        model = str(d.get("Model") or "")
        iface = str(d.get("InterfaceType") or "")
        index = d.get("Index")
        if index is None:
            continue
        if DEVICE_MODEL.lower() in model.lower() and iface.upper() == "USB":
            found.append({"path": "\\\\.\\PhysicalDrive%d" % int(index),
                          "description": "%s (disk %d, %s)" % (model, int(index), iface)})
    return found


def find_windows(run=subprocess.run):
    try:
        res = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WIN_DISK_QUERY],
                  capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise UnbrickError("could not run PowerShell to list disks: %s" % e)
    if res.returncode != 0:
        raise UnbrickError("Get-CimInstance Win32_DiskDrive failed: %s" % res.stderr.strip())
    return parse_windows_disks(res.stdout)


def _read_attr(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def scan_linux_sysfs(sysroot="/sys", devroot="/dev"):
    """Candidates from /sys/class/scsi_generic/sgN/device/{vendor,model}."""
    base = Path(sysroot) / "class" / "scsi_generic"
    found = []
    if not base.is_dir():
        return found
    for entry in sorted(base.iterdir(), key=lambda p: (len(p.name), p.name)):
        if not re.match(r"^sg\d+$", entry.name):
            continue
        dev = entry / "device"
        vendor = _read_attr(dev / "vendor")
        model = _read_attr(dev / "model")
        if vendor.upper() != DEVICE_VENDOR or not model.upper().startswith(DEVICE_PRODUCT):
            continue
        real = os.path.realpath(str(dev))
        if "/usb" not in real.replace("\\", "/"):
            continue
        found.append({"path": str(Path(devroot) / entry.name),
                      "description": "%s %s (%s, USB)" % (vendor, model, entry.name)})
    return found


def find_devices():
    if sys.platform == "win32":
        return find_windows()
    if sys.platform.startswith("linux"):
        if not Path("/sys/class/scsi_generic").is_dir():
            log.info("No /sys/class/scsi_generic: load the SCSI generic driver with "
                     "`sudo modprobe sg` and try again.")
        return scan_linux_sysfs()
    raise UnbrickError(MACOS_MESSAGE, EXIT_UNSUPPORTED)


def select_device(explicit, candidates, i_know=False):
    """Pick the one device to use; refuse anything ambiguous."""
    if explicit:
        if sys.platform == "win32" and not WIN_PATH_RE.match(explicit):
            raise SafetyError("--device must look like \\\\.\\PhysicalDriveN on Windows")
        if sys.platform.startswith("linux") and not LINUX_PATH_RE.match(explicit):
            raise SafetyError("--device must look like /dev/sgN on Linux")
        if candidates is not None and explicit not in [c["path"] for c in candidates]:
            if not i_know:
                raise SafetyError(
                    "%s is not listed as a %s USB disk; refusing (--i-know overrides, "
                    "the device must still answer as %s)" % (explicit, DEVICE_MODEL, DEVICE_VENDOR))
            log.warning("WARNING: %s not in the scan, used because of --i-know", explicit)
        return explicit
    if not candidates:
        raise UnbrickError(
            "No %s device found. Check: the FM-1 is switched on and plugged in directly with "
            "a data cable; it shows a black screen; on Windows run from an administrator "
            "PowerShell; on Linux run with sudo (and `sudo modprobe sg`). If it never appears "
            "as %s, see the FM-1 Transporter (README)." % (DEVICE_MODEL, DEVICE_MODEL))
    if len(candidates) > 1:
        listing = "\n".join("  %s  %s" % (c["path"], c["description"]) for c in candidates)
        raise SafetyError("%d candidate devices found; refusing to guess. Unplug the others "
                          "or pass --device explicitly:\n%s" % (len(candidates), listing))
    return candidates[0]["path"]


# --------------------------------------------------------------------------
# The device: jl-uboot-tool's loader protocol, or a mock
# --------------------------------------------------------------------------

class Fm1Device:
    """A WL82 in UBOOT mode with the USB loader running.

    `ldr` offers jl-uboot-tool's JL_LoaderV2 methods: chip_key(),
    online_device(), flash_read(), flash_write(), flash_erase_sector(),
    flash_erase_block(). Mutating access goes only through erase_range()
    and write_range() below, which enforce the 0x4000 rule.
    """

    def __init__(self, path, ldr, vendor=DEVICE_VENDOR, product=DEVICE_PRODUCT,
                 io_max=512, closer=None, mock=False):
        self.path, self.ldr, self.vendor, self.product = path, ldr, vendor, product
        self.io_max = io_max
        self.mock = mock
        self._closer = closer

    def chip_key(self):
        return self.ldr.chip_key()

    def flash_id(self):
        return self.ldr.online_device()["id"]

    def online_type(self):
        return self.ldr.online_device()["type"]

    def close(self):
        if self._closer:
            self._closer()
            self._closer = None


def read_flash(dev: Fm1Device, addr: int, length: int, label="Reading") -> bytes:
    if addr < 0 or length < 0 or addr + length > FLASH_SIZE:
        raise SafetyError("read 0x%X+0x%X is outside the flash" % (addr, length))
    out = bytearray()
    prog = Progress(label, length)
    try:
        while length > 0:
            n = min(length, dev.io_max)
            data = dev.ldr.flash_read(addr, n)
            if len(data) != n:
                raise UnbrickError("short read at 0x%X: %d of %d bytes" % (addr, len(data), n))
            out += data
            addr += n
            length -= n
            prog.update(n)
    finally:
        prog.close()
    return bytes(out)


def _check_mutable(addr: int, length: int):
    """The single gate for every erase and write."""
    if length <= 0:
        raise SafetyError("empty erase/write")
    if addr < PROTECTED_END:
        raise SafetyError("refusing to touch 0x%X: nothing below 0x%X is ever erased or "
                          "written (boot head)" % (addr, PROTECTED_END))
    if addr + length > FLASH_SIZE:
        raise SafetyError("0x%X+0x%X runs past the end of the flash" % (addr, length))


def erase_range(dev: Fm1Device, addr: int, length: int):
    """Erase exactly [addr, addr+length), like jl-uboot-tool's flash_erase()
    (64 KiB blocks where aligned, 4 KiB sectors elsewhere), but only on
    sector boundaries and never below 0x4000."""
    _check_mutable(addr, length)
    if addr % SECTOR_SIZE or length % SECTOR_SIZE:
        raise SafetyError("erase 0x%X+0x%X is not 4 KiB aligned; refusing (it would "
                          "destroy neighbouring data)" % (addr, length))
    end = addr + length
    prog = Progress("Erasing", length)
    try:
        while addr < end:
            if addr % BLOCK_SIZE == 0 and end - addr >= BLOCK_SIZE:
                _check_mutable(addr, BLOCK_SIZE)
                dev.ldr.flash_erase_block(addr)
                step = BLOCK_SIZE
            else:
                _check_mutable(addr, SECTOR_SIZE)
                dev.ldr.flash_erase_sector(addr)
                step = SECTOR_SIZE
            addr += step
            prog.update(step)
    finally:
        prog.close()


def write_range(dev: Fm1Device, addr: int, data: bytes):
    """Program already-erased flash in io_max chunks (as jl-uboot-tool does)."""
    _check_mutable(addr, len(data))
    prog = Progress("Writing", len(data))
    try:
        off = 0
        while off < len(data):
            chunk = data[off:off + dev.io_max]
            _check_mutable(addr + off, len(chunk))
            dev.ldr.flash_write(addr + off, chunk)
            off += len(chunk)
            prog.update(len(chunk))
    finally:
        prog.close()


class MockUboot:
    """Stands in for jl-uboot-tool's loader on an FM-1 (JL_LoaderV2 API).

    The flash is a bytearray with NOR semantics: erase sets 0xFF in whole
    4 KiB sectors / 64 KiB blocks, programming can only clear bits.
    Fault injection: flaky_read=N corrupts one byte of the N-th read call,
    flaky_write=N corrupts one byte of the N-th write call, bad_cell=ADDR
    makes that byte never program correctly.
    """

    def __init__(self, image=None, chip_key=EXPECTED_CHIP_KEY, flash_id=EXPECTED_FLASH_ID,
                 io_max=512, flaky_read=None, flaky_write=None, bad_cell=None):
        if image is None:
            image = synthetic_flash()
        if len(image) != FLASH_SIZE:
            raise ValueError("mock image must be exactly 0x%X bytes" % FLASH_SIZE)
        self.flash = bytearray(image)
        self.key, self.id, self.io_max = chip_key, flash_id, io_max
        self.flaky_read, self.flaky_write, self.bad_cell = flaky_read, flaky_write, bad_cell
        self.reads = 0
        self.writes = 0
        self.ops = []          # ("erase_sector"|"erase_block"|"write", addr, length)

    def chip_key(self, arg=0xAC6900):
        return self.key

    def online_device(self):
        return {"type": 0x03, "id": self.id}

    def read_id(self):
        return self.id

    def usb_buffer_size(self):
        return self.io_max

    def flash_read(self, addr, length):
        self.reads += 1
        data = bytearray(self.flash[addr:addr + length])
        if self.flaky_read is not None and self.reads == self.flaky_read and data:
            data[len(data) // 2] ^= 0x5A
        return bytes(data)

    def flash_erase_sector(self, addr):
        assert addr % SECTOR_SIZE == 0, "unaligned sector erase"
        self.ops.append(("erase_sector", addr, SECTOR_SIZE))
        self.flash[addr:addr + SECTOR_SIZE] = b"\xff" * SECTOR_SIZE

    def flash_erase_block(self, addr):
        assert addr % BLOCK_SIZE == 0, "unaligned block erase"
        self.ops.append(("erase_block", addr, BLOCK_SIZE))
        self.flash[addr:addr + BLOCK_SIZE] = b"\xff" * BLOCK_SIZE
        return 0

    def flash_write(self, addr, data):
        assert len(data) <= self.io_max, "write larger than the USB buffer"
        assert addr + len(data) <= FLASH_SIZE
        self.writes += 1
        self.ops.append(("write", addr, len(data)))
        data = bytearray(data)
        if self.flaky_write is not None and self.writes == self.flaky_write:
            data[0] ^= 0x01
        for i, b in enumerate(data):
            a = addr + i
            if a == self.bad_cell:
                b ^= 0x80
            self.flash[a] &= b

    def flash_erase_chip(self):
        raise AssertionError("erase chip must never be used")

    def lowest_touched(self):
        return min((op[1] for op in self.ops), default=None)


def synthetic_flash(seed=b"fm1-mock") -> bytes:
    """A deterministic 'bricked' flash image for dry runs."""
    out = bytearray()
    counter = 0
    while len(out) < FW_END:
        out += hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
        counter += 1
    out = out[:FW_END] + b"\xff" * (FLASH_SIZE - FW_END)
    return bytes(out)


def mock_device(args) -> Fm1Device:
    image = None
    if getattr(args, "mock_image", None):
        image = Path(args.mock_image).read_bytes()
    flaky = getattr(args, "mock_flaky", None)
    mock = MockUboot(image=image,
                     chip_key=getattr(args, "mock_chip_key", None) or EXPECTED_CHIP_KEY,
                     flaky_read=2 if flaky == "read" else None,
                     flaky_write=3 if flaky == "write" else None,
                     bad_cell=0x50000 if flaky == "write-always" else None)
    log.info("DRY RUN: using a MOCK FM-1 (no real device is opened, nothing is written to "
             "hardware)")
    return Fm1Device("mock://fm1-uboot", mock, io_max=mock.usb_buffer_size(), mock=True)


# --------------------------------------------------------------------------
# jl-uboot-tool: setup, import, real device
# --------------------------------------------------------------------------

def _git(*args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)


def jlub_commit(jl_dir: Path):
    """The commit of an existing checkout (git) or extracted zip (marker), or None."""
    if (jl_dir / ".git").exists() and shutil.which("git"):
        res = _git("-C", str(jl_dir), "rev-parse", "HEAD")
        if res.returncode == 0:
            return res.stdout.strip()
    marker = jl_dir / JLUB_MARKER
    if marker.is_file():
        return marker.read_text().strip()
    return None


def verify_jlub(jl_dir: Path):
    """Raise unless jl_dir is jl-uboot-tool at the pinned commit with an intact wl82 loader."""
    if not jl_dir.is_dir():
        raise UnbrickError("jl-uboot-tool not found at %s; run `python fm1_unbrick.py setup` "
                           "first" % jl_dir)
    commit = jlub_commit(jl_dir)
    if commit != JLUB_COMMIT:
        raise UnbrickError("%s is at commit %s, expected %s; delete the folder and run setup "
                           "again" % (jl_dir, commit, JLUB_COMMIT))
    for rel in ("jltech/uboot.py", "jltech/cipher.py", "scsiio/__init__.py",
                "data/chips.yaml", "data/usb-loaders.yaml", WL82_LOADER_REL):
        if not (jl_dir / rel).is_file():
            raise UnbrickError("%s is incomplete: %s missing; run setup again" % (jl_dir, rel))
    got = sha256((jl_dir / WL82_LOADER_REL).read_bytes())
    if got != WL82_LOADER_SHA256:
        raise UnbrickError("the wl82 loader blob has sha256 %s, expected %s; refusing"
                           % (got, WL82_LOADER_SHA256))
    return commit


def cmd_setup(args):
    jl_dir = Path(args.jl_dir)
    source = args.source or JLUB_REPO
    if jl_dir.exists():
        log.info("Found %s, verifying it", jl_dir)
    elif args.dry_run:
        log.info("DRY RUN: would fetch %s at %s into %s", source, JLUB_COMMIT, jl_dir)
    elif shutil.which("git"):
        log.info("Cloning %s into %s", source, jl_dir)
        res = _git("clone", "--quiet", source, str(jl_dir))
        if res.returncode != 0:
            raise UnbrickError("git clone failed: %s" % res.stderr.strip())
        res = _git("-C", str(jl_dir), "checkout", "--quiet", JLUB_COMMIT)
        if res.returncode != 0:
            raise UnbrickError("git checkout %s failed: %s" % (JLUB_COMMIT, res.stderr.strip()))
    else:
        if args.source:
            raise UnbrickError("--source needs git")
        log.info("git not found; downloading %s", JLUB_ZIP_URL)
        with urllib.request.urlopen(JLUB_ZIP_URL, timeout=120) as r:
            blob = r.read()
        log.debug("zip sha256 %s", sha256(blob))
        tmp = jl_dir.with_name(jl_dir.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(str(tmp))
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(str(tmp))
        inner = [p for p in tmp.iterdir() if p.is_dir()]
        if len(inner) != 1:
            raise UnbrickError("unexpected zip layout from GitHub")
        inner[0].rename(jl_dir)
        shutil.rmtree(str(tmp))
        (jl_dir / JLUB_MARKER).write_text(JLUB_COMMIT + "\n")

    if args.dry_run and not jl_dir.exists():
        log.info("DRY RUN: would run %s -m pip install -r %s", sys.executable,
                 jl_dir / "requirements.txt")
        return EXIT_OK
    commit = verify_jlub(jl_dir)
    log.info("jl-uboot-tool OK: commit %s, wl82 loader sha256 %s", commit, WL82_LOADER_SHA256)

    req = jl_dir / "requirements.txt"
    pip = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    if args.no_pip:
        log.info("Skipping pip (--no-pip). Requirements: %s", req)
    elif args.dry_run:
        log.info("DRY RUN: would run %s", " ".join(pip))
    else:
        log.info("Installing jl-uboot-tool's requirements: %s", " ".join(pip))
        res = subprocess.run(pip)
        if res.returncode != 0:
            raise UnbrickError("pip install failed (exit %d)" % res.returncode)
    if not platform_supported():
        log.info("Note: %s", MACOS_MESSAGE)
    log.info("Setup complete.")
    return EXIT_OK


def load_jlub(jl_dir: Path):
    """Import jl-uboot-tool's modules from its folder (nothing is vendored)."""
    path = str(Path(jl_dir).resolve())
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import yaml
        from jltech.uboot import JL_UBOOT, JL_LoaderV2
        from jltech.cipher import cipher_bytes, jl_crc_cipher
        from scsiio.common import SCSIException
    except ImportError as e:
        raise UnbrickError("cannot import jl-uboot-tool (%s); run `python fm1_unbrick.py "
                           "setup`" % e)
    try:
        from scsiio import SCSIDev
    except ImportError:
        SCSIDev = None
    return types.SimpleNamespace(yaml=yaml, JL_UBOOT=JL_UBOOT, JL_LoaderV2=JL_LoaderV2,
                                 cipher_bytes=cipher_bytes, jl_crc_cipher=jl_crc_cipher,
                                 SCSIException=SCSIException, SCSIDev=SCSIDev,
                                 dataroot=Path(path) / "data")


def scsi_inquiry(scsi):
    data = bytearray(36)
    scsi.execute(b"\x12\x00\x00" + len(data).to_bytes(2, "big") + b"\x00", None, data)
    return (data[8:16].decode("ascii", "replace").strip(),
            data[16:32].decode("ascii", "replace").strip(),
            data[32:36].decode("ascii", "replace").strip())


def upload_loader(jl, scsi, chipname="wl82"):
    """Upload and start the USB loader, exactly as jluboottool.py does."""
    chips = jl.yaml.safe_load((jl.dataroot / "chips.yaml").read_text())
    loaders = jl.yaml.safe_load((jl.dataroot / "usb-loaders.yaml").read_text())
    if chipname not in chips or chipname not in loaders:
        raise UnbrickError("jl-uboot-tool has no USB loader for %r" % chipname)
    spec, chipspec = loaders[chipname], chips[chipname]
    blob = (jl.dataroot / spec["file"]).read_bytes()
    if chipname == "wl82" and sha256(blob) != WL82_LOADER_SHA256:
        raise UnbrickError("unexpected wl82 loader blob; refusing")
    block_size = spec.get("blocksize", 512)
    cipher = spec.get("encryption", "none")
    quirks = (chipspec.get("uboot1.00") or {}).get("quirks") or {}
    rom_crypt = quirks.get("memory-rw-mengli-crypt") is True
    crypt = (not rom_crypt and cipher == "MengLi") or (rom_crypt and cipher != "MengLi")
    if cipher == "RxGp":
        raise UnbrickError("RxGp loaders are not supported here")
    uboot = jl.JL_UBOOT(scsi)
    address = spec["address"]
    log.debug("loader %s: %d bytes at 0x%X, block %d, cipher %s, host scramble %s",
              spec["file"], len(blob), address, block_size, cipher, crypt)
    for off in range(0, len(blob), block_size):
        block = blob[off:off + block_size]
        if crypt:
            block = jl.cipher_bytes(jl.jl_crc_cipher, block)
        uboot.mem_write(address + off, block)
    try:
        uboot.mem_jump(spec["address"], LOADER_TARGET_SPI_NOR)
    except jl.SCSIException as e:
        raise UnbrickError("failed to start the USB loader: %s" % e)
    log.info("USB loader uploaded and running (%s, 0x%X)", spec["file"], spec["address"])


def open_real_device(path, jl, scsi=None, wait=10.0) -> Fm1Device:
    if scsi is None:
        if jl.SCSIDev is None:
            raise UnbrickError(MACOS_MESSAGE, EXIT_UNSUPPORTED)
        deadline = time.monotonic() + wait
        while True:
            try:
                scsi = jl.SCSIDev(path)
                break
            except PermissionError:
                raise UnbrickError("permission denied opening %s: run from an administrator "
                                   "PowerShell (Windows) or with sudo (Linux)" % path)
            except OSError as e:
                if time.monotonic() > deadline:
                    raise UnbrickError("cannot open %s: %s" % (path, e))
                time.sleep(0.5)
    try:
        vendor, product, rev = scsi_inquiry(scsi)
        log.info("Device      %s: %s %s (%s)", path, vendor, product, rev)
        if vendor.upper() != DEVICE_VENDOR:
            raise SafetyError("%s answers as %r, not %s; this is not an FM-1 in UBOOT mode. "
                              "Not touching it." % (path, vendor, DEVICE_VENDOR))
        if product == DEVICE_PRODUCT:
            upload_loader(jl, scsi, vendor.lower())
        else:
            log.info("Device does not report %s; checking whether the loader already runs",
                     DEVICE_PRODUCT)
            try:
                jl.JL_LoaderV2(scsi).online_device()
            except Exception as e:
                raise SafetyError("%s reports %r and does not answer the loader protocol "
                                  "(%s); refusing" % (path, product, e))
        ldr = jl.JL_LoaderV2(scsi)
        try:
            io_max = int(ldr.usb_buffer_size())
        except Exception:
            io_max = 512
        if not 16 <= io_max <= 0xFFFF:
            io_max = 512
        return Fm1Device(path, ldr, vendor, product, io_max, closer=scsi.close)
    except BaseException:
        scsi.close()
        raise


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def ensure_platform(args):
    if args.dry_run or platform_supported():
        return
    raise UnbrickError(MACOS_MESSAGE, EXIT_UNSUPPORTED)


def check_privileges(path):
    if sys.platform == "win32":
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                raise UnbrickError("run this from an administrator PowerShell (right-click "
                                   "Start > Terminal (Admin))")
        except (AttributeError, OSError):
            pass
    elif sys.platform.startswith("linux") and os.path.exists(path):
        if not os.access(path, os.R_OK | os.W_OK):
            raise UnbrickError("no read/write access to %s: run with sudo" % path)


def connect(args) -> Fm1Device:
    """find + open + loader. Real hardware unless --dry-run."""
    if args.dry_run:
        return mock_device(args)
    ensure_platform(args)
    jl_dir = Path(args.jl_dir)
    verify_jlub(jl_dir)
    jl = load_jlub(jl_dir)
    candidates = find_devices()
    for c in candidates:
        log.info("Candidate   %s  %s", c["path"], c["description"])
    path = select_device(args.device, candidates, args.i_know)
    check_privileges(path)
    return open_real_device(path, jl)


def check_identity(dev: Fm1Device, i_know=False):
    key = dev.chip_key()
    ondev = dev.ldr.online_device()
    fid = ondev["id"]
    log.info("Chip key    0x%04X (expected 0x%04X)", key, EXPECTED_CHIP_KEY)
    log.info("Flash ID    0x%06X (expected 0x%06X, 1 MiB), type 0x%02X", fid,
             EXPECTED_FLASH_ID, ondev.get("type", 0))
    log.info("USB buffer  %d bytes", dev.io_max)
    problems = []
    if key != EXPECTED_CHIP_KEY:
        problems.append("chip key 0x%04X is not 0x%04X" % (key, EXPECTED_CHIP_KEY))
    if fid != EXPECTED_FLASH_ID:
        problems.append("flash ID 0x%06X is not 0x%06X" % (fid, EXPECTED_FLASH_ID))
    if problems:
        if not i_know:
            raise SafetyError("this does not look like an FM-1: %s. Stopping; nothing was "
                              "written (--i-know overrides)." % "; ".join(problems))
        log.warning("WARNING: %s; continuing because of --i-know", "; ".join(problems))


MARKER_PATTERNS = [
    (re.compile(rb"\[FELUCCA\]"), "Felucca-family config tag"),
    (re.compile(rb"FELUCCA-LOADER-\d+"), "Felucca-family loader"),
    (re.compile(rb"FM-1_\d{3}"), "FM-1 package identity string"),
]


def analyse_dump(data: bytes, packages=()):
    """Report what is in a full flash dump."""
    head, fw = data[:PROTECTED_END], data[FW_START:FW_END]
    fw_sha = sha256(fw)
    log.info("Analysis")
    log.info("  boot head 0x0000..0x3FFF sha256  %s", sha256(head))
    log.info("  firmware  0x4000..0x92FFF sha256 %s", fw_sha)
    if V15_FLASH_FW_SHA256 is not None and fw_sha == V15_FLASH_FW_SHA256:
        log.info("  firmware region = official M-VAVE V15 (exact)")
    sectors = FW_LEN // SECTOR_SIZE
    blank = sum(1 for i in range(sectors)
                if fw[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE] == b"\xff" * SECTOR_SIZE)
    log.info("  firmware sectors erased (all 0xFF): %d of %d%s", blank, sectors,
             "  <- looks like an interrupted install" if blank > 8 else "")
    for rx, what in MARKER_PATTERNS:
        hits = [(m.start(), m.group(0).decode("ascii", "replace")) for m in rx.finditer(data)]
        if hits:
            log.info("  marker: %s %s", what,
                     ", ".join("%r at 0x%05X" % (t, o) for o, t in hits[:4]))
    for pkg in packages:
        same = sum(1 for i in range(sectors)
                   if fw[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE]
                   == pkg.firmware[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE])
        log.info("  vs %s (%s): firmware sectors identical %d/%d%s", pkg.path.name,
                 pkg.identity, same, sectors, "  = exact copy" if same == sectors else "")
        if pkg.kind == "official-v15":
            if head == pkg.head:
                log.info("  boot head matches the official V15 package's head")
            else:
                log.warning("  boot head DIFFERS from the official V15 package's head. This "
                            "tool never writes the boot head; see README, Troubleshooting.")
        else:
            log.info("  boot head %s %s's head (informational: third-party packages may "
                     "carry their own head)", "matches" if head == pkg.head else "differs from",
                     pkg.path.name)


def take_backup(dev: Fm1Device, outdir: Path, packages=()):
    """Read the whole flash twice, compare, save, re-check the saved file."""
    outdir.mkdir(parents=True, exist_ok=True)
    log.info("Backing up the whole flash (0x0..0x%X), twice", FLASH_SIZE - 1)
    a = read_flash(dev, 0, FLASH_SIZE, "Backup read 1/2")
    log.info("  read 1 sha256 %s", sha256(a))
    b = read_flash(dev, 0, FLASH_SIZE, "Backup read 2/2")
    log.info("  read 2 sha256 %s", sha256(b))
    if a != b:
        diffs = [i for i in range(FLASH_SIZE) if a[i] != b[i]]
        raise UnbrickError(
            "the two backup reads DIFFER (%d bytes, first at 0x%X): the USB connection is "
            "not reliable. Nothing was written. Use another (data) cable or port, plug the "
            "FM-1 in directly (no hub) and try again." % (len(diffs), diffs[0]))
    stem = "fm1-flash-backup-" + timestamp() + ("-dryrun" if dev.mock else "")
    path = unique_path(outdir, stem, ".bin")
    with open(str(path), "xb") as f:
        f.write(a)
        f.flush()
        os.fsync(f.fileno())
    if path.read_bytes() != a:
        raise UnbrickError("the backup file %s does not read back correctly; check the disk"
                           % path)
    log.info("Backup OK   %s", path)
    log.info("  sha256 %s (both reads identical)", sha256(a))
    analyse_dump(a, packages)
    return path, a


def confirm(args, lines, phrase):
    for line in lines:
        log.info(line)
    if args.yes:
        log.info("Confirmation skipped (--yes)")
        return
    if not stdin_is_tty():
        raise UnbrickError("no terminal to confirm on; re-run interactively or pass --yes")
    answer = ask("Type %s to continue (anything else cancels): " % phrase)
    log.debug("confirmation answer %r", answer)
    if answer.strip() != phrase:
        raise UnbrickError("cancelled; nothing was written")


def stdin_is_tty():
    return sys.stdin is not None and sys.stdin.isatty()


def ask(prompt):
    return input(prompt)


def write_and_verify(dev: Fm1Device, data: bytes):
    """Erase+write FW region once, verify, retry once. Returns True on success."""
    assert len(data) == FW_LEN
    for attempt in (1, 2):
        log.info("Write attempt %d: erase+write 0x%X..0x%X", attempt, FW_START, FW_END - 1)
        erase_range(dev, FW_START, FW_LEN)
        write_range(dev, FW_START, data)
        log.info("Verifying: reading back 0x%X bytes from 0x%X", FW_LEN, FW_START)
        back = read_flash(dev, FW_START, FW_LEN, "Verify")
        log.info("  read-back sha256 %s", sha256(back))
        if back == data:
            log.info("  byte-for-byte identical")
            return True
        diffs = [i for i in range(FW_LEN) if back[i] != data[i]]
        log.error("  MISMATCH: %d bytes differ, first at 0x%X", len(diffs), FW_START + diffs[0])
        if attempt == 1:
            log.info("Retrying the write once")
    return False


DANGER_TEXT = """
!!! The firmware region could not be verified. !!!
  * Do NOT power off, unplug or restart the FM-1.
  * The boot head (0x0000-0x3FFF) was not touched.
  * Re-run the same restore command (it backs up again and rewrites), if possible
    with another cable or USB port. Your backup: %s
  * If it keeps failing, follow UNBRICK-GUIDE.md (manual write) and ask for help.
"""

DONE_TEXT = """
Done. Now:
  1. Unplug the USB cable.
  2. Switch the FM-1 off.
  3. Wait 5 seconds.
  4. Switch it on.
"""


def cmd_find(args):
    if args.dry_run:
        cands = [{"path": "mock://fm1-uboot", "description": DEVICE_MODEL + " (MOCK)"}]
    else:
        ensure_platform(args)
        cands = find_devices()
    if not cands:
        log.info("No %s device found.", DEVICE_MODEL)
    for c in cands:
        log.info("%s  %s", c["path"], c["description"])
    if args.dry_run:
        return EXIT_OK
    select_device(args.device, cands, args.i_know)
    log.info("Exactly one device: OK")
    return EXIT_OK


def with_device(args, fn, device=None):
    dev = device or connect(args)
    try:
        return fn(dev)
    finally:
        dev.close()


def cmd_info(args, device=None):
    def run(dev):
        check_identity(dev, args.i_know)
        log.info("This is an FM-1 in UBOOT mode: OK")
        return EXIT_OK
    return with_device(args, run, device)


def cmd_backup(args, device=None):
    packages = [Package.load(p) for p in (args.package or [])]

    def run(dev):
        check_identity(dev, args.i_know)
        take_backup(dev, Path(args.dir), packages)
        return EXIT_OK
    return with_device(args, run, device)


def cmd_extract(args):
    pkg = Package.load(args.package)
    pkg.log_summary()
    if args.verify_v15 and pkg.kind != "official-v15":
        log.error("NOT the official V15 firmware: expected logical sha256 %s%s. Stop here.",
                  V15_LOGICAL_SHA256, "" if V15_FLASH_FW_SHA256 is None
                  else " and flash-region sha256 %s" % V15_FLASH_FW_SHA256)
        return EXIT_FAIL
    if args.verify_v15:
        log.info("V15 file identified (logical hash verified).")
        if V15_FLASH_FW_SHA256 is None:
            log.info("V15 flash-region sha256 %s (not yet pinned in this tool)",
                     pkg.firmware_sha256)
        else:
            log.info("V15 flash-region hash verified.")
    out = Path(args.out)
    out.write_bytes(pkg.firmware)
    log.info("Wrote %s (0x%X bytes); write it at 0x%X only.", out, len(pkg.firmware), FW_START)
    return EXIT_OK


def cmd_restore(args, device=None):
    log.info("Step 1/6: package")
    pkg = Package.load(args.package)
    pkg.log_summary()
    pkg.check_writable(args.i_know)
    if args.verify_v15 and pkg.kind != "official-v15":
        raise SafetyError("--verify-v15 given but this is not the verified official V15")

    def run(dev):
        log.info("Step 2/6: device")
        check_identity(dev, args.i_know)
        log.info("Step 3/6: backup (mandatory)")
        backup_path, dump = take_backup(dev, Path("backups"), [pkg])
        if dump[FW_START:FW_END] == pkg.firmware and not args.force_write:
            log.info("The firmware region already holds exactly this firmware; nothing to "
                     "write. (If it still does not boot, the cause is elsewhere: see README, "
                     "Troubleshooting. --force-write rewrites anyway.)")
            log.info(DONE_TEXT)
            return EXIT_OK
        log.info("Step 4/6: confirmation")
        confirm(args, [
            "",
            "About to WRITE the FM-1 flash:",
            "  device    %s (%s %s)%s" % (dev.path, dev.vendor, dev.product,
                                          "  [MOCK]" if dev.mock else ""),
            "  package   %s" % pkg.path.name,
            "  identity  %s = %s" % (pkg.identity or "(none)", pkg.description),
            "  size      0x%X bytes (%d)" % (FW_LEN, FW_LEN),
            "  address   0x%X .. 0x%X (boot head 0x0000-0x3FFF is never touched)"
            % (FW_START, FW_END - 1),
            "  backup    %s" % backup_path,
            "",
        ], "WRITE")
        log.info("Step 5/6: write + verify. Do not unplug anything now.")
        try:
            ok = write_and_verify(dev, pkg.firmware)
        except KeyboardInterrupt:
            log.error("Interrupted during the write.")
            log.error(DANGER_TEXT % backup_path)
            return EXIT_DANGER
        except Exception as e:
            log.error("Write failed: %s", e)
            log.error(DANGER_TEXT % backup_path)
            return EXIT_DANGER
        if not ok:
            log.error(DANGER_TEXT % backup_path)
            return EXIT_DANGER
        log.info("Step 6/6: restored %s (%s), verified byte for byte.", pkg.identity,
                 pkg.description)
        log.info(DONE_TEXT)
        return EXIT_OK
    return with_device(args, run, device)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def int0(s):
    return int(s, 0)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="use a MOCK device; never open or write real hardware")
    common.add_argument("--device", help=r"device path (\\.\PhysicalDriveN or /dev/sgN); "
                                         "default: the single one found")
    common.add_argument("--i-know", action="store_true",
                        help="override the chip key / flash ID / identity checks")
    common.add_argument("--jl-dir", default=str(DEFAULT_JL_DIR),
                        help="jl-uboot-tool folder (default: %(default)s)")
    common.add_argument("--log-dir", default="logs", help="log folder (default: ./logs)")
    common.add_argument("--mock-image", help="dry run: seed the mock flash from a 1 MiB file")
    common.add_argument("--mock-flaky", choices=["read", "write", "write-always"],
                        help="dry run: inject a fault (one bad read / one bad write / a dead cell)")
    common.add_argument("--mock-chip-key", type=int0, help=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(
        prog="fm1_unbrick.py",
        description="Restore a soft-bricked M-VAVE FM-1 (USB disk 'WL82 UBOOT1.00') by "
                    "writing the firmware region at 0x4000 through jl-uboot-tool.")
    ap.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("setup", parents=[common],
                       help="fetch jl-uboot-tool (pinned) and install its requirements")
    p.add_argument("--source", help="clone from this URL/path instead of GitHub")
    p.add_argument("--no-pip", action="store_true", help="do not run pip")

    sub.add_parser("find", parents=[common], help="list WL82 UBOOT1.00 devices")
    sub.add_parser("info", parents=[common], help="upload the loader, show chip key and flash ID")

    p = sub.add_parser("backup", parents=[common], help="read the whole flash twice into DIR")
    p.add_argument("dir")
    p.add_argument("--package", action="append",
                   help="also compare the dump with this .fwsc (repeatable)")

    p = sub.add_parser("extract", parents=[common],
                       help="extract the firmware region 0x4000..0x92FFF from a .fwsc")
    p.add_argument("package")
    p.add_argument("out")
    p.add_argument("--verify-v15", action="store_true",
                   help="fail unless it is the official V15 firmware")

    p = sub.add_parser("restore", parents=[common],
                       help="find + info + backup + write at 0x4000 + verify")
    p.add_argument("package")
    p.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    p.add_argument("--verify-v15", action="store_true",
                   help="refuse unless the package is the verified official V15")
    p.add_argument("--force-write", action="store_true",
                   help="write even if the device already holds this exact firmware")
    return ap


def main(argv=None, device=None):
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.command, Path(args.log_dir))
    log.debug("args %r", vars(args))
    try:
        if args.command == "setup":
            return cmd_setup(args)
        if args.command == "find":
            return cmd_find(args)
        if args.command == "info":
            return cmd_info(args, device)
        if args.command == "backup":
            return cmd_backup(args, device)
        if args.command == "extract":
            return cmd_extract(args)
        if args.command == "restore":
            return cmd_restore(args, device)
        return EXIT_UNSUPPORTED
    except UnbrickError as e:
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
