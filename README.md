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

**Not covered:** if your FM-1 never shows up as `WL82 UBOOT1.00`, this tool cannot reach it.
The hardware route is the **FM-1 Transporter** (an RP2040 board on the USB data lines that
forces the chip into that mode): <https://github.com/kurogedelic/FM-1-transporter>.
Damage to the boot head (`0x0000`-`0x3FFF`) is not covered either: this tool never writes it.

## What it does

`python fm1_unbrick.py restore FM-1.fwsc` runs these steps, and stops at the first problem:

1. **Package**: reads the `.fwsc`, prints its identity (`FM-1_015` = official V15), extracts the
   firmware region `0x4000..0x92FFF` (`0x8F000` bytes) and checks the official V15 hash
   `6edf3c37…58a8255686`. A file that says it is V15 but has another hash is refused.
2. **Device**: finds exactly one `WL82 UBOOT1.00` USB disk (it refuses if there are none or
   several), checks that it answers as `WL82`, uploads jl-uboot-tool's wl82 USB loader, and
   requires chip key `0x980F` and flash ID `0x856014`.
3. **Backup** (mandatory): reads the whole 1 MiB flash **twice**, compares the two reads,
   saves `backups/fm1-flash-backup-YYYYMMDD-HHMMSS.bin`, and prints its sha256 and what it
   found (for example "firmware sectors identical 143/143" against the package).
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
| `restore PACKAGE.fwsc` | everything above, then write and verify. `--yes` skips the typed confirmation; `--verify-v15` refuses anything but the verified official V15; `--force-write` writes even if the region already matches |

Options for every command: `--dry-run` (use a mock FM-1: never opens or writes real
hardware), `--device PATH` (`\\.\PhysicalDriveN` or `/dev/sgN`, only if the scan cannot decide),
`--i-know` (override the chip key / flash ID / identity checks: do not use it unless you know
why). Dry runs can inject faults: `--mock-flaky read|write|write-always`.

Exit codes: 0 done, 1 refused or failed with nothing written, 2 unsupported platform,
3 the write could not be verified (**do not power off**, run `restore` again).

## Requirements

- the FM-1 and a USB **data** cable (not charge-only), plugged in **directly** (no hub);
- Python 3.9 or newer;
- **Windows** 10/11, run from an **administrator** PowerShell; or
- **Linux** (a Raspberry Pi works), run as root (`sudo`), with the `sg` driver loaded
  (`sudo modprobe sg`);
- internet access once, for `setup` (and git, optional);
- the firmware package, e.g. the official `FM-1.fwsc` (next section).

**macOS is not supported**: jl-uboot-tool has no macOS SCSI back end. `fm1_unbrick.py` says so
and exits with code 2. Use any Windows PC or Linux machine, a Raspberry Pi is enough.
`extract` and every `--dry-run` work on macOS.

## Getting the official V15 package

Download the FM-1 firmware from M-VAVE's product page:
<https://www.m-vave.com/product?id=fm-1>. You need the file `FM-1.fwsc` (V15). The tool checks
it: identity `FM-1_015`, firmware sha256
`6edf3c37fb5bbbc33607c89375ee024d5477c17914d72221c8c68e58a8255686`. The firmware is
M-VAVE's; this repository does not contain or redistribute it.

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

Type `WRITE` when asked. When it says **Done**: unplug USB, switch the FM-1 off, wait 5 seconds,
switch it on. Keep the file in `backups/`: if you used Felucca or ChoralRoot, your on-device
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
    -> Quick info must show  Chip key: 0x980F  and  ID: 0x856014
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

- **Nothing below `0x4000` is ever written or erased.** `0x0000`-`0x3FFF` is the boot head that
  lets the chip recover at all. In the script every erase and write goes through a single gate
  that refuses addresses below `0x4000`, unaligned erases, and anything past the end of the flash.
- **Never `erasechip`.** The script has no way to send it.
- **Always back up first.** `restore` cannot write without a double-read, compared, saved backup.
- **Write the extracted firmware region, never the `.fwsc` itself, and only at `0x4000`.**
- **One device only.** With two `WL82 UBOOT1.00` disks connected the script refuses to guess.
  It never opens any disk that is not reported as `WL82 UBOOT1.00`, and the device must answer
  the SCSI inquiry as `WL82` before anything else is sent.
- **Cancel Windows' "format disk" dialog.** Never format the `WL82 UBOOT1.00` disk.
- If verification fails: **do not power off**. Run `restore` again (another cable or port helps).

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
   over USB and this tool cannot help. The hardware route is the
   [FM-1 Transporter](https://github.com/kurogedelic/FM-1-transporter).


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
  stock one. This tool never writes it. Restoring V15 is still the first thing to try; if the
  FM-1 stays black with a verified write, ask for help in the community before going further.
- **On-device songs**: stock firmware may overwrite the area where Felucca keeps projects;
  they are in your backup.

## Tests

No device needed:

```sh
python3 -m pytest -q          # or: python3 -m unittest -v
```

They cover the `.fwsc` extractor (against a real package and an independent black-box
implementation when available), a `MockUboot` flash with NOR erase/program semantics and
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
  [FM-1 Transporter](https://github.com/kurogedelic/FM-1-transporter).

## Licence

MIT, see [LICENSE](LICENSE). jl-uboot-tool is MIT-licensed by kagaimiq and is downloaded
separately. The official FM-1 firmware belongs to M-VAVE and is not included. This project is
not affiliated with M-VAVE, Felucca or jl-uboot-tool.
