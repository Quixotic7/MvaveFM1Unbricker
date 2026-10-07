# SPDX-License-Identifier: MIT
"""Tests for fm1_unbrick.py. No device needed.

Run:  python3 -m pytest -q      or      python3 -m unittest -v

Optional inputs (tests skip cleanly without them):
  FM1_ORACLE_FWSC   a real FM-1 .fwsc package (default: the ChoralRoot build next door)
  FM1_ORACLE_TOOLS  a folder with an independent fm1_install.py used as a black-box oracle
  FM1_JLUB_DIR      a jl-uboot-tool checkout (default: ./jl-uboot-tool) for the protocol test
"""

import contextlib
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fm1_unbrick as fu  # noqa: E402

PROJECTS = ROOT.parent
ORACLE_FWSC = Path(os.environ.get(
    "FM1_ORACLE_FWSC", PROJECTS / "ChoralRootFM1" / "build" / "choralroot.fwsc"))
ORACLE_TOOLS = Path(os.environ.get(
    "FM1_ORACLE_TOOLS", PROJECTS / "ChoralRootFM1" / "tools"))
JLUB_DIR = Path(os.environ.get("FM1_JLUB_DIR", ROOT / "jl-uboot-tool"))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def make_fwsc(identity: str, image: bytes) -> bytes:
    """Build a package the way the format is described (inverse of parse_fwsc)."""
    assert len(identity) <= fu.FWSC_MARKED_BLOCKS
    out = bytearray()
    for i in range(fu.FWSC_MARKED_BLOCKS):
        out += image[i * fu.FWSC_BLOCK:(i + 1) * fu.FWSC_BLOCK]
        if i < len(identity):
            out.append((ord(identity[i]) + i + 1) & 0xFF)
        else:
            out.append(fu.FWSC_NO_CHAR)
    out += image[fu.FWSC_BLOCK * fu.FWSC_MARKED_BLOCKS:]
    return bytes(out)


def random_image(seed, length=0x94000):
    rnd = random.Random(seed)
    return rnd.randbytes(length)


