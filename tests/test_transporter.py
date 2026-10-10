# SPDX-License-Identifier: MIT
"""Tests for fm1_transporter_recover.py. No hardware: a MockTransporter speaks
the Transporter's line protocol over an in-memory 1 MiB NOR flash.

Run:  python3 -m pytest -q      or      python3 -m unittest -v

Optional inputs (tests skip cleanly without them):
  FM1_TRANSPORTER_DUMP  the real 2026-10-09 dump (default: ../ChoralRootFM1/recovery-20261009/backup1.bin)
  FM1_V15_FWSC          the official FM-1.fwsc (default: ../MVaveOfficial/V15-FM-1.fwsc)
"""

import os
import random
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fm1_unbrick as fu  # noqa: E402
import fm1_transporter_recover as tr  # noqa: E402
from tests.test_unbrick import Workdir, make_fwsc, make_ufw, random_image  # noqa: E402

PROJECTS = ROOT.parent
REAL_DUMP = Path(os.environ.get(
    "FM1_TRANSPORTER_DUMP", PROJECTS / "ChoralRootFM1" / "recovery-20261009" / "backup1.bin"))
V15_FWSC = Path(os.environ.get("FM1_V15_FWSC", PROJECTS / "MVaveOfficial" / "V15-FM-1.fwsc"))

SECTOR = 0x1000


# --------------------------------------------------------------------------
# synthetic flash content
# --------------------------------------------------------------------------

def loader_area(area=0xE0000, body_len=0x1A2D, marker=True):
    """A staged loader: 32-byte outer header + body (inner header with the load address)."""
    rnd = random.Random(area)
    body = bytearray(rnd.randbytes(body_len))
    body[-1] = 0x00                                      # body does not end in 0xFF
    struct.pack_into("<I", body, 8, tr.LOADER_LOAD_ADDR)
    body[0x10:0x20] = b"usb_hid_ota.bin\0"
    if marker:
        body[0x2C:0x2C + 16] = tr.LOADER_MARKER          # outer offset 0x4C
    else:
        body[0x2C:0x2C + 16] = b"X" * 16
    hdr = bytearray(b"\xff" * 32)
    struct.pack_into("<HII", hdr, 2, 0xFFFF, area, body_len)
    hdr[12] = 0x41
    hdr[16:32] = b"LOADER.BIN".ljust(16, b"\0")
    struct.pack_into("<H", hdr, 0, fu._crc16(hdr[2:32]))
    return bytes(hdr + body)


def record(identity=b"ota-FM-1_015", area=0xE0000):
    rec = bytearray(112)
    struct.pack_into("<HHH", rec, 2, 0x5A0D, 0x5A01, 0x5441)
    rec[8:8 + len(identity)] = identity
    struct.pack_into("<I", rec, 72, area)
    struct.pack_into("<H", rec, 0, fu._crc16(rec[2:80]))
    return bytes(rec)


def data_area(with_loader=True, with_record=True):
    """0x93000..0xFFFFF: a few FELU objects, a loader at 0xE0000, a record at 0xE4F00."""
    d = bytearray(b"\xff" * (fu.FLASH_SIZE - fu.FW_END))
    base = fu.FW_END

    def put(addr, blob):
        d[addr - base:addr - base + len(blob)] = blob
    put(0x97000, b"FELU" + struct.pack("<III", 1, 1, 0))
    put(0x99000, b"FELU" + struct.pack("<III", 2, 5, 0x810) + bytes(0x810))
    if with_loader:
        put(0xE0000, loader_area())
    if with_record:
        put(0xE4F00, record())
    return bytes(d)


def flash_from(head, app, data):
    out = bytes(head) + bytes(app) + bytes(data)
    assert len(out) == fu.FLASH_SIZE
    return out


class Base(Workdir):

    def pkg(self, name, seed, identity="FM-1_920"):
        path, image = self.package(identity=identity, seed=seed, name=name)
        return path, image

    def transporter(self, **kw):
        m = tr.MockTransporter(**kw)
        return tr.Transporter(m, m.port, mock=True), m

    def run_main(self, argv, t=None):
        return tr.main(["--log-dir", "logs", "--wait", "2"] + argv, transporter=t)


# --------------------------------------------------------------------------
# mock protocol
# --------------------------------------------------------------------------

