# Prebuilt FM-1 Transporter firmware (placeholder build)

`fm1_transporter-a632d92-placeholder.uf2` is the [FM-1 Transporter](https://github.com/kurogedelic/FM-1-transporter)
firmware (MIT, Leo Kuroshita / Hügelton Instruments) for the **Seeed XIAO RP2040**, built from commit
`a632d923203170e3a08565f4056dbb3fd3d65ca5` (2026-10-01) with pico-sdk 2.2.0 and Arm GNU Toolchain 14.2.Rel1
(`-DPICO_BOARD=seeed_xiao_rp2040`), **with a placeholder where JieLi's `wl82loader.bin` goes**. The Transporter
embeds that 24064-byte loader at build time and its author does not redistribute it; neither does this repository.
`python3 fm1_transporter_recover.py setup` fetches it from kagaimiq's jl-uboot-tool at the pinned commit (the same
file `fm1_unbrick.py setup` uses), checks its sha256, and splices it into the placeholder to produce the firmware you
flash, `transporter/fm1_transporter.uf2` (not committed).

| file | sha256 |
| --- | --- |
| `fm1_transporter-a632d92-placeholder.uf2` (210944 B) | `363ec4b98071a40a1eadec14574c936c78bd8f2afdf17cc890104e887771b577` |
| `placeholder_loader.pattern` (24064 B: the bytes that stand in for the loader) | `25f62babdb1c62753f7e293338c8ee1b6f323aec9805644b794e6581e7c5bc29` |
| `wl82loader.bin` (fetched by `setup`, jl-uboot-tool `adb3f18`, `data/loaderblobs/usb/`) | `d41da6126760c9d66660bcc0cac8d27d221806c5e369a8036921efe68dca5376` |
| `fm1_transporter.uf2` (the spliced result) | `f275a52a0bd870c72f523ae28a54ac4d81dda9068a271318aa21dbdf95ef55ea` |

The spliced file is byte-identical to a build made with the real loader present (checked 2026-10-09 against the
build that recovered the author's unit). To rebuild the placeholder yourself: build the Transporter as its README says
with `-DFM1T_LOADER_BIN=placeholder_loader.pattern`; `setup --check` verifies all four hashes.
