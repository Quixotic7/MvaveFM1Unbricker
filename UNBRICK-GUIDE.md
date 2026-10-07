# Unbricking an FM-1 by hand

This is the manual version of what `fm1_unbrick.py restore` does. It follows the community guide
by u/acrawf1 on r/MVaveFM1 (credits in [README.md](README.md)), rewritten with Linux added.
Use it if you want to see every step, or if the script cannot run on your machine.

At your own risk. Not affiliated with M-VAVE.

## The idea in one paragraph

The FM-1's chip (JieLi WL82 / AC791N) has a boot mode in its mask ROM. When the firmware in
flash does not start, the chip falls back to it and the computer sees a USB disk named
`WL82 UBOOT1.00`. jl-uboot-tool uploads a small loader into the chip's RAM through that mode,
and the loader can read, erase and write the 1 MiB flash. The flash has two parts that matter:
the **boot head** at `0x0000`-`0x3FFF`, which you never touch, and the **firmware region** at
`0x4000`-`0x92FFF` (`0x8F000` bytes, 143 sectors of 4 KiB), which you replace with a known-good
copy taken from a firmware package.

## Ground rules

1. Only write at `0x4000`. Never write or erase anything below it.
2. Never use `erasechip`, and do not use `erase` or `write` at addresses other than the ones here.
3. Back up the whole flash first, twice, and compare the two copies.
4. Write the **extracted** firmware region, never the `.fwsc` file itself.
5. If Windows offers to format a disk, click **Cancel**.

## You need

- the FM-1 and a USB data cable, plugged in directly;
- Windows (administrator PowerShell) or Linux (root);
- Python 3.9+;
- jl-uboot-tool: <https://github.com/kagaimiq/jl-uboot-tool> (Code > Download ZIP, or
  `py fm1_unbrick.py setup`, which puts the tested commit `adb3f18` in `jl-uboot-tool/`), and its
  requirements: `py -m pip install -r requirements.txt` inside its folder;
- the official firmware package `FM-1.fwsc` from <https://www.m-vave.com/product?id=fm-1>.

## 1. Make the file to write

A `.fwsc` package is the flash image with one extra byte after each of its first 20 blocks of
47 bytes; those 20 bytes spell the package identity. Removing them gives the logical image, a JieLi UFW
container: a ciphered header and entry list in its first `0x400` bytes, then the files. The
flash image is the package's flash.bin (found through the UFW entry list, at `0x400`), and
`0x4000`-`0x92FFF` of that flash.bin is what you write.

> **Fixed 2026-10-07.** Versions before this fix took the firmware region from the wrong offset
> (logical image `0x4000`, instead of the package's flash.bin, which starts at `0x400` in the
> logical image), so a restore would have written bytes shifted by `0x400`. The published V15
> hash `6edf3c37…` was right all along: it is the hash of the flash.bin region; only the slice
> the tool took was wrong.


```sh
py fm1_unbrick.py extract FM-1.fwsc app_v15.bin --verify-v15        # Windows
python3 fm1_unbrick.py extract FM-1.fwsc app_v15.bin --verify-v15   # Linux
```