class MockProtocolTests(Base):

    def test_line_protocol(self):
        t, m = self.transporter()
        self.assertTrue(t.ping())
        self.assertEqual(t.status(), {"uboot": "1", "v15": "0", "loader": "1",
                                      "loader_running": "0"})
        self.assertEqual(t.info(), "OK key=980F type=3 id=856014")
        data = t.read_flash(0x1000, 0x2000)
        self.assertEqual(data, bytes(m.flash[0x1000:0x3000]))
        self.assertEqual(t.request("bogus"), "ERR unknown")

    def test_wsec_nor_semantics_and_gate(self):
        t, m = self.transporter()
        sec = bytes(range(256)) * 16
        self.assertEqual(t.write_sector(0x5000, sec), "OK")
        self.assertEqual(bytes(m.flash[0x5000:0x6000]), sec)
        for bad in (0x0, 0x3000, 0x93000, 0x5800):
            with self.assertRaises(fu.SafetyError):
                t.write_sector(bad, sec)
        # the mock refuses like the firmware, even when asked directly
        m.write(b"wsec 0x3000 %08X\n" % tr.crc32(sec) + sec)
        self.assertEqual(m.readline(), b"ERR range\n")
        m.write(b"wsec 0x6000 00000000\n" + sec)
        self.assertTrue(m.readline().startswith(b"ERR crc"))
        self.assertEqual(m.writes, [0x5000])

    def test_absent_fm1_only_pings(self):
        t, m = self.transporter(present=False)
        self.assertTrue(t.ping())
        with mock.patch.object(tr, "HINT_EVERY", 0.2):
            rc = self.run_main(["--wait", "0.5", "status"], t)
        self.assertEqual(rc, 1)
        text = self.out.getvalue()
        self.assertIn("has not answered the USB_KEY yet", text)
        self.assertIn("try rekey", text)
        self.assertEqual(m.requests.count("status"), 1)     # one request, never repeated

    def test_status_and_info_commands(self):
        t, m = self.transporter()
        self.assertEqual(self.run_main(["status"], t), 0)
        self.assertIn("uboot=1", self.out.getvalue())
        self.assertEqual(self.run_main(["info"], t), 0)
        self.assertTrue(list(Path("logs").glob("transporter-*.log")))

    def test_wrong_chip_key_info(self):
        t, m = self.transporter(chip_key=0x1234)
        self.assertEqual(self.run_main(["info"], t), 1)
        self.assertIn("chip key 1234 is not 980F", self.out.getvalue())

    def test_wrong_flash_id_info(self):
        t, m = self.transporter(flash_id=0x856015)
        self.assertEqual(self.run_main(["info"], t), 1)
        self.assertIn("flash id 856015 is not 856014", self.out.getvalue())


# --------------------------------------------------------------------------
# dump / restore / recover end to end
# --------------------------------------------------------------------------