class Workdir(unittest.TestCase):
    """Each test runs in its own temporary cwd (backups/ and logs/ land there)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._old = os.getcwd()
        os.chdir(str(self.tmp))
        self.out = io.StringIO()
        self._redir = contextlib.redirect_stdout(self.out)
        self._redir.__enter__()

    def tearDown(self):
        self._redir.__exit__(None, None, None)
        os.chdir(self._old)
        self._tmp.cleanup()

    def package(self, identity="FM-1_920", seed=1, name="pkg.fwsc"):
        image = random_image(seed)
        path = self.tmp / name
        path.write_bytes(make_fwsc(identity, image))
        return path, image

    def device(self, **kw):
        m = fu.MockUboot(**kw)
        return fu.Fm1Device("mock://test", m, io_max=m.io_max, mock=True), m


# --------------------------------------------------------------------------
# .fwsc
# --------------------------------------------------------------------------

class FwscTests(unittest.TestCase):

    def test_roundtrip_synthetic(self):
        image = random_image(7)
        ident, img = fu.parse_fwsc(make_fwsc("FM-1_015", image))
        self.assertEqual(ident, "FM-1_015")
        self.assertEqual(img, image)

    def test_no_char_markers_skipped(self):
        ident, _ = fu.parse_fwsc(make_fwsc("AB", random_image(3)))
        self.assertEqual(ident, "AB")

    def test_truncated_package_refused(self):
        with self.assertRaises(fu.UnbrickError):
            fu.parse_fwsc(b"\x00" * 100)
        short = make_fwsc("FM-1_920", random_image(2, 0x80000))
        with self.assertRaises(fu.UnbrickError):
            fu.Package(Path("short.fwsc"), short)

    def test_classification(self):
        self.assertEqual(fu.classify("FM-1_015", fu.V15_FW_SHA256)[0], "official-v15")
        self.assertEqual(fu.classify("FM-1_015", "0" * 64)[0], "damaged-v15")
        self.assertEqual(fu.classify("FM-1_920", "0" * 64)[0], "felucca-family")
        self.assertEqual(fu.classify("FM-1_014", "0" * 64)[0], "official-other")
        self.assertEqual(fu.classify("garbage", "0" * 64)[0], "unknown")

    @unittest.skipUnless(ORACLE_FWSC.is_file(), "oracle package not available")
    def test_real_package_layout(self):
        data = ORACLE_FWSC.read_bytes()
        pkg = fu.Package(ORACLE_FWSC, data)
        self.assertEqual(pkg.identity, "FM-1_920")
        self.assertEqual(len(pkg.firmware), 0x8F000)
        # Only the first 20 blocks carry a marker byte: the image is exactly 20 bytes shorter.
        self.assertEqual(len(data) - len(pkg.image), fu.FWSC_MARKED_BLOCKS)
        self.assertEqual(pkg.kind, "felucca-family")

    @unittest.skipUnless(ORACLE_FWSC.is_file() and (ORACLE_TOOLS / "fm1_install.py").is_file(),
                         "black-box oracle not available")
    def test_matches_black_box_oracle(self):
        code = ("import sys, hashlib; sys.path.insert(0, sys.argv[1]); import fm1_install as m; "
                "d = open(sys.argv[2], 'rb').read(); img = bytes(m.logical_image(d)); "
                "print(hashlib.sha256(img).hexdigest(), len(img), m.product_of(d))")
        res = subprocess.run([sys.executable, "-c", code, str(ORACLE_TOOLS), str(ORACLE_FWSC)],
                             capture_output=True, text=True, cwd=str(ORACLE_TOOLS))
        self.assertEqual(res.returncode, 0, res.stderr)
        oracle_sha, oracle_len, oracle_id = res.stdout.split()
        pkg = fu.Package.load(ORACLE_FWSC)
        self.assertEqual(hashlib.sha256(pkg.image).hexdigest(), oracle_sha)
        self.assertEqual(len(pkg.image), int(oracle_len))
        self.assertEqual(pkg.identity, oracle_id)


class ExtractCommandTests(Workdir):

    def test_extract(self):
        path, image = self.package()
        self.assertEqual(fu.main(["extract", str(path), "out.bin", "--log-dir", "logs"]), 0)
        self.assertEqual(Path("out.bin").read_bytes(), image[0x4000:0x93000])
        self.assertIn("FM-1_920", self.out.getvalue())
        self.assertTrue(list(Path("logs").glob("unbrick-*.log")))

    def test_extract_verify_v15_rejects_other(self):
        path, _ = self.package()
        self.assertEqual(fu.main(["extract", str(path), "out.bin", "--verify-v15"]), 1)
        self.assertFalse(Path("out.bin").exists())


# --------------------------------------------------------------------------
# MockUboot and the guarded flash access
# --------------------------------------------------------------------------

class MockAndGuardTests(Workdir):

    def test_nor_semantics(self):
        dev, m = self.device()
        fu.erase_range(dev, 0x5000, 0x1000)
        self.assertEqual(m.flash[0x5000:0x6000], b"\xff" * 0x1000)
        m.flash_write(0x5000, b"\x0f")
        m.flash_write(0x5000, b"\xf3")          # programming only clears bits
        self.assertEqual(m.flash[0x5000], 0x03)
        with self.assertRaises(AssertionError):
            m.flash_erase_chip()

    def test_erase_uses_blocks_and_sectors_exactly(self):
        dev, m = self.device()
        before = bytes(m.flash)
        fu.erase_range(dev, fu.FW_START, fu.FW_LEN)
        self.assertEqual(m.lowest_touched(), 0x4000)
        self.assertEqual(m.flash[:0x4000], before[:0x4000])
        self.assertEqual(m.flash[0x93000:], before[0x93000:])
        self.assertEqual(m.flash[0x4000:0x93000], b"\xff" * 0x8F000)
        kinds = {op[0] for op in m.ops}
        self.assertEqual(kinds, {"erase_sector", "erase_block"})

    def test_nothing_below_0x4000_by_construction(self):
        dev, m = self.device()
        for addr in (0, 0x1000, 0x3000, 0x3FFF):
            with self.assertRaises(fu.SafetyError):
                fu.erase_range(dev, addr & ~0xFFF, 0x1000)
            with self.assertRaises(fu.SafetyError):
                fu.write_range(dev, addr, b"\x00" * 16)
        with self.assertRaises(fu.SafetyError):
            fu.erase_range(dev, 0x4800, 0x1000)       # unaligned
        with self.assertRaises(fu.SafetyError):
            fu.write_range(dev, 0xFFF00, b"\x00" * 0x200)   # past the end
        self.assertEqual(m.ops, [])
        self.assertGreaterEqual(fu.FW_START, fu.PROTECTED_END)
        self.assertFalse(hasattr(fu, "erase_chip"))


class BackupTests(Workdir):

    def test_double_read_backup(self):
        dev, m = self.device()
        with self.assertLogs(fu.log, "INFO") as logs:
            path, data = fu.take_backup(dev, Path("bk"))
        self.assertEqual(path.read_bytes(), bytes(m.flash))
        self.assertTrue(path.name.startswith("fm1-flash-backup-"))
        self.assertIn(hashlib.sha256(m.flash).hexdigest(), "\n".join(logs.output))
        self.assertEqual(m.ops, [])

    def test_flaky_read_aborts_backup(self):
        dev, m = self.device(flaky_read=5)
        with self.assertRaises(fu.UnbrickError) as cm:
            fu.take_backup(dev, Path("bk"))
        self.assertIn("DIFFER", str(cm.exception))
        self.assertEqual(list(Path("bk").glob("*.bin")), [])

    def test_backup_command_reports_v15_identity_of_region(self):
        dev, m = self.device()
        path, image = self.package()
        m.flash[0x4000:0x93000] = image[0x4000:0x93000]
        rc = fu.main(["backup", "bk", "--package", str(path)], device=dev)
        self.assertEqual(rc, 0)
        self.assertIn("firmware sectors identical 143/143", self.out.getvalue())


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

class RestoreTests(Workdir):

    def check_restored(self, m, before, image):
        self.assertEqual(m.flash[:0x4000], before[:0x4000], "boot head changed")
        self.assertEqual(m.flash[0x4000:0x93000], image[0x4000:0x93000])
        self.assertEqual(m.flash[0x93000:], before[0x93000:], "data area changed")
        self.assertEqual(m.lowest_touched(), 0x4000)

    def test_restore_end_to_end(self):
        path, image = self.package()
        dev, m = self.device()
        before = bytes(m.flash)
        self.assertEqual(fu.main(["restore", str(path), "--yes"], device=dev), 0)
        self.check_restored(m, before, image)
        self.assertEqual(len(list(Path("backups").glob("fm1-flash-backup-*.bin"))), 1)
        self.assertEqual(Path(next(Path("backups").glob("*.bin"))).read_bytes(), before)
        log_text = next(Path("logs").glob("unbrick-*.log")).read_text()
        self.assertIn(hashlib.sha256(image[0x4000:0x93000]).hexdigest(), log_text)
        self.assertIn("byte-for-byte identical", log_text)

    def test_typed_confirmation(self):
        path, image = self.package()
        dev, m = self.device()
        with mock.patch.object(fu, "stdin_is_tty", return_value=True), \
                mock.patch.object(fu, "ask", return_value="no"):
            self.assertEqual(fu.main(["restore", str(path)], device=dev), 1)
        self.assertEqual(m.ops, [])
        dev, m = self.device()
        before = bytes(m.flash)
        with mock.patch.object(fu, "stdin_is_tty", return_value=True), \
                mock.patch.object(fu, "ask", return_value="WRITE"):
            self.assertEqual(fu.main(["restore", str(path)], device=dev), 0)
        self.check_restored(m, before, image)

    def test_no_tty_without_yes_refuses(self):
        path, _ = self.package()
        dev, m = self.device()
        with mock.patch.object(fu, "stdin_is_tty", return_value=False):
            self.assertEqual(fu.main(["restore", str(path)], device=dev), 1)
        self.assertEqual(m.ops, [])

    def test_flaky_write_is_retried_once(self):
        path, image = self.package()
        dev, m = self.device(flaky_write=10)
        before = bytes(m.flash)
        self.assertEqual(fu.main(["restore", str(path), "--yes"], device=dev), 0)
        self.check_restored(m, before, image)
        erases_at_start = [op for op in m.ops if op[1] == 0x4000 and op[0] == "erase_sector"]
        self.assertEqual(len(erases_at_start), 2)
        self.assertIn("Retrying the write once", self.out.getvalue())

    def test_persistent_failure_stops_with_instructions(self):
        path, _ = self.package()
        dev, m = self.device(bad_cell=0x50000)
        self.assertEqual(fu.main(["restore", str(path), "--yes"], device=dev), fu.EXIT_DANGER)
        text = self.out.getvalue()
        self.assertIn("Do NOT power off", text)
        self.assertEqual(text.count("Write attempt"), 2)
        self.assertNotIn("Write attempt 3", text)

    def test_flaky_read_during_backup_writes_nothing(self):
        path, _ = self.package()
        dev, m = self.device(flaky_read=3)
        self.assertEqual(fu.main(["restore", str(path), "--yes"], device=dev), 1)
        self.assertEqual(m.ops, [])

    def test_already_identical_skips_write(self):
        path, image = self.package()
        dev, m = self.device()
        m.flash[0x4000:0x93000] = image[0x4000:0x93000]
        self.assertEqual(fu.main(["restore", str(path), "--yes"], device=dev), 0)
        self.assertEqual(m.ops, [])
        self.assertIn("already holds exactly this firmware", self.out.getvalue())

    def test_wrong_chip_key_refused(self):
        path, _ = self.package()
        dev, m = self.device(chip_key=0x1234)
        self.assertEqual(fu.main(["restore", str(path), "--yes"], device=dev), 1)
        self.assertEqual(m.ops, [])
        self.assertIn("not 0x980F", self.out.getvalue())
        dev, m = self.device(chip_key=0x1234)
        self.assertEqual(fu.main(["info", "--i-know"], device=dev), 0)

    def test_wrong_flash_id_refused(self):
        dev, m = self.device(flash_id=0x856015)
        self.assertEqual(fu.main(["info"], device=dev), 1)

    def test_damaged_v15_refused_before_device(self):
        path, _ = self.package(identity="FM-1_015")
        dev, m = self.device()
        self.assertEqual(fu.main(["restore", str(path), "--yes"], device=dev), 1)
        self.assertEqual(m.reads, 0)
        self.assertIn("NOT match", self.out.getvalue())

    def test_unknown_identity_needs_i_know(self):
        path, image = self.package(identity="")
        dev, m = self.device()
        self.assertEqual(fu.main(["restore", str(path), "--yes"], device=dev), 1)
        self.assertEqual(m.reads, 0)
        dev, m = self.device()
        self.assertEqual(fu.main(["restore", str(path), "--yes", "--i-know"], device=dev), 0)

    def test_verify_v15_flag_refuses_others(self):
        path, _ = self.package()
        dev, m = self.device()
        self.assertEqual(fu.main(["restore", str(path), "--yes", "--verify-v15"], device=dev), 1)
        self.assertEqual(m.reads, 0)

    def test_cli_dry_run(self):
        path, image = self.package()
        self.assertEqual(fu.main(["restore", str(path), "--yes", "--dry-run"]), 0)
        self.assertTrue(list(Path("backups").glob("*-dryrun.bin")))
        self.assertIn("MOCK", self.out.getvalue())
        self.assertEqual(fu.main(["restore", str(path), "--yes", "--dry-run",
                                  "--mock-flaky", "write"]), 0)
        self.assertEqual(fu.main(["restore", str(path), "--yes", "--dry-run",
                                  "--mock-flaky", "write-always"]), fu.EXIT_DANGER)
        self.assertEqual(fu.main(["backup", "bk", "--dry-run", "--mock-flaky", "read"]), 1)
        self.assertEqual(fu.main(["info", "--dry-run"]), 0)
        self.assertEqual(fu.main(["find", "--dry-run"]), 0)


# --------------------------------------------------------------------------
# device discovery and selection
# --------------------------------------------------------------------------

WIN_JSON_TWO_DISKS = json.dumps([
    {"Index": 0, "Model": "Samsung SSD 980 PRO 1TB", "InterfaceType": "SCSI",
     "PNPDeviceID": "SCSI\\DISK&VEN_NVME", "Size": 1000202273280},
    {"Index": 5, "Model": "WL82 UBOOT1.00 USB Device", "InterfaceType": "USB",
     "PNPDeviceID": "USBSTOR\\DISK&VEN_WL82&PROD_UBOOT1.00&REV_1.00\\7&1", "Size": None},
    {"Index": 3, "Model": "SanDisk Ultra USB Device", "InterfaceType": "USB",
     "PNPDeviceID": "USBSTOR\\DISK&VEN_SANDISK", "Size": 30752636928},
])
WIN_JSON_ONE = json.dumps({"Index": 2, "Model": "WL82 UBOOT1.00 USB Device",
                           "InterfaceType": "USB", "PNPDeviceID": "x", "Size": None})


class FinderTests(Workdir):

    def test_windows_parser(self):
        found = fu.parse_windows_disks(WIN_JSON_TWO_DISKS)
        self.assertEqual([f["path"] for f in found], ["\\\\.\\PhysicalDrive5"])
        self.assertEqual([f["path"] for f in fu.parse_windows_disks(WIN_JSON_ONE)],
                         ["\\\\.\\PhysicalDrive2"])
        self.assertEqual(fu.parse_windows_disks(""), [])

    def test_windows_runner(self):
        fake = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout=WIN_JSON_ONE,
                                                            stderr=""))
        self.assertEqual(len(fu.find_windows(run=fake)), 1)
        self.assertIn("Win32_DiskDrive", fake.call_args[0][0][-1])

    def make_sysfs(self, entries):
        """entries: name -> (vendor, model, usb)"""
        sysroot = self.tmp / "sys"
        for name, (vendor, model, usb) in entries.items():
            real = sysroot / "devices" / ("pci0/usb1/1-1" if usb else "pci0/ata1") / name
            real.mkdir(parents=True)
            (real / "vendor").write_text(vendor.ljust(8) + "\n")
            (real / "model").write_text(model.ljust(16) + "\n")
            sg = sysroot / "class" / "scsi_generic" / name
            sg.mkdir(parents=True)
            (sg / "device").symlink_to(real)
        return sysroot

    def test_linux_sysfs(self):
        sysroot = self.make_sysfs({
            "sg0": ("ATA", "Samsung SSD 870", False),
            "sg1": ("WL82", "UBOOT1.00", True),
            "sg2": ("SanDisk", "Ultra", True),
            "sg3": ("WL82", "UBOOT1.00", False),     # not on USB: ignored
        })
        found = fu.scan_linux_sysfs(str(sysroot), "/dev")
        self.assertEqual([f["path"] for f in found], ["/dev/sg1"])
        self.assertEqual(fu.scan_linux_sysfs(str(self.tmp / "nothing")), [])

    def test_two_devices_refused(self):
        sysroot = self.make_sysfs({"sg1": ("WL82", "UBOOT1.00", True),
                                   "sg4": ("WL82", "UBOOT1.00", True)})
        found = fu.scan_linux_sysfs(str(sysroot), "/dev")
        self.assertEqual(len(found), 2)
        with self.assertRaises(fu.SafetyError):
            fu.select_device(None, found)

    def test_selection_rules(self):
        one = [{"path": "/dev/sg1", "description": "x"}]
        self.assertEqual(fu.select_device(None, one), "/dev/sg1")
        with self.assertRaises(fu.UnbrickError):
            fu.select_device(None, [])
        with mock.patch.object(fu.sys, "platform", "linux"):
            self.assertEqual(fu.select_device("/dev/sg1", one), "/dev/sg1")
            with self.assertRaises(fu.SafetyError):
                fu.select_device("/dev/sda", one)         # never a block disk
            with self.assertRaises(fu.SafetyError):
                fu.select_device("/dev/sg0", one)         # not the scanned device
            self.assertEqual(fu.select_device("/dev/sg0", one, i_know=True), "/dev/sg0")
        with mock.patch.object(fu.sys, "platform", "win32"):
            with self.assertRaises(fu.SafetyError):
                fu.select_device("C:", [])

    def test_macos_unsupported(self):
        with mock.patch.object(fu.sys, "platform", "darwin"):
            for cmd in (["find"], ["info"], ["backup", "bk"]):
                self.assertEqual(fu.main(cmd), 2)
            self.assertIn("not supported: jl-uboot-tool has no macOS SCSI back end",
                          self.out.getvalue())
            self.assertEqual(fu.main(["info", "--dry-run"]), 0)


# --------------------------------------------------------------------------
# Protocol level: jl-uboot-tool's real classes over a mock SCSI device
# --------------------------------------------------------------------------

def _load_jl_or_skip():
    if not (JLUB_DIR / "jltech" / "uboot.py").is_file():
        raise unittest.SkipTest("jl-uboot-tool checkout not available (set FM1_JLUB_DIR)")
    try:
        import crcmod  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("jl-uboot-tool requirements not installed")
    if "scsiio" not in sys.modules and not fu.platform_supported():
        # scsiio refuses to import on macOS; stub the package so jltech can load.
        stub = types.ModuleType("scsiio")
        stub.__path__ = [str(JLUB_DIR / "scsiio")]
        stub.SCSIDev = None
        sys.modules["scsiio"] = stub
    return fu.load_jlub(JLUB_DIR)


class MockScsi:
    """Speaks UBOOT1.00 and the wl82 loader protocol at the CDB level."""

    LOADER_ADDR = 0x1C02000

    def __init__(self, jl, uboot: fu.MockUboot, blob: bytes):
        self.jl, self.m, self.blob = jl, uboot, blob
        self.ram = {}
        self.loader_running = False
        self.jump_arg = None
        self.closed = False

    def close(self):
        self.closed = True

    def _resp(self, data_in, cmd, payload=b""):
        buf = cmd.to_bytes(2, "big") + payload
        data_in[:len(buf)] = buf

    def execute(self, cdb, data_out, data_in):
        from jltech.crc import jl_crc16
        if cdb[0] == 0x12:
            inq = bytearray(36)
            inq[8:16] = b"WL82    "
            inq[16:32] = b"UBOOT1.00       "
            inq[32:36] = b"1.00"
            data_in[:] = inq
            return 0
        cmd = int.from_bytes(cdb[0:2], "big")
        addr = int.from_bytes(cdb[2:6], "big")
        ln = int.from_bytes(cdb[6:8], "big")
        if not self.loader_running:
            if cmd == 0xFB06:
                assert len(data_out) == ln
                assert jl_crc16(data_out) == int.from_bytes(cdb[9:11], "little")
                self.ram[addr] = bytes(data_out)
                return 0
            if cmd == 0xFB08:
                image = b"".join(self.ram[a] for a in sorted(self.ram))
                assert min(self.ram) == addr == self.LOADER_ADDR
                assert image == self.blob, "loader upload differs from the blob"
                self.jump_arg = ln
                self.loader_running = True
                self._resp(data_in, cmd)
                return 0
            raise self.jl.SCSIException("UBOOT: unsupported command %04X" % cmd)
        if cmd == 0xFC09:
            enc = self.jl.cipher_bytes(self.jl.jl_crc_cipher, self.m.key.to_bytes(2, "little"))
            self._resp(data_in, cmd, b"\0\0\0\0" + enc[::-1])
        elif cmd == 0xFC0A:
            self._resp(data_in, cmd, bytes([3, 0]) + self.m.id.to_bytes(4, "little"))
        elif cmd == 0xFC14:
            self._resp(data_in, cmd, self.m.io_max.to_bytes(4, "big"))
        elif cmd == 0xFD05:
            data_in[:] = self.m.flash_read(addr, ln)
        elif cmd == 0xFB04:
            assert len(data_out) == ln
            assert jl_crc16(data_out) == int.from_bytes(cdb[9:11], "little")
            self.m.flash_write(addr, data_out)
        elif cmd == 0xFB01:
            self.m.flash_erase_sector(addr)
            self._resp(data_in, cmd)
        elif cmd == 0xFB00:
            self.m.flash_erase_block(addr)
            self._resp(data_in, cmd)
        elif cmd == 0xFB02:
            self.m.flash_erase_chip()
        else:
            raise self.jl.SCSIException("loader: unsupported command %04X" % cmd)
        return 0


class ProtocolTests(Workdir):

    def setUp(self):
        super().setUp()
        self.jl = _load_jl_or_skip()
        self.blob = (JLUB_DIR / fu.WL82_LOADER_REL).read_bytes()

    def test_open_upload_identify_restore(self):
        m = fu.MockUboot()
        scsi = MockScsi(self.jl, m, self.blob)
        dev = fu.open_real_device("/dev/sg9", self.jl, scsi=scsi)
        self.assertTrue(scsi.loader_running)
        self.assertEqual(scsi.jump_arg, 1)
        self.assertEqual(dev.chip_key(), 0x980F)
        self.assertEqual(dev.flash_id(), 0x856014)
        self.assertEqual(dev.io_max, 512)
        path, image = self.package()
        before = bytes(m.flash)
        self.assertEqual(fu.main(["restore", str(path), "--yes"], device=dev), 0)
        self.assertEqual(m.flash[:0x4000], before[:0x4000])
        self.assertEqual(m.flash[0x4000:0x93000], image[0x4000:0x93000])
        self.assertEqual(m.flash[0x93000:], before[0x93000:])
        self.assertEqual(m.lowest_touched(), 0x4000)
        self.assertTrue(scsi.closed)

    def test_non_wl82_device_untouched(self):
        m = fu.MockUboot()
        scsi = MockScsi(self.jl, m, self.blob)
        real_exec = scsi.execute

        def other_vendor(cdb, out, din):
            real_exec(cdb, out, din)
            if cdb[0] == 0x12:
                din[8:16] = b"Samsung "
            return 0
        scsi.execute = other_vendor
        with self.assertRaises(fu.SafetyError):
            fu.open_real_device("/dev/sg9", self.jl, scsi=scsi)
        self.assertFalse(scsi.loader_running)
        self.assertEqual(scsi.ram, {})
        self.assertTrue(scsi.closed)

    def test_verify_pinned_checkout(self):
        commit = fu.jlub_commit(JLUB_DIR)
        if commit is None:
            self.skipTest("checkout has no commit information")
        self.assertEqual(commit, fu.JLUB_COMMIT)
        self.assertEqual(fu.verify_jlub(JLUB_DIR), fu.JLUB_COMMIT)


if __name__ == "__main__":
    unittest.main()
