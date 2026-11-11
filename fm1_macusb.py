# SPDX-License-Identifier: MIT
# Copyright (c) 2026 MvaveFM1Unbricker contributors
"""
fm1_macusb.py - the FM-1's ROM boot mode over plain USB on macOS.

On Windows and Linux, jl-uboot-tool hands its SCSI commands to the operating
system (SCSI pass-through / SG_IO). macOS has no such pass-through for a USB
disk, so jl-uboot-tool has no macOS back end. This module is one: it takes the
"WL82 UBOOT1.00" device away from macOS's disk driver (which needs root, as
for M-Flasher) and speaks USB Mass Storage Bulk-Only Transport itself, through
libusb (pyusb, with the libusb that the `libusb-package` wheel bundles, or a
Homebrew one).

BotScsiDev offers the same surface as jl-uboot-tool's scsiio.SCSIDev
(open / close / execute(cdb, data_out, data_in)), so everything above it,
jl-uboot-tool's protocol classes and fm1_unbrick's safety rules, is unchanged.
Nothing here decides what is written to the flash.
"""

from __future__ import annotations

import errno
import re
import struct
import threading
import time

VID = 0x4C4A                   # the WL82's ROM boot mode ("WL82 UBOOT1.00")
PID = 0x8057
PATH_RE = re.compile(r"^usb:(\d+):(\d+)$")

CBW_SIG = b"USBC"
CSW_SIG = b"USBS"
CSW_LEN = 13
TIMEOUT_MS = 10000
MASS_STORAGE = 0x08
BOT_PROTOCOL = 0x50

# The WL82 resets itself when its USB goes quiet for a few seconds. On Windows and Linux the
# disk driver keeps it busy (TEST UNIT READY every second or two); here the driver is detached,
# so the transport does that itself whenever the line has been idle this long.
KEEPALIVE_S = 1.0
TEST_UNIT_READY = bytes(6)

HOMEBREW_LIBUSB =("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib")


class MacUsbError(Exception):
    """pyusb / libusb missing, or the device cannot be taken."""


def _usb():
    try:
        import usb.core
        import usb.util
    except ImportError:
        raise MacUsbError("pyusb is not installed: run `python3 fm1_unbrick.py setup` (it installs "
                          "pyusb and libusb-package)")
    return usb


def backend():
    """libusb: the libusb-package wheel's, else Homebrew's, else whatever pyusb finds."""
    usb = _usb()
    try:
        import libusb_package
        be = libusb_package.get_libusb1_backend()
        if be is not None:
            return be
    except ImportError:
        pass
    import usb.backend.libusb1 as lb
    for path in HOMEBREW_LIBUSB:
        be = lb.get_backend(find_library=lambda _name, p=path: p)
        if be is not None:
            return be
    be = lb.get_backend()
    if be is None:
        raise MacUsbError("libusb not found: run `python3 fm1_unbrick.py setup` (it installs "
                          "libusb-package), or `brew install libusb`")
    del usb
    return be


def find(be=None, usb=None):
    """Every WL82 UBOOT device on USB, as fm1_unbrick candidates ({"path", "description"}).
    Reading descriptors needs no root."""
    usb = usb or _usb()
    be = be or backend()
    found = []
    for d in usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=be) or ():
        found.append({"path": "usb:%d:%d" % (d.bus, d.address),
                      "description": "WL82 UBOOT1.00 (USB %04X:%04X, bus %d, address %d)"
                      % (VID, PID, d.bus, d.address)})
    return found


def _is(e, code):
    return getattr(e, "errno", None) == code or getattr(e, "backend_error_code", None) == {
        errno.EPIPE: -9, errno.EACCES: -3, errno.ETIMEDOUT: -7}.get(code)