class EndToEndTests(Base):

    def setUp(self):
        super().setUp()
        self.path, self.image = self.pkg("newfw.fwsc", 1)
        old = random_image(99)
        # the FM-1: same head as the package, an old app, device data
        self.before = flash_from(self.image[:0x4000], old[0x4000:0x93000], data_area())
        self.expected = self.before[:0x4000] + self.image[0x4000:0x93000] + self.before[0x93000:]

    def dump(self, t):
        self.assertEqual(self.run_main(["dump", "bk", "--package", str(self.path)], t), 0)
        dumps = sorted(Path("bk").glob("fm1-transporter-dump-*.bin"))
        self.assertEqual(len(dumps), 1)
        return dumps[0]

    def test_dump_analyse_restore(self):
        t, m = self.transporter(flash=self.before)
        ref = self.dump(t)
        self.assertEqual(m.reads, 2)                                  # read twice
        self.assertEqual(ref.read_bytes(), self.before)
        self.assertTrue(ref.with_suffix(".txt").is_file())
        self.assertIn("crc32 %08X" % tr.crc32(self.before), self.out.getvalue())

        self.assertEqual(self.run_main(["analyse", str(ref), "--package", str(self.path)]), 0)
        self.assertIn("record 0xE4F00", self.out.getvalue())

        # dry run: nothing written
        self.assertEqual(self.run_main(["restore", str(self.path), "--ref", str(ref)], t), 0)
        self.assertEqual(m.writes, [])
        self.assertIn("DRY RUN: nothing written (add --write to write 143 sectors)",
                      self.out.getvalue())

        self.assertEqual(self.run_main(["restore", str(self.path), "--ref", str(ref),
                                        "--write", "--yes"], t), 0)
        self.assertEqual(len(m.writes), 143)
        self.assertEqual(m.lowest_written(), 0x4000)
        self.assertEqual(bytes(m.flash), self.expected)
        text = self.out.getvalue()
        self.assertIn("Final full read EQUALS the expected image", text)
        self.assertIn("Unplug the three Transporter wires", text)

    def test_only_differing_sectors_written(self):
        before = bytearray(self.expected)
        before[0x10000:0x11000] = b"\x00" * SECTOR
        before[0x80000:0x80010] = b"\x01" * 16
        t, m = self.transporter(flash=bytes(before))
        ref = self.dump(t)
        self.assertEqual(self.run_main(["restore", str(self.path), "--ref", str(ref),
                                        "--write", "--yes"], t), 0)
        self.assertEqual(m.writes, [0x10000, 0x80000])
        self.assertEqual(bytes(m.flash), self.expected)

    def test_recover_flow(self):
        t, m = self.transporter(flash=self.before)
        self.assertEqual(self.run_main(["recover", str(self.path), "--out", "rec", "--yes"], t), 0)
        self.assertEqual(bytes(m.flash), self.expected)
        self.assertEqual(len(m.writes), 143)
        self.assertEqual(len(list(Path("rec").glob("*.bin"))), 1)
        self.assertEqual(len(list(Path("rec").glob("*.txt"))), 1)
        self.assertIn("Final full read EQUALS", self.out.getvalue())

    def test_cli_dry_run_with_mock_flash(self):
        Path("seed.bin").write_bytes(self.before)
        rc = tr.main(["--dry-run", "--mock-flash", "seed.bin", "recover", str(self.path),
                      "--out", "rehearsal", "--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(Path("seed.bin").read_bytes(), self.before)   # seed untouched
        self.assertTrue(list(Path("rehearsal").glob("*-dryrun.bin")))
        self.assertIn("MOCK", self.out.getvalue())
        # options after the subcommand work too
        self.assertEqual(tr.main(["info", "--dry-run"]), 0)

    def test_typed_confirmation(self):
        t, m = self.transporter(flash=self.before)
        ref = self.dump(t)
        argv = ["restore", str(self.path), "--ref", str(ref), "--write"]
        with mock.patch.object(fu, "stdin_is_tty", return_value=True), \
                mock.patch.object(fu, "ask", return_value="no"):
            self.assertEqual(self.run_main(argv, t), 1)
        self.assertEqual(m.writes, [])
        with mock.patch.object(fu, "stdin_is_tty", return_value=False):
            self.assertEqual(self.run_main(argv, t), 1)
        self.assertEqual(m.writes, [])
        with mock.patch.object(fu, "stdin_is_tty", return_value=True), \
                mock.patch.object(fu, "ask", return_value="WRITE"):
            self.assertEqual(self.run_main(argv, t), 0)
        self.assertEqual(bytes(m.flash), self.expected)

    # refusals ------------------------------------------------------------

    def test_ref_mismatch_writes_nothing(self):
        t, m = self.transporter(flash=self.before)
        ref = self.dump(t)
        m.flash[0x50000] ^= 0xFF                      # the flash changed since --ref
        rc = self.run_main(["restore", str(self.path), "--ref", str(ref), "--write", "--yes"], t)
        self.assertEqual(rc, 1)
        self.assertEqual(m.writes, [])
        self.assertIn("differs from --ref in package-region sectors 0x50000", self.out.getvalue())

    def test_head_difference_reported_never_written(self):
        other_head = random_image(5, 0x4000)
        before = flash_from(other_head, self.before[0x4000:0x93000], self.before[0x93000:])
        t, m = self.transporter(flash=before)
        ref = self.dump(t)
        rc = self.run_main(["restore", str(self.path), "--ref", str(ref), "--write", "--yes"], t)
        self.assertEqual(rc, 0)
        text = self.out.getvalue()
        self.assertIn("head differs from the device's in sectors 0x00000 0x01000 0x02000 0x03000",
                      text)
        self.assertIn("NEVER written", text)
        self.assertEqual(bytes(m.flash[:0x4000]), other_head)
        self.assertEqual(m.lowest_written(), 0x4000)
        self.assertEqual(bytes(m.flash[0x4000:0x93000]), self.image[0x4000:0x93000])
        self.assertEqual(bytes(m.flash[0x93000:]), before[0x93000:])

    def test_wrong_chip_key_refused(self):
        Path("ref.bin").write_bytes(self.before)
        t, m = self.transporter(flash=self.before, chip_key=0x1234)
        rc = self.run_main(["restore", str(self.path), "--ref", "ref.bin", "--write", "--yes"], t)
        self.assertEqual(rc, 1)
        self.assertEqual(m.writes, [])
        self.assertEqual(m.reads, 0)
        self.assertIn("not 980F", self.out.getvalue())
        self.assertEqual(self.run_main(["recover", str(self.path), "--yes"], t), 1)
        self.assertEqual(m.reads, 0)

    def test_wsec_error_is_exit_3(self):
        t, m = self.transporter(flash=self.before, wsec_errors={0x6000: "ERR write"})
        ref = self.dump(t)
        rc = self.run_main(["restore", str(self.path), "--ref", str(ref), "--write", "--yes"], t)
        self.assertEqual(rc, tr.EXIT_DANGER)
        text = self.out.getvalue()
        self.assertIn("Sector 0x06000: ERR write", text)
        self.assertIn("Do NOT switch the FM-1 off", text)
        self.assertEqual(m.writes, [0x4000, 0x5000])            # stopped at the failure
        self.assertNotIn("Final full read EQUALS", text)

    def test_unstable_reads_abort_dump(self):
        t, m = self.transporter(flash=self.before, flaky_read=2)
        self.assertEqual(self.run_main(["dump", "bk"], t), 1)
        self.assertIn("the two reads DIFFER", self.out.getvalue())
        self.assertEqual(list(Path("bk").glob("*.bin")), [])

    def test_verify_v15_refuses_others(self):
        Path("ref.bin").write_bytes(self.before)
        t, m = self.transporter(flash=self.before)
        rc = self.run_main(["restore", str(self.path), "--ref", "ref.bin", "--verify-v15"], t)
        self.assertEqual(rc, 1)
        self.assertEqual(m.reads, 0)

    def test_ref_must_be_full_dump(self):
        Path("ref.bin").write_bytes(self.before[:0x80000])
        t, m = self.transporter(flash=self.before)
        self.assertEqual(self.run_main(["restore", str(self.path), "--ref", "ref.bin"], t), 1)
        self.assertEqual(m.reads, 0)


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

class AnalysisTests(Base):

    def setUp(self):
        super().setUp()
        self.pa, self.ia = self.pkg("A.fwsc", 11)
        self.pb, self.ib = self.pkg("B.fwsc", 22)
        self.pv, self.iv = self.pkg("V15fake.fwsc", 33, identity="FM-1_014")
        app = self.ib[0x4000:0x25000] + self.ia[0x25000:0x93000]
        self.dump = bytearray(flash_from(self.iv[:0x4000], app, data_area()))

    def analyse(self, data):
        labelled, v15 = tr.load_labelled(str(self.pv), [str(self.pa), str(self.pb)])
        return tr.analyse_dump(bytes(data), labelled, v15)

    def test_two_package_mix(self):
        rep = self.analyse(self.dump)
        text = rep.text
        self.assertEqual([(s, e, v) for s, e, v in rep.runs],
                         [(0x4000, 0x25000, "B"), (0x25000, 0x93000, "A")])
        self.assertIn("0x04000..0x24FFF   33 sectors  B", text)
        self.assertIn("0x25000..0x92FFF  110 sectors  A", text)
        self.assertIn("last sector equal to B: 0x24000", text)
        self.assertIn("last sector equal to A: 0x92000", text)
        self.assertIn("no torn sector", text)
        self.assertEqual(rep.torn, [])
        self.assertIn("head == V15", text)
        self.assertTrue(rep.head_equals_v15)
        self.assertIn("0xE4F00  CRC valid, identity 'ota-FM-1_015', area 0xE0000  => valid", text)
        self.assertIn("=> loader intact", text)
        self.assertIn("marker FELUCCA-LOADER-1 present", text)
        self.assertIn("inner load address 0x01C0A800", text)
        self.assertEqual(rep.verdicts, ["resume state intact (loader + record valid)"])
        self.assertTrue(rep.mixed)
        self.assertIn("app is a MIX", text)
        self.assertIn("FELU object at 0x99000: type 0x2, seq 5, len 0x810", text)
        self.assertIn("erased 0x93000..0x96FFF", text)
        # the analyse command prints the same report
        Path("d.bin").write_bytes(bytes(self.dump))
        self.assertEqual(tr.main(["analyse", "d.bin", "--v15", str(self.pv), "--package",
                                  str(self.pa), "--package", str(self.pb)]), 0)
        self.assertIn("0x04000..0x24FFF   33 sectors  B", self.out.getvalue())

    def test_torn_sector(self):
        d = self.dump
        d[0x30000:0x31000] = self.ia[0x30000:0x30C00] + b"\xff" * 0x400   # cut at a page
        d[0x40000:0x41000] = b"\xff" * SECTOR
        rep = self.analyse(d)
        self.assertEqual(rep.torn, [0x30000])
        self.assertIn("TORN sectors (programmed up to a page boundary, then >= 256 bytes of "
                      "0xFF): 0x30000", rep.text)
        self.assertIn("0x30000..0x30FFF    1 sector  TORN", rep.text)
        self.assertIn("0x40000..0x40FFF    1 sector  ERASED", rep.text)
        self.assertIn("1 TORN sector(s): 0x30000", rep.text)

    def test_padding_is_not_torn(self):
        d = self.dump
        d[0x92000:0x93000] = b"\x12" * 0x52A + b"\xff" * (SECTOR - 0x52A)
        rep = self.analyse(d)
        self.assertEqual(rep.torn, [])
        self.assertIn("0x92000 ends in 0xFF from +0x52A", rep.text)

    def test_no_record_and_damaged_loader(self):
        d = bytearray(self.dump)
        d[0xE4F00:0xE4F70] = b"\xff" * 112
        rep = self.analyse(d)
        self.assertEqual(rep.verdicts, ["no record"])
        d = bytearray(self.dump)
        d[0xE0000 + 0x4C:0xE0000 + 0x5C] = b"X" * 16        # marker gone, header CRC still ok
        rep = self.analyse(d)
        self.assertEqual(rep.verdicts, ["loader damaged"])
        self.assertIn("marker FELUCCA-LOADER-1 missing", rep.text)
        d = bytearray(self.dump)
        d[0xE4F00 + 9] ^= 1                                 # record CRC broken
        rep = self.analyse(d)
        self.assertEqual(rep.verdicts, ["no record"])
        self.assertIn("CRC INVALID", rep.text)

    def test_head_differs(self):
        d = self.dump
        d[0x1000] ^= 0xFF
        rep = self.analyse(d)
        self.assertFalse(rep.head_equals_v15)
        self.assertIn("head DIFFERS from V15 in sectors 0x01000", rep.text)

    @unittest.skipUnless(REAL_DUMP.is_file() and V15_FWSC.is_file(), "real dump not available")
    def test_real_dump(self):
        labelled, v15 = tr.load_labelled(str(V15_FWSC), [])
        rep = tr.analyse_dump(REAL_DUMP.read_bytes(), labelled, v15)
        self.assertTrue(rep.head_equals_v15)
        self.assertEqual(rep.torn, [])
        self.assertEqual(rep.verdicts, ["resume state intact (loader + record valid)"])
        self.assertEqual([(r["addr"], r["identity"], r["area"], r["valid"]) for r in rep.records],
                         [(0xE4F00, "ota-FM-1_015", 0xE0000, True)])


# --------------------------------------------------------------------------
# flash-xiao (no device: validation and dry run only)
# --------------------------------------------------------------------------

def uf2_blocks(n=2):
    out = bytearray()
    for i in range(n):
        blk = bytearray(512)
        struct.pack_into("<4sIIIIIII", blk, 0, b"UF2\n", 0x9E5D5157, 0x2000, 0x10000000 + i * 256,
                         256, i, n, 0xE48BFF56)
        struct.pack_into("<I", blk, 508, 0x0AB16F30)
        out += blk
    return bytes(out)


class FlashXiaoTests(Base):

    def test_refuses_non_uf2(self):
        Path("fw.bin").write_bytes(uf2_blocks())
        self.assertEqual(tr.main(["--dry-run", "flash-xiao", "fw.bin"]), 1)
        bad = bytearray(uf2_blocks())
        bad[4] ^= 1
        Path("bad.uf2").write_bytes(bytes(bad))
        self.assertEqual(tr.main(["--dry-run", "flash-xiao", "bad.uf2"]), 1)
        self.assertIn("UF2 magic", self.out.getvalue())

    def test_dry_run_accepts_uf2(self):
        Path("fm1-transporter.uf2").write_bytes(uf2_blocks())
        with mock.patch.object(tr, "find_rp2_volumes", side_effect=AssertionError("scanned")):
            self.assertEqual(tr.main(["--dry-run", "flash-xiao", "fm1-transporter.uf2"]), 0)
        self.assertIn("DRY RUN: would find RPI-RP2", self.out.getvalue())

    def test_copy_to_mounted_volume(self):
        vol = Path("RPI-RP2")
        vol.mkdir()
        (vol / "INFO_UF2.TXT").write_text("UF2 Bootloader v3.0\nBoard-ID: RPI-RP2\n")
        Path("fm1-transporter.uf2").write_bytes(uf2_blocks())
        t, m = self.transporter()

        def copy_and_reboot(src, dst):
            Path(dst).write_bytes(Path(src).read_bytes())
            (vol / "INFO_UF2.TXT").unlink()            # the board reboots
        with mock.patch.object(tr, "find_rp2_volumes", return_value=[vol]), \
                mock.patch.object(tr.shutil, "copyfile", side_effect=copy_and_reboot), \
                mock.patch.object(tr, "probe_port", return_value=t), \
                mock.patch.object(tr, "rp2_ports", return_value=[]), \
                mock.patch.object(tr, "touch_1200", side_effect=AssertionError("touched")):
            self.assertEqual(tr.main(["flash-xiao", "fm1-transporter.uf2"]), 0)
        self.assertEqual((vol / "fm1-transporter.uf2").read_bytes(), uf2_blocks())
        self.assertIn("Transporter data port: mock://fm1-transporter", self.out.getvalue())

    def test_real_placeholder_passes_check_uf2(self):
        data = tr.check_uf2(tr.PLACEHOLDER_UF2)
        self.assertEqual(len(tr.parse_uf2(data)), 412)


# --------------------------------------------------------------------------
# splice_uf2, setup
# --------------------------------------------------------------------------

JL_LOADER = Path(os.environ.get("FM1_JL_DIR", PROJECTS / "jl-uboot-tool")) / fu.WL82_LOADER_REL


def synthetic_uf2(payloads, base=0x10000000, flags=0x2000, family=0xE48BFF56):
    """A UF2 with the given payloads at consecutive addresses; padding is not zero
    so a splice that loses it is caught."""
    out, addr, n = bytearray(), base, len(payloads)
    for i, pl in enumerate(payloads):
        blk = bytearray(bytes([0xA0 + i]) * 512)
        struct.pack_into("<4sIIIIIII", blk, 0, b"UF2\n", 0x9E5D5157, flags, addr, len(pl), i, n,
                         family)
        blk[32:32 + len(pl)] = pl
        struct.pack_into("<I", blk, 508, 0x0AB16F30)
        out += blk
        addr += len(pl)
    return bytes(out)


class SpliceTests(unittest.TestCase):

    def setUp(self):
        rnd = random.Random(7)
        self.pattern = b"PLACEHOLDER-" * 6                   # 72 bytes
        flat = bytearray(rnd.randbytes(256 + 256 + 200))
        self.at = 230                                       # straddles blocks 0 and 1
        flat[self.at:self.at + len(self.pattern)] = self.pattern
        self.flat = bytes(flat)
        self.uf2 = synthetic_uf2([self.flat[:256], self.flat[256:512], self.flat[512:]])
        self.loader = rnd.randbytes(len(self.pattern))

    def test_round_trip(self):
        out = tr.splice_uf2(self.uf2, self.pattern, self.loader)
        self.assertEqual(len(out), len(self.uf2))
        for i in range(3):
            a, b = self.uf2[i * 512:(i + 1) * 512], out[i * 512:(i + 1) * 512]
            self.assertEqual(a[:32], b[:32])                  # header fields preserved
            size = struct.unpack_from("<I", a, 16)[0]
            self.assertEqual(a[32 + size:], b[32 + size:])    # padding + magic end preserved
        blocks = tr.parse_uf2(out)
        flat = b"".join(blk[32:32 + size] for blk, size in blocks)
        want = self.flat[:self.at] + self.loader + self.flat[self.at + len(self.pattern):]
        self.assertEqual(flat, want)
        # and back again
        self.assertEqual(tr.splice_uf2(out, self.loader, self.pattern), self.uf2)

    def test_refusals(self):
        with self.assertRaisesRegex(fu.SafetyError, "loader is 71 bytes"):
            tr.splice_uf2(self.uf2, self.pattern, self.loader[:-1])
        with self.assertRaisesRegex(fu.SafetyError, "not in the firmware"):
            tr.splice_uf2(self.uf2, b"Z" * 72 + b"Q", b"x" * 73)
        dup = bytearray(self.flat)
        dup[600:600 + len(self.pattern)] = self.pattern
        uf2 = synthetic_uf2([bytes(dup[:256]), bytes(dup[256:512]), bytes(dup[512:])])
        with self.assertRaisesRegex(fu.SafetyError, "more than once"):
            tr.splice_uf2(uf2, self.pattern, self.loader)
        bad = bytearray(self.uf2)
        bad[2 * 512 + 4] ^= 1                               # block 2 magic1
        with self.assertRaisesRegex(fu.SafetyError, "block 2: bad UF2 magic"):
            tr.splice_uf2(bytes(bad), self.pattern, self.loader)
        bad = bytearray(self.uf2)
        struct.pack_into("<I", bad, 512 + 12, 0x10000200)   # gap before block 1
        with self.assertRaisesRegex(fu.SafetyError, "not consecutive"):
            tr.splice_uf2(bytes(bad), self.pattern, self.loader)
        bad = bytearray(self.uf2)
        struct.pack_into("<I", bad, 512 + 20, 5)
        with self.assertRaisesRegex(fu.SafetyError, "block 5 of 3"):
            tr.splice_uf2(bytes(bad), self.pattern, self.loader)
        with self.assertRaisesRegex(fu.SafetyError, "UF2 magic"):
            tr.splice_uf2(self.uf2[:-1], self.pattern, self.loader)

    @unittest.skipUnless(JL_LOADER.is_file(), "jl-uboot-tool checkout not available")
    def test_real_splice(self):
        out = tr.splice_uf2(tr.PLACEHOLDER_UF2.read_bytes(), tr.PLACEHOLDER_PATTERN.read_bytes(),
                            JL_LOADER.read_bytes())
        self.assertEqual(fu.sha256(out), tr.TRANSPORTER_UF2_SHA256)
        self.assertEqual(fu.sha256(out), "f275a52a0bd870c72f523ae28a54ac4d81dda9068a271318aa21dbdf95ef55ea")


def fake_jl_dir(root: Path, loader: bytes) -> Path:
    """What fm1_unbrick's setup accepts as an existing checkout at the pinned commit."""
    jl = root / "jl"
    for rel in ("jltech/uboot.py", "jltech/cipher.py", "scsiio/__init__.py", "data/chips.yaml",
                "data/usb-loaders.yaml"):
        (jl / rel).parent.mkdir(parents=True, exist_ok=True)
        (jl / rel).write_text("")
    (jl / fu.WL82_LOADER_REL).parent.mkdir(parents=True, exist_ok=True)
    (jl / fu.WL82_LOADER_REL).write_bytes(loader)
    (jl / fu.JLUB_MARKER).write_text(fu.JLUB_COMMIT + "\n")
    return jl


@unittest.skipUnless(JL_LOADER.is_file(), "jl-uboot-tool checkout not available")
class SetupTests(Base):

    def setUp(self):
        super().setUp()
        self.loader = JL_LOADER.read_bytes()

    def setup_cmd(self, jl, *extra, input_fn=None):
        return tr.main(["setup", "--jl-dir", str(jl), "--uf2", "out/fm1_transporter.uf2"]
                       + list(extra), input_fn=input_fn)

    def test_setup_builds_pinned_firmware(self):
        jl = fake_jl_dir(self.tmp, self.loader)
        with mock.patch.object(tr, "pyserial_ok", return_value=True), \
                mock.patch.object(fu, "_git", side_effect=AssertionError("network")):
            self.assertEqual(self.setup_cmd(jl, "--yes"), 0)
            out = Path("out/fm1_transporter.uf2")
            self.assertEqual(fu.sha256(out.read_bytes()), tr.TRANSPORTER_UF2_SHA256)
            text = self.out.getvalue()
            self.assertIn(tr.TRANSPORTER_UF2_SHA256, text)
            self.assertNotIn("not supported", text)                # fm1_unbrick's macOS note
            # idempotent
            self.assertEqual(self.setup_cmd(jl, "--yes"), 0)
            self.assertIn("already built and correct", self.out.getvalue())
            self.assertEqual(self.setup_cmd(jl, "--check"), 0)
            self.assertIn("All fingerprints match", self.out.getvalue())
            out.write_bytes(b"x" + out.read_bytes()[1:])
            self.assertEqual(self.setup_cmd(jl, "--check"), 1)

    def test_setup_refuses_tampered_loader(self):
        bad = bytearray(self.loader)
        bad[100] ^= 1
        jl = fake_jl_dir(self.tmp, bytes(bad))
        with mock.patch.object(tr, "pyserial_ok", return_value=True):
            self.assertEqual(self.setup_cmd(jl, "--yes"), 1)
        self.assertFalse(Path("out/fm1_transporter.uf2").exists())
        self.assertIn("jl-uboot-tool check failed", self.out.getvalue())
        # even past fm1_unbrick's own check, the splice step refuses it
        with mock.patch.object(fu, "cmd_setup", return_value=0):
            self.assertEqual(self.setup_cmd(jl, "--yes"), 1)
        self.assertIn("is not the expected file", self.out.getvalue())
        self.assertFalse(Path("out/fm1_transporter.uf2").exists())

    def test_pyserial_install_asks_first(self):
        jl = fake_jl_dir(self.tmp, self.loader)
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(tr, "pyserial_ok", return_value=False), \
                mock.patch.object(tr.subprocess, "run", run):
            self.assertEqual(self.setup_cmd(jl, input_fn=lambda p: "n"), 1)
        run.assert_not_called()
        with mock.patch.object(tr, "pyserial_ok", side_effect=[False, True]), \
                mock.patch.object(tr.subprocess, "run", run):
            self.assertEqual(self.setup_cmd(jl, input_fn=lambda p: "y"), 0)
        self.assertEqual(run.call_args[0][0], [sys.executable, "-m", "pip", "install", "pyserial"])


# --------------------------------------------------------------------------
# wizard
# --------------------------------------------------------------------------

class Script:
    """Scripted keyboard answers; EOF when they run out."""

    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class WizardTests(Base):

    def setUp(self):
        super().setUp()
        self.path, self.image = self.pkg("FM-1.fwsc", 3, identity="FM-1_015")
        pkg = fu.Package.load(self.path)
        p = mock.patch.object(fu, "V15_FLASH_FW_SHA256", pkg.firmware_sha256)  # "official" here
        p.start()
        self.addCleanup(p.stop)
        self.before = flash_from(self.image[:0x4000], random_image(77)[0x4000:0x93000],
                                 data_area())
        self.expected = self.before[:0x4000] + self.image[0x4000:0x93000] + self.before[0x93000:]

    def wizard(self, t, answers, *extra):
        script = Script(answers)
        rc = tr.main(["--dry-run", "--wait", "2", "wizard", "--out", "bk", "--uf2", "none.uf2"]
                     + list(extra), transporter=t, input_fn=script)
        return rc, script

    def test_wizard_synthetic_end_to_end(self):
        t, m = self.transporter(flash=self.before)
        other, _ = self.pkg("other.fwsc", 4)
        rc, s = self.wizard(t, [str(other), "'%s'" % self.path, "", "", "WRITE"])
        self.assertEqual(rc, 0)
        self.assertEqual(s.answers, [])
        self.assertEqual(bytes(m.flash), self.expected)
        text = self.out.getvalue()
        self.assertIn("not the official V15", text)
        for n in range(7):
            self.assertIn("Step %d of 6" % n, text)
        self.assertIn("DRY RUN: skipping flash-xiao", text)
        self.assertIn("D6 (GP0)      green          D+", text)
        self.assertIn("In short (the backup is", text)
        self.assertIn("Final full read EQUALS", text)
        self.assertIn("it boots stock V15", text)
        self.assertEqual(len(list(Path("bk").glob("*.bin"))), 1)

    def test_wizard_wrong_confirmation_writes_nothing(self):
        t, m = self.transporter(flash=self.before)
        rc, s = self.wizard(t, ["", "", "write"], "--v15", str(self.path))
        self.assertEqual(rc, 1)
        self.assertEqual(m.writes, [])
        self.assertEqual(bytes(m.flash), self.before)
        text = self.out.getvalue()
        self.assertIn("you did not type WRITE, so nothing was written", text)
        self.assertNotIn("Step 6 of 6", text)
        self.assertNotIn("Traceback", text)

    def test_wizard_no_keyboard_without_yes_stops(self):
        t, m = self.transporter(flash=self.before)
        rc, s = self.wizard(t, [], "--v15", str(self.path))
        self.assertEqual(rc, 1)
        self.assertEqual(m.reads, 0)
        self.assertIn("Nothing was written to the FM-1", self.out.getvalue())

    def test_wizard_connection_failure_explains_and_retries(self):
        t, m = self.transporter(flash=self.before, present=False)
        with mock.patch.object(tr, "HINT_EVERY", 0.2):
            rc, s = self.wizard(t, ["", "", "", "q"], "--v15", str(self.path), "--wait", "0.3")
        self.assertEqual(rc, 1)
        text = self.out.getvalue()
        self.assertIn("Problem while connecting to the FM-1", text)
        self.assertIn("the red wire must NOT be connected", text)
        self.assertEqual(m.writes, [])

    def test_real_run_needs_setup_first(self):
        with mock.patch.object(tr, "connect", side_effect=AssertionError("port opened")), \
                mock.patch.object(tr, "cmd_flash_xiao", side_effect=AssertionError("flashed")):
            rc = tr.main(["wizard", "--uf2", "none.uf2", "--v15", str(self.path)],
                         input_fn=Script([]))
        self.assertEqual(rc, 1)
        self.assertIn("Run `python3 fm1_transporter_recover.py setup` first", self.out.getvalue())


@unittest.skipUnless(REAL_DUMP.is_file() and V15_FWSC.is_file(), "real dump / V15 not available")
class WizardRealTests(Base):

    def test_wizard_real_v15_and_dump(self):
        uf2 = Path("none.uf2")
        if JL_LOADER.is_file():
            uf2 = Path("fm1_transporter.uf2")
            uf2.write_bytes(tr.splice_uf2(tr.PLACEHOLDER_UF2.read_bytes(),
                                          tr.PLACEHOLDER_PATTERN.read_bytes(),
                                          JL_LOADER.read_bytes()))
        script = Script(["no/such/FM-1.fwsc", str(V15_FWSC), "", "", "WRITE"])
        rc = tr.main(["--dry-run", "--mock-flash", str(REAL_DUMP), "wizard", "--out", "bk",
                      "--uf2", str(uf2)], input_fn=script)
        self.assertEqual(rc, 0)
        self.assertEqual(script.answers, [])
        text = self.out.getvalue()
        self.assertIn("That file cannot be used: cannot read package", text)
        self.assertIn("Official V15 verified: firmware sha256 %s" % fu.V15_FLASH_FW_SHA256, text)
        self.assertIn("resume state intact", text)
        self.assertIn("Final full read EQUALS the expected image", text)
        self.assertIn("Step 6 of 6: finish", text)


if __name__ == "__main__":
    unittest.main()
