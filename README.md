**New to this? Start with the illustrated guide: <https://quixotic7.github.io/MvaveFM1Unbricker/>**
(why FM-1s get bricked, a "find my fix" wizard, and step-by-step instructions in plain language).

Another easy guide: https://github.com/GoodPerson42/FM1-Unbricking-For-Dummies

# Recover from firmware lock

Newer FM-1's have different flash chips (0xB36014) that are locking users into firmwares they can't flash out of. 

Use to confirm your flash chip info. 

```sh
python3 fm1_unbrick.py info
```

Too get your unit back from this: put it in ROM boot (hold OCT-, OCT+ and HOME for 5 s, power on + OCT-, might be different depending on the firmware; or `python3 fm1_unbrick.py softkey`, or the Rescue box on https://thefirmwarehub.com/#rescue), then run

```sh
python3 fm1_unbrick.py restore V15-FM-1.fwsc
```

with the official V15 in the same folder: https://github.com/Quixotic7/FM1Backups (V15, V16, and a full dump of a
factory-fresh unit on the older flash chip). Both flash chips are accepted since 1.2.1, so `--i-know` is not needed.

**Use V15 for `restore`.** M-VAVE now only distributes V16, whose firmware runs to `0xA8FFF`, past the
`0x4000..0x92FFF` that `restore` (and the Transporter) writes. `restore` refuses it rather than write half a firmware.
To put V16 on directly, use `full-restore V16-FM-1.fwsc`: it writes all of V16's flash image, its own stock
bootloader included ([full-restore](#full-restore-everything-boot-head-included)).

# MvaveFM1Unbricker

Restore a soft-bricked **M-VAVE FM-1** over a plain USB cable, without extra hardware.

`fm1_unbrick.py` automates the recovery procedure that the r/MVaveFM1 community confirmed
working (see [Credits](#credits)). It talks to the FM-1's chip through
[jl-uboot-tool](https://github.com/kagaimiq/jl-uboot-tool) by kagaimiq, takes a verified
backup of the whole flash, writes the firmware region at `0x4000`, and checks every byte.

> Unofficial. Not affiliated with or endorsed by M-VAVE. At your own risk; read the
> [safety rules](#safety-rules) first.

## Is my FM-1 soft-bricked?

This tool is for you if **all** of these are true:

- the screen stays **black** when you switch the FM-1 on (typically right after a firmware update);
- the firmware installers (M-VAVE's, Felucca's, ChoralRoot's) say **"FM-1 not found"**;
- the computer sees a **USB disk called `WL82 UBOOT1.00`** (USB `VID_4C4A PID_8057`).
  On Windows: `Get-CimInstance Win32_DiskDrive | Format-Table Index, Model, InterfaceType`
  shows `WL82 UBOOT1.00 USB Device`. On Linux: `lsscsi -g` or `dmesg` shows `WL82 UBOOT1.00`.
  On macOS it appears as an unreadable disk.

`WL82 UBOOT1.00` is the boot mode built into the chip's mask ROM (the FM-1 is a JieLi WL82 /
AC791N). An update cannot erase it, and from it the flash can be read and rewritten.

**If your FM-1 never shows up as `WL82 UBOOT1.00`** (nothing on USB at all, in any mode), this
tool cannot reach it. The hardware route is the **FM-1 Transporter** (a Seeed XIAO RP2040 on the
USB data lines that forces the chip into that mode): <https://github.com/kurogedelic/FM-1-transporter>.
This repository has the step-by-step procedure for it, the parts, the wiring and a script that
automates it: **[TRANSPORTER-GUIDE.md](TRANSPORTER-GUIDE.md)** and `fm1_transporter_recover.py`
(it works on macOS, Linux and Windows: the Transporter is a serial port, no SCSI needed). That is
how the author's own unit came back on 2026-10-09 after an interrupted update.
**Boot head replaced or damaged** (`0x0000`-`0x3FFF`, for example by a replacement bootloader
installed with M-Flasher that does not start)? `restore` never writes the head, but
[`full-restore`](#full-restore-everything-boot-head-included) does: from the official package
(stock bootloader + firmware) or from a full backup. It needs the unit to show up as
`WL82 UBOOT1.00` (Windows, Linux or macOS). The Transporter route cannot write the head: its
firmware refuses it.

## What it does

`python fm1_unbrick.py restore FM-1.fwsc` runs these steps, and stops at the first problem:

1. **Package**: reads the `.fwsc`, prints its identity (`FM-1_015` = official V15), extracts the
   firmware region `0x4000..0x92FFF` (`0x8F000` bytes) of the package's flash.bin (found through
   the UFW entry list) and checks the official V15 hash `6edf3c37…58a8255686` of that region. A file that says it is V15 but has another hash is refused.
2. **Device**: finds exactly one `WL82 UBOOT1.00` USB disk (it refuses if there are none or
   several), checks that it answers as `WL82`, uploads jl-uboot-tool's wl82 USB loader, and
   requires chip key `0x980F` and flash ID `0x856014` or `0xB36014` (the 1 MiB chip of the
   2026 units with the logo on the front). Just before the loader starts it also
   reads 512 bytes of RAM at `0x1C7C000` (where Melodee leaves its crash record) and saves
   them as `retained-ram.bin` in this run's folder (see step 3). This is read-only and
   optional: if the read fails the tool carries on.
3. **Backup** (mandatory): reads the whole 1 MiB flash **twice**, compares the two reads,
   saves `backups/fm1-recovery-YYYYMMDD-HHMMSS/full-flash-before.bin`, and prints its sha256
   and what it found (for example "firmware sectors identical 143/143" against the package).
   That folder collects everything from the run: `full-flash-before.bin`, `retained-ram.bin`,
   `backup.json`, `recovery-result.json` (after a write) and `unbrick.log`. They are the same
   files Melodee's `fm1-recover` produces: if your unit was bricked by Melodee, zip the folder
   and send it to the Melodee developer. It contains your whole flash, on-device songs and
   sounds included.
4. **Confirmation**: shows device, identity, size and address, and asks you to type `WRITE`.
5. **Write**: erases and writes exactly `0x4000..0x92FFF`, then reads it back and compares
   byte for byte. On a mismatch it rewrites once; if that fails too it stops and tells you
   **not** to power off.
6. Tells you to unplug, switch off, wait 5 s, switch on.

Every step and hash is logged to `logs/unbrick-YYYYMMDD-HHMMSS.log`.

It works with the official `FM-1.fwsc` (recommended, hash-verified) or with any
Felucca-family package such as ChoralRoot's (`FM-1_920`); then the FM-1 boots straight into that
firmware.

### Commands

| Command | What it does |
|---|---|
| `setup` | fetches jl-uboot-tool at the pinned commit `adb3f18` into `./jl-uboot-tool/` (git, or a GitHub zip if git is missing), checks it, runs `pip install -r` on its requirements |
| `find` | lists `WL82 UBOOT1.00` USB disks (Windows: `Win32_DiskDrive`; Linux: `/sys/class/scsi_generic`). Opens nothing |
| `info` | uploads the loader, prints chip key and flash ID, checks them |
| `backup DIR` | double-read full backup into `DIR`; `--package X.fwsc` also compares the dump with a package |
| `extract PACKAGE.fwsc OUT.bin` | writes the firmware region to `OUT.bin` and prints the identity; `--verify-v15` fails unless it is official V15 |
| `restore PACKAGE.fwsc` | everything above, then write and verify. Refuses a package whose firmware runs past `0x92FFF` (official V16): use V15, or `full-restore` for V16. `--yes` skips the typed confirmation; `--verify-v15` refuses anything but the verified official V15; `--force-write` writes even if the region already matches |
| `full-restore PACKAGE.fwsc` or `full-restore --image FULL.bin` | backup, then rewrites every 4 KiB sector that differs, **boot head included**, sector by sector with read-back, head last, then a final full read. Type `FULL` to confirm. See [full-restore](#full-restore-everything-boot-head-included) |
| `softkey [--port NAME]` | for a unit that still runs: asks its identity over USB-MIDI, then sends the key `F0 22 24 35 7D F7` that drops stock V15 (and the Felucca family) into ROM boot mode (`WL82 UBOOT1.00`). Writes nothing; a power cycle brings it back. Type `UBOOT` to confirm. Works on macOS too (needs `python-rtmidi`, which `setup` installs; Linux falls back to raw ALSA) |
| `repair-settings --factory DUMP.bin [--package X.fwsc]` | backup, then puts the settings areas `0x93000..0xE8FFF` and `0xFC000..0xFEFFF` back to factory state (the factory settings sector `0x93000` from `DUMP.bin`, the rest erased), sector by sector with read-back, then a final full read. Type `REPAIR` to confirm. See [Settings corrupted after going back to stock](#settings-corrupted-after-going-back-to-stock) |

Options for every command: `--dry-run` (use a mock FM-1: never opens or writes real
hardware), `--device PATH` (`\\.\PhysicalDriveN` or `/dev/sgN`, only if the scan cannot decide),
`--i-know` (override the chip key / flash ID / identity checks: do not use it unless you know
why). Dry runs can inject faults: `--mock-flaky read|write|write-always`.

Exit codes: 0 done, 1 refused or failed with nothing written, 2 unsupported platform,
3 the write could not be verified (**do not power off**, run `restore` again; after
`repair-settings`: do not use the unit before a successful `repair-settings` run).

### The Transporter route: `fm1_transporter_recover.py`

For a unit with no USB device at all, through the [FM-1 Transporter](TRANSPORTER-GUIDE.md).
macOS, Linux or Windows; no compiler needed. The short version:

```sh
python3 fm1_transporter_recover.py setup                  # JieLi loader from jl-uboot-tool, the Transporter firmware file, pyserial
python3 fm1_transporter_recover.py wizard --v15 FM-1.fwsc  # step-by-step: flash the XIAO, wire it, dump, write V15
```

| Command | What it does |
|---|---|
| `setup` | fetches jl-uboot-tool at the pinned commit, checks `wl82loader.bin`'s sha256, splices it into the prebuilt placeholder firmware (`transporter/`) and checks the result's sha256, installs pyserial if missing |
| `wizard [--v15 FM-1.fwsc]` | the whole procedure as numbered steps with "press Enter when done" prompts: the checks, `flash-xiao`, the wiring, the FM-1 power-on, `dump` + analysis, the dry run, the typed `WRITE`, the write + verify, the power-cycle |
| `flash-xiao FILE.uf2` | puts the XIAO RP2040 into its `RPI-RP2` boot disk (the 1200-baud touch, or hold BOOT yourself), copies the Transporter firmware, waits for it to come back |
| `status` / `info` | waits for the Transporter's data port and for the FM-1 to answer the USB_KEY; `info` requires chip key `0x980F` and flash ID `0x856014` or `0xB36014` |
| `rekey` | reboots the Transporter into USB_KEY mode (then switch the FM-1 off and on) |
| `dump DIR [--v15 FM-1.fwsc] [--package X.fwsc ...]` | reads the whole 1 MiB twice, compares, saves one dump, and prints the analysis (head, app sectors per package, torn sectors, the staged loader, the update records, the data objects) |
| `analyse DUMP.bin [--v15 ...] [--package ...]` | the same analysis offline, on a saved dump |
| `restore PACKAGE.fwsc --ref DUMP.bin [--write] [--yes] [--verify-v15]` | dry run unless `--write`: re-reads the flash (it must equal the dump), writes only the differing 4 KiB sectors of `0x4000..0x92FFF`, each erased, written and read back by the Transporter, then a final full read must equal the expected image. Type `WRITE` to confirm |
| `recover PACKAGE.fwsc [--out DIR]` | `info`, `dump`, the dry run, the confirmation, the write, the power-cycle instructions |
| `restore` / `recover` `--image FULL.bin` | the same, with the application region taken from a full 1 MiB flash backup instead of a package. Only `0x4000..0x92FFF` is written; the backup's head and device data are not (the Transporter refuses them). Refused if the image is torn or blank, or if its head differs from the unit's and its application region is not V15. See [TRANSPORTER-GUIDE.md](TRANSPORTER-GUIDE.md#from-a-full-flash-backup-instead-of-a-package) |

`--dry-run` drives an in-memory mock Transporter; `--mock-flash DUMP.bin` seeds it from a real
dump, to rehearse the whole flow without hardware. Exit codes as above.

## Requirements

- the FM-1 and a USB **data** cable (not charge-only), plugged in **directly** (no hub);
- Python 3.9 or newer;
- **Windows** 10/11, run from an **administrator** PowerShell; or
- **Linux** (a Raspberry Pi works), run as root (`sudo`), with the `sg` driver loaded
  (`sudo modprobe sg`); or
- **macOS** 11 or newer (Apple silicon or Intel), run as root (`sudo`): see below;
- internet access once, for `setup` (and git, optional);
- the firmware package, e.g. the official `FM-1.fwsc` (next section).

**macOS** (new in 1.4.0). macOS has no SCSI pass-through for a USB disk, so jl-uboot-tool has no
macOS back end of its own. `fm1_macusb.py` is one: it takes the `WL82 UBOOT1.00` device from macOS's
disk driver and speaks USB Mass Storage Bulk-Only Transport itself, through libusb (pyusb and the
`libusb-package` wheel, which `setup` installs; no Homebrew needed). Everything above it is
unchanged: jl-uboot-tool's protocol classes, the backups, the checks, the gates. Taking a device from
the disk driver needs root, as for M-Flasher, so the commands that open the FM-1 run with `sudo`;
`find` and `softkey` do not need it.

The WL82 resets itself when its USB goes quiet for a few seconds: on Windows and Linux the disk
driver keeps it busy, on macOS nothing does once the driver is detached. So `fm1_macusb.py` sends
TEST UNIT READY whenever the line has been idle for a second (measured on hardware: without it the
unit dropped after 10 s idle, for instance while you type `WRITE`; with it, it stayed 30 s and more).
Before the tool has opened it, macOS's own driver also lets it go back to its firmware after about
half a minute (M-Flasher reports the same): start soon. The easy way is one line that asks for the
password first, then sends the soft key and starts the command at once (below).

Tested on an FM-1 (flash `0x856014`) on an Apple-silicon Mac, 2026-11-10: `info`, `backup`,
`restore` V15 (written and verified), then `full-restore --image` of the earlier backup, which put
the unit back byte for byte.

## Getting the official V15 package

Download the FM-1 firmware from M-VAVE's product page:
<https://www.m-vave.com/product?id=fm-1>. You need the file `FM-1.fwsc` (V15). The tool checks
it: identity `FM-1_015`, firmware-region sha256
`6edf3c37fb5bbbc33607c89375ee024d5477c17914d72221c8c68e58a8255686` (file sha256
`db1642b2b6fa5c2cccb11ffd13878068bb28601678d3644049f99dc40e7edb8a`, informational). The firmware is
M-VAVE's; this repository does not contain or redistribute it.

> **Fixed 2026-10-07.** Versions before this fix took the firmware region from the wrong offset
> (logical image `0x4000`, instead of the package's flash.bin, which starts at `0x400` in the
> logical image), so a restore would have written bytes shifted by `0x400`. The published V15
> hash `6edf3c37…` was right all along: it is the hash of the flash.bin region; only the slice
> the tool took was wrong.

## The 5-minute recipe

Put `FM-1.fwsc` in this folder. Switch the FM-1 on and plug it in (the screen stays black).
**If Windows offers to format a disk, click Cancel.**

### Windows (administrator PowerShell: right-click Start > Terminal (Admin))

```powershell
cd C:\path\to\MvaveFM1Unbricker
py fm1_unbrick.py setup
py fm1_unbrick.py restore FM-1.fwsc
```

### Linux / Raspberry Pi

```sh
cd MvaveFM1Unbricker
python3 -m venv .venv
.venv/bin/python fm1_unbrick.py setup
sudo modprobe sg
sudo .venv/bin/python fm1_unbrick.py restore FM-1.fwsc
```

### macOS (Terminal)

```sh
cd MvaveFM1Unbricker
python3 -m venv .venv
.venv/bin/python fm1_unbrick.py setup
sudo .venv/bin/python fm1_unbrick.py restore FM-1.fwsc
```

If the FM-1 still runs a firmware that answers the soft key (stock, Felucca family), let the tool put
it into ROM boot mode and start right away:

```sh
sudo -v && .venv/bin/python fm1_unbrick.py softkey --yes && sudo .venv/bin/python fm1_unbrick.py restore FM-1.fwsc
```

If macOS says the disk is not readable, click **Ignore** (never Initialize).

Type `WRITE` when asked. When it says **Done**: unplug USB, switch the FM-1 off, wait 5 seconds,
switch it on. Keep the folder in `backups/`: if you used Felucca or ChoralRoot, your on-device
projects and settings are in it.

Want to look before you leap? `py fm1_unbrick.py restore FM-1.fwsc --dry-run` runs the
whole procedure against a mock FM-1.

## Manual fallback (reference)

If you would rather type it yourself, this is the community guide's procedure, unchanged in
substance. [UNBRICK-GUIDE.md](UNBRICK-GUIDE.md) explains every step for Windows and Linux.

```text
py fm1_unbrick.py extract FM-1.fwsc app_v15.bin --verify-v15     (or the guide's fm1_extract_app.py)
Get-CimInstance Win32_DiskDrive | Format-Table Index, Model, InterfaceType
cd jl-uboot-tool
py jluboottool.py --device '\\.\PhysicalDrive5'                  (your Index; Linux: /dev/sgN)
    -> Quick info must show  Chip key: 0x980F  and  ID: 0x856014 (or 0xB36014)
=>JL: read 0 0x100000 backup.bin
=>JL: read 0 0x100000 backup2.bin
=>JL: exit
Get-FileHash backup.bin, backup2.bin                             (must be equal)
py jluboottool.py --device '\\.\PhysicalDrive5'
=>JL: write 0x4000 app_v15.bin
=>JL: read 0x4000 0x8F000 verify.bin
=>JL: exit
Get-FileHash verify.bin       (must be 6EDF3C37FB5BBBC33607C89375EE024D5477C17914D72221C8C68E58A8255686)
```

Then unplug, switch off, wait 5 s, switch on.

## Safety rules

- **Nothing below `0x4000` is ever written or erased, except by `full-restore`.** `0x0000`-`0x3FFF`
  is the boot head that starts the firmware. In the script every erase and write of `restore`
  and `repair-settings` goes through a single gate that refuses addresses below `0x4000`,
  unaligned erases, and anything past the end of the flash. `full-restore` has its own gate
  (whole 4 KiB sectors only, this unit's Bluetooth data and key_mac kept unless asked) and only
  writes a head that passes the checks below.
- **Never `erasechip`.** The script has no way to send it.
- **Always back up first.** `restore` cannot write without a double-read, compared, saved backup.
- **Write the extracted firmware region, never the `.fwsc` itself, and only at `0x4000`.**
- **One device only.** With two `WL82 UBOOT1.00` disks connected the script refuses to guess.
  It never opens any disk that is not reported as `WL82 UBOOT1.00`, and the device must answer
  the SCSI inquiry as `WL82` before anything else is sent.
- **Cancel Windows' "format disk" dialog.** Never format the `WL82 UBOOT1.00` disk.
- If verification fails: **do not power off**. Run `restore` again (another cable or port helps).

## full-restore: everything, boot head included

For a unit whose **boot head** (`0x0000`-`0x3FFF`, the stock bootloader and its configuration)
was replaced or damaged, typically by installing a replacement bootloader that then does not
start. The firmware can be perfect and the unit still stays dark, and `restore` cannot help
because it never writes the head.

```sh
python fm1_unbrick.py full-restore FM-1.fwsc --plan-only    # back up, show what would change, stop
python fm1_unbrick.py full-restore FM-1.fwsc                # write it (type FULL)
python fm1_unbrick.py full-restore --image my-backup.bin    # or: everything from a full backup
```

(Windows: `py` in an administrator PowerShell; Linux and macOS: `sudo .venv/bin/python`.)

- **From the package** (`FM-1.fwsc`): writes its whole flash image from `0x0`: for V15
  `0x0..0x92FFF` (the stock V15 bootloader and firmware), for V16 `0x0..0xA8FFF` (V16's own,
  larger stock bootloader and its longer firmware). Everything past the package's image stays as it
  is. Both official packages are recognised by sha256 (V16: file `d017fb65…`, the same M-Flasher
  documents).
- **From a full backup** (`--image FULL.bin`, exactly 1 MiB, for example the backup M-Flasher or
  this tool made before the change): writes all of it, including the factory voices
  (`0xEA000..0xFBFFF`), which only a backup can bring back. This unit's own Bluetooth data
  (`0xE9000..0xE9FFF`) and key_mac area (`0xFF000..0xFFFFF`) are kept, so another unit's backup
  does not clone its Bluetooth identity; `--include-unit-data` writes them too (your own
  backup).

What it checks:

- the usual: chip key and flash ID, the double-read backup before anything is written, the typed
  confirmation;
- **the head it writes must have valid JieLi headers**: header CRC, file-table CRCs, a
  `uboot.boot` inside the head, the `isd_config.ini` data CRC. No override. The body of
  `uboot.boot` itself has no checksum this tool can verify, which is why the next rule exists;
- **the head must be the stock V15 or V16 one** (by sha256; two real V15 units carry exactly the
  V15 head), unless you pass `--other-head`, for example to put back m-bootloader from your own
  backup;
- an image must not be blank or have torn firmware sectors;
- only the 4 KiB sectors that differ are erased (sector erases only, never block or chip) and
  written, each read back and retried once, **the head last**, then the whole flash is read and
  compared with the expected image. `full-restore-result.json` in the run's folder records it.

If it is interrupted while the head is written, the FM-1 will not start until the command has
run again: keep it plugged in and run the same command again.

## Settings corrupted after going back to stock

Felucca-family firmwares (Felucca, ChoralRoot) keep their songs, sounds and settings at
`0x97000..0xDFFFF`, stage their update loader at `0xE0000`, and keep globals at `0xFC000`. The
first two are inside stock V15's own settings store (`0x93000..0xE8FFF`), and the installers only
rewrite the firmware (`0x4000..0x92FFF`). Back on V15, stock reads those leftovers as its own
settings, and the presets come out corrupted.

1. First try stock's own **System > Reset Patches**.
2. If that does not help: `python fm1_unbrick.py softkey` (any OS, the FM-1 on its normal
   screen) drops the unit into ROM boot mode. On a Mac, start the next step at once (it leaves
   ROM boot mode after about half a minute): `sudo -v && .venv/bin/python fm1_unbrick.py softkey
   --yes && sudo .venv/bin/python fm1_unbrick.py repair-settings --factory DUMP.bin`.
3. `python fm1_unbrick.py repair-settings --factory DUMP.bin` (administrator PowerShell / sudo).
   It takes the usual double-read backup, then makes `0x93000..0xE8FFF` and `0xFC000..0xFEFFF`
   look like a factory unit: the factory settings sector `0x93000` from `DUMP.bin`, everything
   else there erased. Everything else stays bit for bit as it is: the firmware, the Bluetooth
   data (`0xE9000`), the factory voices (`0xEA000..0xFBFFF`) and `0xFF000..0xFFFFF` are never
   written (the tool's write gate refuses them). Songs, sounds and settings in those areas are
   deleted; the backup keeps them.
4. Unplug, switch off, wait 5 s, switch on, then reload the banks with System > Reset Patches.

`DUMP.bin` is the `backup` (`full-flash-before.bin`) of a factory-fresh FM-1, taken before any
custom firmware was installed on it. The tool refuses a dump that is not clean (its sector
`0x93000` must start with `55 AA AA 55`, and `0x94000..0xE8FFF` and `0xFC000..0xFEFFF` must be
erased). **Not yet tested on a real corrupted unit**: only against a mock FM-1.

## Troubleshooting

### The FM-1 does not show up as "WL82 UBOOT1.00" at all

The chip only exposes the UBOOT disk once it has fallen back to ROM boot. A black screen alone
does not mean it has. In order, with the FM-1 plugged straight into the PC (no hub) with a cable
known to carry data:

1. **Look everywhere, not only at disk drives.** In PowerShell:
   `Get-PnpDevice | Where-Object { $_.InstanceId -like '*VID_4C4A*' } | Format-Table Status, Class, FriendlyName`
   (the JieLi vendor id is 4C4A; the ROM boot disk is PID 8057). A device listed with an error
   status still counts: note it. On Linux: `lsusb | grep -i 4c4a` and `dmesg | tail`.
2. **Leave it switched on for two full minutes.** Felucca-family firmware arms a watchdog and a
   boot-loop guard: a firmware that hangs or crashes at boot is reset by the watchdog, and after
   two failed boots in a row the guard itself enters ROM boot. Then check again.
3. **The update-mode hold.** Switch the FM-1 off. Hold OCT- and OCT+ together, switch it on, and
   keep holding for 10 seconds. If the firmware reaches its main loop at all, this enters update
   mode (the screen may stay black). Check again.
4. **Three quick restarts.** Switch on, wait 10 s, off; on, wait 10 s, off; on. If each boot was
   a crash, the third one lands in ROM boot. (This is also how a unit can get there by accident.)
5. If after all that nothing with vendor id 4C4A ever appears, the chip is not reaching ROM boot
   over USB and `fm1_unbrick.py` cannot help. The hardware route is the
   [FM-1 Transporter](https://github.com/kurogedelic/FM-1-transporter): see
   [TRANSPORTER-GUIDE.md](TRANSPORTER-GUIDE.md) (parts, wiring, `fm1_transporter_recover.py`).
   This is exactly what an update interrupted mid-write by the FM-1 itself (a flat battery) looks
   like: the half-written firmware hangs before any of its own safety code runs, and the chip's
   SPL does not fall back to the update loader on a power-on. Holds and restarts cannot help there.


- **"No WL82 UBOOT1.00 device found"**: switch the FM-1 on, use a data cable, plug in directly.
  Windows: run from an administrator PowerShell. Linux: `sudo modprobe sg`, run with `sudo`.
  Check `Get-CimInstance Win32_DiskDrive` / `lsscsi -g`. jl-uboot-tool's own `jldevfind.py`
  usually reports "No devices found" on Windows (it scans volumes, not disks); that is expected,
  this script uses `Win32_DiskDrive` instead.
- **The disk number changes** after replugging. The script re-scans every time; with the manual
  procedure, check it again every session.
- **"Unknown syntax: Get-FileHash"** (manual procedure): you are still inside jl-uboot-tool.
  Type `exit` and wait for `Bye!`.
- **SyntaxWarning about `\s`** from `jldevfind.py`: harmless.
- **The two backup reads differ**: the connection is unreliable. Nothing was written. Another
  cable, another port, no hub.
- **"already holds exactly this firmware"**: the firmware region is intact, so rewriting it will
  not help. This was the case for the guide's author: Felucca 1.0.1 had been written perfectly
  but would not start because of data left by Felucca 0.9-beta. The guide's update and thread
  describe erasing Felucca's data areas in UBOOT mode (`erase 0x97000 0x8000`,
  `erase 0xFC000 0x2000`, and for old user presets `erase 0xDC000 0x4000`), which deletes the
  songs and settings stored on the device. This script deliberately does not do that; if you
  try it by hand, back up first. Restoring official V15 instead is the safer step: the guide
  reports that V15 boots even with old Felucca data present. (`--force-write` rewrites a region
  that already matches, which only helps if you suspect the flash itself.)
- **"boot head DIFFERS from the official V15 package's head"**: your boot head is not the
  stock one (M-Flasher can replace it with m-bootloader, for example). `restore` never writes
  it. If the FM-1 stays black after a verified `restore`, put the stock head back with
  `full-restore FM-1.fwsc` ([above](#full-restore-everything-boot-head-included)).
- **On-device songs**: stock firmware may overwrite the area where Felucca keeps projects;
  they are in your backup.

## Tests

No device needed:

```sh
python3 -m pytest -q          # or: python3 -m unittest -v
```

They cover the `.fwsc` extractor (against a real package and an independent black-box
implementation when available), the Transporter script against an in-memory mock Transporter
(`tests/test_transporter.py`: the dump, the analysis on synthetic half-written flashes, the dry
run, the write and its refusals), a `MockUboot` flash with NOR erase/program semantics and
fault injection (a corrupted backup read must abort; a corrupted write must be retried once;
a dead cell must stop with the do-not-power-off message), the device finders on canned
Windows and Linux outputs, the refusals (two devices, wrong chip key or flash ID, damaged V15,
unknown package, macOS), the 0x4000 gate, and, when a jl-uboot-tool checkout and its
requirements are present (`FM1_JLUB_DIR`), jl-uboot-tool's real protocol classes driving a
mock SCSI device through loader upload, backup, write and verify.

## Credits

- The guide **"[GUIDE] Unbricking a soft-bricked FM-1 (black screen / "WL82 UBOOT1.00") over
  plain USB, no extra hardware"** on r/MVaveFM1
  (<https://www.reddit.com/r/MVaveFM1/comments/1wz48t7/>), by **u/acrawf1**, who also wrote the
  original extraction script
  ([fm1_extract_app.py gist](https://gist.github.com/Acrawf1/ae2b9930b49231db397568cfc2abc2a8)).
  This tool automates that procedure.
- **u/International-Soup24**, who first showed jl-uboot-tool detecting and dumping a bricked FM-1.
- **kagaimiq** (Andrey Grigoryev), author of
  [jl-uboot-tool](https://github.com/kagaimiq/jl-uboot-tool) (MIT), which does all the talking to
  the chip. It is fetched by `setup`, not copied into this repository.
- **Leo Kuroshita (kurogedelic) / Hügelton Instruments**, credited by the guide: the
  [Felucca](https://github.com/hugelton/Felucca) documentation of the package format and flash
  layout (including "nothing below 0x4000"), and the
  [FM-1 Transporter](https://github.com/kurogedelic/FM-1-transporter) (MIT): the board firmware
  and line protocol that `fm1_transporter_recover.py` talks to.

## Licence

MIT, see [LICENSE](LICENSE). jl-uboot-tool is MIT-licensed by kagaimiq and is downloaded
separately. The official FM-1 firmware belongs to M-VAVE and is not included. This project is
not affiliated with M-VAVE, Felucca or jl-uboot-tool.