class BotScsiDev:
    """jl-uboot-tool's SCSIDev, over USB Bulk-Only Transport (macOS)."""

    def __init__(self, path, scsi_exception=Exception, usb=None, be=None, device=None,
                 keepalive=KEEPALIVE_S):
        self.path, self.SCSIException = path, scsi_exception
        self._usbmod, self._be, self._device = usb, be, device
        self.dev = None
        self.is_open = False
        self.tag = 0
        self._lock = threading.RLock()
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._ka_thread = None
        self.keepalives = 0
        self.keepalive_errors = 0
        self.open(path)
        if keepalive:
            self._ka_thread = threading.Thread(target=self._keepalive, args=(keepalive,),
                                               name="fm1-usb-keepalive", daemon=True)
            self._ka_thread.start()

    def _keepalive(self, every):
        """TEST UNIT READY whenever the line has been idle `every` seconds (see KEEPALIVE_S)."""
        while not self._stop.wait(min(0.25, every / 4)):
            if time.monotonic() - self._last < every:
                continue
            with self._lock:
                if not self.is_open or self._stop.is_set():
                    return
                if time.monotonic() - self._last < every:
                    continue
                try:
                    self._transfer(TEST_UNIT_READY, None, None)
                    self.keepalives += 1
                except Exception:
                    self.keepalive_errors += 1
                    try:
                        self.reset_recovery()
                    except Exception:
                        pass

    # -- opening ---------------------------------------------------------
    def open(self, path):
        usb = self._usbmod or _usb()
        self._usbmod = usb
        dev = self._device
        if dev is None:
            m = PATH_RE.match(path or "")
            if not m:
                raise MacUsbError("not a macOS USB device path: %r (expected usb:BUS:ADDRESS)" % path)
            bus, addr = int(m.group(1)), int(m.group(2))
            be = self._be or backend()
            dev = usb.core.find(idVendor=VID, idProduct=PID, bus=bus, address=addr, backend=be)
            if dev is None:
                raise OSError(errno.ENODEV, "no WL82 UBOOT device at %s (unplugged or "
                                            "re-enumerated? run `find` again)" % path)
        try:
            cfg = dev.get_active_configuration()
        except usb.core.USBError:
            dev.set_configuration()
            cfg = dev.get_active_configuration()
        intf = None
        for i in cfg:
            if i.bInterfaceClass == MASS_STORAGE:
                intf = i
                break
        if intf is None:
            raise MacUsbError("%s has no mass-storage interface" % path)
        self.ifnum = intf.bInterfaceNumber
        eps = list(intf)
        self.ep_out = next((e for e in eps if not e.bEndpointAddress & 0x80
                            and e.bmAttributes & 3 == 2), None)
        self.ep_in = next((e for e in eps if e.bEndpointAddress & 0x80
                           and e.bmAttributes & 3 == 2), None)
        if self.ep_out is None or self.ep_in is None:
            raise MacUsbError("%s has no bulk endpoints" % path)
        try:
            if dev.is_kernel_driver_active(self.ifnum):
                dev.detach_kernel_driver(self.ifnum)
            usb.util.claim_interface(dev, self.ifnum)
        except usb.core.USBError as e:
            if _is(e, errno.EACCES) or "ccess" in str(e):
                raise PermissionError(errno.EACCES, "macOS's disk driver holds the FM-1; only an "
                                                    "administrator can take it: run with sudo")
            raise MacUsbError("cannot take %s from macOS's disk driver: %s" % (path, e))
        self.dev = dev
        self.is_open = True

    def close(self):
        self._stop.set()
        if self._ka_thread is not None and self._ka_thread is not threading.current_thread():
            self._ka_thread.join(timeout=TIMEOUT_MS / 1000.0 + 1)
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        if self.dev is not None:
            usb = self._usbmod
            try:
                usb.util.release_interface(self.dev, self.ifnum)
            except Exception:
                pass
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None
        self.is_open = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # -- Bulk-Only Transport -------------------------------------------------
    def _clear(self, ep):
        try:
            self.dev.clear_halt(ep)
        except Exception:
            pass

    def reset_recovery(self):
        """BOT reset recovery (USB MSC BOT 1.0, 5.3.4): class reset, then clear both halts."""
        try:
            self.dev.ctrl_transfer(0x21, 0xFF, 0, self.ifnum, None, TIMEOUT_MS)
        except Exception:
            pass
        self._clear(self.ep_in)
        self._clear(self.ep_out)

    def _read_csw(self):
        usb = self._usbmod
        for attempt in (1, 2):
            try:
                raw = bytes(self.ep_in.read(CSW_LEN, TIMEOUT_MS))
                break
            except usb.core.USBError as e:
                if attempt == 1 and _is(e, errno.EPIPE):
                    self._clear(self.ep_in)
                    continue
                raise
        if len(raw) != CSW_LEN or raw[:4] != CSW_SIG:
            self.reset_recovery()
            raise self.SCSIException("invalid CSW %s" % raw.hex())
        tag, residue, status = struct.unpack_from("<IIB", raw, 4)
        if tag != self.tag:
            self.reset_recovery()
            raise self.SCSIException("CSW tag %d, expected %d" % (tag, self.tag))
        return residue, status

    def _transfer(self, cdb, data_out, data_in):
        usb = self._usbmod
        self.tag = (self.tag + 1) & 0xFFFFFFFF or 1
        if data_out:
            length, flags = len(data_out), 0x00
        elif data_in is not None and len(data_in):
            length, flags = len(data_in), 0x80
        else:
            length, flags = 0, 0x00
        cbw = CBW_SIG + struct.pack("<IIBBB", self.tag, length, flags, 0, len(cdb)) + \
            bytes(cdb).ljust(16, b"\0")
        self.ep_out.write(cbw, TIMEOUT_MS)
        got = b""
        if length and flags == 0x00:
            try:
                self.ep_out.write(bytes(data_out), TIMEOUT_MS)
            except usb.core.USBError as e:
                if not _is(e, errno.EPIPE):
                    raise
                self._clear(self.ep_out)
        elif length:
            try:
                got = bytes(self.ep_in.read(length, TIMEOUT_MS))
            except usb.core.USBError as e:
                if not _is(e, errno.EPIPE):
                    raise
                self._clear(self.ep_in)
        residue, status = self._read_csw()
        self._last = time.monotonic()
        return got, residue, status

    def _sense(self):
        buf = bytearray(18)
        try:
            got, _, status = self._transfer(b"\x03\x00\x00\x00\x12\x00", None, buf)
            return got.hex(".") if status == 0 else ""
        except Exception:
            return ""

    def execute(self, cdb, data_out, data_in, max_sense_len=32, return_sense_buffer=False):
        if not self.is_open:
            raise self.SCSIException("device is closed")
        if data_out and data_in:
            raise NotImplementedError("bidirectional transfers are not supported")
        usb = self._usbmod
        with self._lock:
            return self._execute_locked(cdb, data_out, data_in, usb)

    def _execute_locked(self, cdb, data_out, data_in, usb):
        try:
            got, residue, status = self._transfer(cdb, data_out, data_in)
        except usb.core.USBError as e:
            self.reset_recovery()
            raise self.SCSIException("USB transfer failed: %s" % e)
        if status == 2:
            self.reset_recovery()
            raise self.SCSIException("phase error (CSW status 2)")
        if status != 0:
            sense = self._sense()
            raise self.SCSIException("command %s failed (CSW status %d)%s"
                                     % (bytes(cdb[:2]).hex(), status,
                                        "\nSense looks like this:\n" + sense if sense else ""))
        if data_in is not None and got:
            data_in[:len(got)] = got
        return 0


def wait_for_device(timeout=10.0, be=None, usb=None):
    """Poll until a WL82 UBOOT device is on USB, or `timeout` passes."""
    deadline = time.monotonic() + timeout
    while True:
        found = find(be, usb)
        if found or time.monotonic() > deadline:
            return found
        time.sleep(0.5)
