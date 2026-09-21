# jidu_autoboot

Boot your own **OpenWrt** on a stock **JIDU6801** (Telpa / Jio AX6000 AirFiber IDU, MediaTek MT7986A)
**without flashing anything**.

The unit enforces secure boot — a fused ROTPK eFuse, a 5-cert TBBR chain verified by BL2 on every boot, a
signed kernel FIT, a dm-verity rootfs — so the signed boot chain cannot be replaced. What *can* be written
is **data**, and that is the whole technique: let the stock chain boot normally, then have the rooted stock
Linux hand off to our kernel **in RAM**, re-staged from a UBI volume on every power-up. The signed chain is
never touched, and a disarm flag puts the box back on stock.

## → Start here: [`INSTALL_GUIDE.md`](INSTALL_GUIDE.md)

That is the reproducible recipe end to end. Read it before anything else.

## What is in here

The bare minimum to build the image and install it:

| | |
|---|---|
| `INSTALL_GUIDE.md` | the recipe |
| `patches/` | the OpenWrt tree changes, applyable with `git am` against upstream `820fbf4` |
| `autostart/` | payload generator (`mkpayload.py`), packer (`build.sh`), on-device stager source |
| `autostart/install/` | the device-side files: the boot hook, the rootfs installer, the disarm helper |
| `kmod/handoff/` | the staging/handoff kernel module source + its `CPU_ON` stub |
| `tools/` | `uart_broker.py` (serial capture) and `deploy_payload.sh` (payload/stager swap) |

## Prerequisites the guide assumes but this repo does not ship

- **Root on the stock firmware.** Obtained with the third-party [`idu-unlock`](https://github.com/wpfyorg/idu-unlock)
  tool — get it from upstream rather than a vendored copy. (`INSTALL_GUIDE.md` §2; on this hardware its own
  `check` can report "not unlockable" while the unlock still works.)
- **The stock kernel tree, for building the module.** `kmod/handoff/Makefile` builds against the stock
  5.4.225 headers to get the matching `vermagic` — a module with the wrong one will not `insmod`. Its
  `KDIR ?=` default points at the author's path, so pass your own:
  `make KDIR=/path/to/linux-5.4.225`. The stock firmware is a MediaTek vendor OpenWrt tree, so the source is
  obtainable from the vendor/GPL drop for this model.
- **The OpenWrt build host prerequisites** (a Linux host, ~30 GB, `gcc/g++/make/git`, ideally `ccache`) and a
  **serial console** — strongly recommended, and the only channel that survives a handoff.

## Two traps that will bite you, both silently

Both concern `files/`, the tree-root overlay the patches create:

1. **Upstream's `.gitignore` has `/files`.** A new file there is skipped by a plain `git add -A` — it lands in
   no commit while `git status` looks clean. Use `git add -f`.
2. **A change under `files/` does not invalidate the image target.** `make` regenerates `root.squashfs`
   *byte-identical* and your change never ships. Force it:
   `touch target/linux/mediatek/image/Makefile target/linux/mediatek/image/filogic.mk`.

In both cases the tell is the same: verify the marker inside the **built** root, and treat an unchanged
sha256 against a previous build as the giveaway.

## What is deliberately not here

This is a trimmed subset of a larger working tree. Not included, because none is needed to build or install:
the U-Boot secure-boot RE and mainline-chainload work, the U-Boot FIT-parser probes, the session records and
serial captures, the author's test harnesses, and the earlier initramfs-era setup scripts. Where
`INSTALL_GUIDE.md` mentions those paths, they refer to that larger tree, not this one.

Unit-identifying values are absent: the source here is a publication copy in which serials, MACs, PCBA codes
and the wifi SSID appear as **length-preserving markers** (`[redacted-serial]`, `[redacted-mac-0x27]`, …).
Credentials are not present at all.

## Licensing

This repo contains GPL-2.0-derived work — `kmod/handoff/jidu6801_handoff.c` is a Linux kernel module
(`MODULE_LICENSE("GPL")`) and the `patches/` are OpenWrt tree changes. **No licence file is included yet**;
that is a decision for the author, but the GPL obligations on those two components travel with them.