(The original guide uses u/acrawf1's `fm1_extract_app.py FM-1.fwsc app_v15.bin`. Check that its
output starts with the same bytes as this tool's before using it.)

It must print identity `FM-1_015` and firmware sha256
`6edf3c37fb5bbbc33607c89375ee024d5477c17914d72221c8c68e58a8255686` (the region written).
The genuine `FM-1.fwsc` file itself has sha256
`db1642b2b6fa5c2cccb11ffd13878068bb28601678d3644049f99dc40e7edb8a` (informational).
**Any other hash: stop**, the package is damaged or not the genuine V15.

Copy `app_v15.bin` into the jl-uboot-tool folder and open a terminal there.

## 2. Find the device

Switch the FM-1 on (black screen) and plug it in.

**Windows.** `jldevfind.py` usually says "No devices found" on Windows; ask Windows instead:

```powershell
Get-CimInstance Win32_DiskDrive | Format-Table Index, Model, InterfaceType
```

Note the **Index** of `WL82 UBOOT1.00 USB Device` (InterfaceType `USB`). Your device is
`\\.\PhysicalDrive<Index>`, for example `\\.\PhysicalDrive5`. Every other line is one of your
real disks: never use those numbers. The number can change when you replug, so check every time.

**Linux.**

```sh
sudo modprobe sg
lsscsi -g        # or: grep -H . /sys/class/scsi_generic/sg*/device/{vendor,model}
```

Find the line with vendor `WL82` and model `UBOOT1.00`; your device is the `/dev/sgN` on it.

Below, replace `DEVICE` with your path.

## 3. Connect and check the chip

```sh
py jluboottool.py --device '\\.\PhysicalDrive5'      # Windows
sudo python3 jluboottool.py --device /dev/sg2        # Linux
```

The tool uploads its loader and prints a Quick info block. It must show **Chip key: 0x980F**
and **ID: 0x856014** (1 MiB flash). If not, type `exit` and stop. You are now at the `=>JL:`
prompt.

## 4. Back up everything

```text
=>JL: read 0 0x100000 backup.bin
=>JL: read 0 0x100000 backup2.bin
=>JL: exit
```

Compare the two files:

```powershell
Get-FileHash backup.bin, backup2.bin          # Windows
```
```sh
sha256sum backup.bin backup2.bin              # Linux
```

The hashes must be equal; otherwise the connection is unreliable (try another cable or port)
and you must not write anything. Keep `backup.bin` safe: if you used Felucca or ChoralRoot, your
on-device projects are in it.

Optional, to see what is on the flash: `py fm1_unbrick.py extract` on a package and compare
sectors, or run the script's `backup` command, which reports this for you.

## 5. Write the firmware region

Connect again (step 3) and type exactly:

```text
=>JL: write 0x4000 app_v15.bin
```

jl-uboot-tool erases `0x4000`-`0x92FFF` (64 KiB blocks where aligned, 4 KiB sectors at the
edges) and writes the file. Wait for the `=>JL:` prompt. Do not unplug anything meanwhile.

## 6. Verify

```text
=>JL: read 0x4000 0x8F000 verify.bin
=>JL: exit
```

Make sure you are back at your shell (not `=>JL:`), then:

```powershell
Get-FileHash verify.bin                       # Windows
```
```sh
sha256sum verify.bin                          # Linux
```

It must be `6EDF3C37FB5BBBC33607C89375EE024D5477C17914D72221C8C68E58A8255686`. If it is not,
**do not restart the FM-1**: connect again and repeat step 5, then step 6.

## 7. Restart

Unplug USB, switch the FM-1 off, wait 5 seconds, switch it on. It should start stock V15.

## Other packages

The same steps work with any Felucca-family `.fwsc` (for example ChoralRoot, identity
`FM-1_920`): extract it without `--verify-v15`, write at `0x4000`, and verify the read-back
against the sha256 that `extract` printed. The FM-1 then starts that firmware directly.

## When the region is already correct

If your backup's firmware region is already an exact copy of the firmware you tried to install,
rewriting it changes nothing; something else stops it from starting. The guide's author found
that Felucca 1.0.x could hang at start-up on data left by Felucca 0.9-beta, and that erasing
Felucca's data areas in UBOOT mode (after a backup) fixed it:

```text
=>JL: erase 0x97000 0x8000
=>JL: erase 0xFC000 0x2000
```

and, for old user presets, `erase 0xDC000 0x4000`. This deletes songs and settings stored on
the device. These addresses come from the community thread and were not verified by this
project; `fm1_unbrick.py` never performs these erases. Restoring official V15 is the safer first
step: the guide reports that it starts even with old Felucca data present.

## Boot head

The boot head is outside the scope of this procedure. If you want to know whether yours is
stock, read it (`read 0 0x4000 head.bin`) and compare it with the first `0x4000` bytes of the
flash image in the official V15 package; `fm1_unbrick.py backup DIR --package FM-1.fwsc` does
this comparison for you. In the thread, the author reported that his head was byte-identical to
the V15 package's head. Do not write the boot head.
