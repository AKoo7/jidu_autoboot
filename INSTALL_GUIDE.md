# Installing OpenWrt on a stock JIDU6801 (RAM handoff — no flashing)

This installs **our own OpenWrt build** on a stock JIDU6801 (Telpa / Jio AX6000 IDU, MediaTek
MT7986A) such that **every power-up boots it automatically** — without flashing the boot chain.

Proven end to end on 2026-09-21: cold power cycles, reboots, LuCI serving, and a full rollback.

---

## 1. Why RAM handoff and not a flash

The stock unit enforces secure boot:

* the ROTPK eFuse is **fused**, and BL2 verifies a full 5-cert TBBR chain on every boot;
* the stock U-Boot verifies the kernel FIT (`rsa2048`, key `fit_key`, baked into the FIP-signed
  control DTB);
* the rootfs is `dm-verity` protected, and the anti-rollback counter is fused at 0.

So replacing BL33/U-Boot or the kernel in flash fails signature verification, and the only keyless
writes are to **data** regions. That is the whole trick: the stock chain boots normally, and then
**Linux hands off to our kernel in RAM**, re-staging it from a data volume each boot.

```
BROM → BL2 → BL31 → U-Boot (stock, signed) → stock Linux (rooted)
                                                 │
                        boot hook reads the payload volume,  ← flash (data only)
                        patches the DTB in RAM, streams the
                        kernel (+ramdisk) into RAM, verifies CRCs
                                                 │
                        PSCI CPU_ON(cpu3 → our kernel)  ← firmware starts it
                        CPU0 powers itself off (CPU_OFF)
                                                 ▼
                   our OpenWrt, from RAM (initramfs payload)
                   or squashfs-on-UBI (section 6, persistent)
```

Nothing in the signed chain is modified, and **a reboot with the trigger disarmed returns the box
to stock**.

---

## 2. Prerequisites

| Requirement | Why / how |
|---|---|
| Stock box, already set up (wizard done, admin password set) | the web API is needed to root it |
| **Root SSH on the stock firmware** | `~/jidu6801/tools/idu-unlock/` (`./flash.sh check`, then unlock). Note: on this family the tool's own check can report "not unlockable" while the unlock **works** — it did on ours (fw `GMOBJIO_JIDU6801_R3.0.1`); verify by SSHing in and running `uname -a`. |
| Serial console | strongly recommended — it is the only channel that survives a handoff, and it is how you read boot failures. `~/jidu6801/tools/uart_broker.py` (log `/tmp/uart_broker.log`; `nc 127.0.0.1 4600` types into it) |
| Free space in the `ubi2` partition | initramfs mode: ~42 MiB. Squashfs mode (section 6): ~50 MiB (18 payload + 8 root + 24 overlay). A stock unit's `ubi2` is empty with ~87 MiB free. |
| Build host | Linux, ~30 GB free, `gcc/g++/make/git`, and ideally `ccache` (a rebuild with a warm cache is minutes, not hours) |

Credentials for the bench unit live in `~/jidu6801/creds/credentials.txt` — not reproduced here.

---

## 3. Build the OpenWrt image

```sh
cd ~/jidu6801
git clone --depth 1 https://github.com/openwrt/openwrt.git openwrt     # latest main
cd openwrt
```

**Device definition.** Mainline OpenWrt already carries the Jio AX6000 board support
(`mt7986a-jiorouter-ax6000-jidu6101.dts` + `mt7986a-jiorouter-common.dtsi`), and this hardware is
identical, so add a `jidu6801` device that reuses the upstream DTS. Append to
`target/linux/mediatek/image/filogic.mk`:

```make
define Device/jiorouter_ax6000-jidu6801
  DEVICE_VENDOR := JioRouter
  DEVICE_MODEL := AX6000
  DEVICE_VARIANT := JIDU6801
  DEVICE_DTS := mt7986a-jiorouter-ax6000-jidu6101
  DEVICE_DTS_DIR := ../dts
  DEVICE_PACKAGES := kmod-usb3 kmod-mt7915e kmod-mt7916-firmware kmod-mt7986-firmware mt7986-wo-firmware
  UBINIZE_OPTS := -E 5
  UBOOTENV_IN_UBI := 1
  BLOCKSIZE := 128k
  PAGESIZE := 2048
  IMAGE/sysupgrade.bin := sysupgrade-tar | append-metadata
endef
TARGET_DEVICES += jiorouter_ax6000-jidu6801
```

**Patch `board.d/02_network` — required, not optional.** The JIDU6801 reuses the upstream `jidu6101`
DTS, so board detection lands in that board's case, which reads its MAC with
`mtd_get_mac_ascii u-boot-env mac`. **On this hardware that MTD is a decoy with no `mac=` field**, and
the live env (a UBI volume) carries none either — so `label_mac` comes back empty, `macaddr_add` is fed
an empty string, and its arithmetic error makes it emit the literal **`":"`**. That is *non-empty*, so
the `[ -n "$lan_mac" ]` guard downstream passes it through into `/etc/board.json`, and the box comes up
on a **random MAC that changes every boot** (breaking DHCP reservations and anything keyed on the
address). The real MAC is the first 6 bytes of the **MFG** partition. Patch
`target/linux/mediatek/filogic/base-files/etc/board.d/02_network`, in the `jiorouter,ax6000-jidu6101)`
case:

```sh
label_mac=$(mtd_get_mac_ascii u-boot-env mac)
if [ -z "$label_mac" ]; then
	local mfg_dev model
	mfg_dev=$(find_mtd_chardev "MFG")
	if [ -n "$mfg_dev" ]; then
		model=$(dd if="$mfg_dev" bs=1024 count=1 2>/dev/null | strings | grep -o -m1 'JIDU[0-9A-Z]*')
		[ "$model" = "JIDU6801" ] && label_mac=$(get_mac_binary "$mfg_dev" 0x0)
	fi
fi
wan_mac=$label_mac
[ -n "$label_mac" ] && lan_mac=$(macaddr_add "$label_mac" 1)
```

Gate it on the model string: the MFG layout is **not** uniform across AX6000 boards (the 6j01 branch
reads `0x20` for JIDU6401 and an ASCII key at `0x1d0` for JIDU6601). This yields wan = MFG+0 and
lan = MFG+1, which is exactly what stock programmed — its own `/etc/config/network` has
`[redact-mac-0x27]` on the WAN and `…:28` on the LAN.

**Three files baked into the image** (`files/` is the tree-root overlay). All are needed:

```sh
mkdir -p files/etc/uci-defaults files/etc/init.d files/etc/rc.d

# 1. keep the address the stock firmware used, so existing bookmarks keep working
cat > files/etc/uci-defaults/99-jidu6801-lan <<'EOF'
#!/bin/sh
uci -q add_list network.lan.ipaddr='192.168.31.1/24'
uci -q commit network
exit 0
EOF

# 2. prove to the stock-side hook that OUR kernel booted (see the failure guard, section 6)
#    -> files/etc/init.d/handoff-ack   (copy from this repo, plus its rc.d symlink)
# 3. repair an overlay polluted by an older image on a rootfs-only upgrade (section 6)
#    -> files/etc/uci-defaults/99-jidu6801-fix-macaddr
```

Copy 2 and 3 verbatim from `openwrt/files/` in this repo — they are small and their comments explain
each decision. `handoff-ack` **must** end with a `sync`: the stock hook compares this token to decide
whether a boot reached our kernel, and an un-synced write is lost to a power cut, which makes the hook
count *successful* boots toward its disarm threshold (section 6).

> 🛑 **Two silent traps around `files/`, both hit live.** A new file there can be **neither committed
> nor built** unless you force both, and neither failure reports anything:
> * upstream's `.gitignore` line 16 is `/files` (it is a per-developer local overlay upstream), so a
>   plain `git add -A` **skips it** — it is in no commit and `git status` looks clean. Use
>   `git add -f`, and check with `git ls-files files/`.
> * a change under `files/` does **not** invalidate the image target, so `make` regenerates
>   `root.squashfs` **byte-identical** and the change never ships. Force it with
>   `touch target/linux/mediatek/image/Makefile target/linux/mediatek/image/filogic.mk`.
>
> Always verify inside the **built** root, not the tree: `grep -c <marker> build_dir/target-*/root-mediatek/<path>`,
> and again in the `root.squashfs` from the sysupgrade tar. An *unchanged sha256* on the new root
> against the deployed one is the giveaway.

`chmod +x` each script. Note the initramfs-era advice that "uci-defaults re-run every boot because
`/etc` is recreated" is **wrong in squashfs mode**: `/etc` lives on the persistent overlay, so these
run **once per rootfs install** — which is exactly what makes a migration script possible (a *new*
filename runs even though the old whiteouts persist).

**Configure and build.** LuCI lives in its own feed, and its config symbols only exist *after* the
feeds are installed — do them first or `CONFIG_PACKAGE_luci` silently disappears.

```sh
cat > .config <<'EOF'
CONFIG_TARGET_mediatek=y
CONFIG_TARGET_mediatek_filogic=y
CONFIG_TARGET_mediatek_filogic_DEVICE_jiorouter_ax6000-jidu6801=y
EOF
./scripts/feeds update -a
./scripts/feeds install -a
printf 'CONFIG_PACKAGE_luci=y\nCONFIG_PACKAGE_luci-base=y\nCONFIG_PACKAGE_luci-mod-admin-full=y\nCONFIG_PACKAGE_luci-theme-bootstrap=y\nCONFIG_PACKAGE_uhttpd=y\nCONFIG_PACKAGE_rpcd=y\n' >> .config
make defconfig
grep -E '^CONFIG_PACKAGE_luci' .config | head        # confirm luci is really enabled
make -j$(nproc)                                      # ~45 min cold, minutes with warm ccache
```

Two artifacts come out of the build, and for a squashfs install you need **both**: the
**initramfs-kernel.bin** is the FIT that `mkpayload.py` extracts the kernel and DTB from (section 4),
and the **squashfs-sysupgrade.bin** carries the root filesystem (section 6).

```
bin/targets/mediatek/filogic/openwrt-mediatek-filogic-jiorouter_ax6000-jidu6801-initramfs-kernel.bin
bin/targets/mediatek/filogic/openwrt-mediatek-filogic-jiorouter_ax6000-jidu6801-squashfs-sysupgrade.bin
```

---

## 4. Turn it into a handoff payload

`~/jidu6801/autostart/mkpayload.py` does the conversion; `build.sh` packs it with a manifest.

```sh
cd ~/jidu6801/autostart
python3 mkpayload.py ~/jidu6801/openwrt/bin/targets/mediatek/filogic/*jidu6801-initramfs-kernel.bin out
./build.sh out out
```

What it handles (each of these was a real bug first):

* **decompresses the kernel** (lzma/xz) and checks the arm64 `ARM\x64` header;
* **decompresses the initramfs** — OpenWrt ships the ramdisk as an **xz-compressed cpio**
  (dumpimage reports "uncompressed" because U-Boot isn't asked to decompress it), and the kernel
  wants a plain cpio;
* patches the device tree **surgically** by brace-matched node lookup:
  * `bootargs` gets `earlycon=uart8250,mmio32,0x11002000` — without it an early panic is **silent**
    and just looks like a hang;
  * `linux,initrd-start/end` are written as 8-byte placeholders for the on-device stager to patch
    (initramfs mode only).

Two flags change what it patches — **both default to the shipped behaviour**, and that is deliberate:
the artifact the unit runs was measured with these settings, so a plain rebuild must not silently
change the boot path. (This has bitten twice: the module source was once left on a debug variant that
a rebuild would have shipped, and an unconditional `cpu@0` edit here would have changed two things at
once.)

* `--wifi-off` → set `wifi@18000000` to `disabled`. **Not the default, and not needed.** The node is
  left **enabled**, because what actually makes the handoff safe is the hook's `rmmod` of the stock
  WiFi stack (§5.4 step 7). The disable was added at the same time as that rmmod and never isolated,
  so it looked load-bearing for a long time — while costing the image **both radios**. Isolated
  2026-09-21 (identical kernel, only that property flipped): both firmware images load, `phy0`+`phy1`
  register, board detection generates a full 2-band `/etc/config/wireless`, and an AP beacons at
  HE20/20 dBm. 8/8 cold boots, radios up every time. What you *do* see without the rmmod is the
  documented failure: the new kernel's `mt7915e` cannot get the patch semaphore and dies in
  `mt7915_mac_reset_work` ~20 s in.
* `--cpu0-off` → set `cpu@0` to `disabled`. **Not the default.** Left enabled, the kernel runs **4
  CPUs**: it re-powers physical cpu0 (the core stock was running on, which the module switched off)
  via `PSCI CPU_ON` during SMP bringup, and cpu0 becomes logical CPU1. `--cpu0-off` removes that one
  SMC by keeping the mask honest at 3 CPUs — a principled hardening with *no measured benefit* (see
  §9), so it ships as a variant: `autostart/out_sq_cpu0/`.

Output: `Image`, `part1.bin` (initrd, initramfs mode only), `handoff.dtb`, plus `payload.bin` and a
matching `jidu6801_stage` whose compiled-in CRC manifest must match the payload (always install
**both** — the stager validates the DTB's CRC, so the pair must be deployed together).

---

## 5. Install on the stock router

Everything below runs **on the stock box** over SSH (it is the only place the trigger can live).

### 5.1 Create the payload volume

Find the `ubi2` partition *by name* — the mtd index differs between the stock kernel and ours:

```sh
cat /proc/mtd | grep ubi2                     # stock: mtd6
ubiattach -m <that index> -d 1                # usually already attached as /dev/ubi1
ubimkvol /dev/ubi1 -N handoff -s 48MiB        # remove an old one with: ubirmvol /dev/ubi1 -N handoff
```

### 5.2 Write the payload

```sh
# push payload.bin to the box first (e.g. cat > /tmp/payload.bin over ssh), then:
ubiupdatevol /dev/ubi1_0 /tmp/payload.bin     # NOT dd — dd gets EPERM on this firmware
```

### 5.3 Install the trigger files

The overlay's **upperdir is `/overlay/upper`** (check `/proc/mounts`). Files written to
`/overlay/etc/...` are invisible in the merged `/etc` and a boot hook placed there will silently
never run.

```sh
mkdir -p /overlay/jidu6801 /overlay/upper/etc/init.d /overlay/upper/etc/rc.d
# payload-side files (readable from both stock and our kernel):
#   /overlay/jidu6801/handoff.sh          the sequence
#   /overlay/jidu6801/jidu6801_stage      device-side stager (patches the DTB, streams, verifies)
#   /overlay/jidu6801/jidu6801_handoff.ko staging module (owns the contiguous RAM run)
cp <the three files there> && chmod +x /overlay/jidu6801/handoff.sh /overlay/jidu6801/jidu6801_stage
# boot hook — the S99z name sorts AFTER idu-ssh so sshd exists before the cancel window:
cp handoff.init /overlay/upper/etc/init.d/handoff && chmod +x /overlay/upper/etc/init.d/handoff
ln -sf ../init.d/handoff /overlay/upper/etc/rc.d/S99zhandoff
```

### 5.4 What the hook does each boot

1. exits immediately if `/overlay/jidu6801/handoff-off` exists (the disarm flag);
2. refuses to fire after 5 attempts in a row (it cannot tell a failed handoff from a deliberate
   reboot, so it errs toward stock) — the message is printed on the console;
3. `jidu6801_stage --verify` — CRC-checks the payload in the volume against its manifest;
4. `insmod jidu6801_handoff.ko` — allocates the contiguous staging RAM;
5. `jidu6801_stage --stage` — reads the module's staging base, patches the DTB initrd pointers **in
   RAM**, streams Image+initrd+DTB through `/dev/mem`, then reads the whole run back and verifies
   payload-vs-source and zeroed gaps. Flash is only ever **read**;
6. waits for `br-lan` to have an address, then holds a **30 s window** (touch `handoff-off` to
   cancel), re-checks the flag;
7. quiesces: stops netifd, `wifi down`, brings all netdevs down, `rmmod mt7915e mt76_connac_lib mt76
   mac80211 cfg80211` (the old WiFi must be dead), offlines CPU1–3;
8. fires: writes `go 3 <image_pa> <dtb_pa> <len>` to `/proc/jidu6801_go`. The module flushes the
   payload, clean-invalidates cached RAM (skipping every DTB `no-map` region), CPU_ONs cpu3 into
   the stub → kernel, then powers off CPU0. It never returns. ("parking cpu0" in the log is a
   misleading pre-branch message — the CPU_OFF succeeds, and you can tell because **neither**
   `direct CPU_OFF rc=` nor `cpu_off did not take` follows it; the code after the SMC simply never
   runs. `cpu_off=0000000000000000` in the log only means `psci_cpu_off_fn` resolved to NULL, so it
   took the direct-SMC path.)

Note the new kernel then brings that core back: it issues `PSCI CPU_ON` for physical cpu0 during SMP
bringup, and cpu0 becomes **logical CPU1** — so the box runs **4 CPUs**, not 3. (The boot core is
physical cpu3, labelled logical CPU0; hence `Booting Linux on physical CPU 0x3`.) Section 4's
`--cpu0-off` is the switch that suppresses that call, and it is off by default because there is no
measured reason to want it.

In **squashfs** mode (section 6) step 5 streams `Image + DTB` with no initrd — the stager's
`--no-initrd` path also stopped verifying the DTB against an absent initrd's CRC (each part now
carries its own expected CRC).

---

## 6. Regular (squashfs) root instead of initramfs

The initramfs payload runs entirely from RAM: simple, but **every change is lost on reboot**.
For a normal, persistent OpenWrt, boot a **squashfs root** from UBI with a `rootfs_data` volume
as its overlay. Proven end to end on 2026-09-21 (autonomous boot, config surviving reboots).

Layout — three volumes on the same `ubi2` partition the payload already lives on:

| # | volume | type | size | contents |
|---|---|---|---|---|
| 0 | `handoff` | dynamic | 18 MiB | payload: Image + DTB, **no** initrd |
| 1 | `owrt-root` | **static** | 8 MiB | the squashfs root |
| 2 | `rootfs_data` | dynamic | 24 MiB | the overlay — this is what persists |

The kernel is still staged in RAM and started by PSCI; only the root filesystem comes from UBI.
The payload is rebuilt in **`--no-initrd`** mode, which sets the command line to:

```
dm-mod.create='' loglevel=7 earlycon=uart8250,mmio32,0x11002000 \
ubi.mtd=5 ubi.block=0,1 root=/dev/ubiblock0_1 rootfstype=squashfs rootwait
```

* `ubi.mtd=5` — in **our** kernel's partition map `mtd5` is `ubi2`; the stock map has it at 6.
  (The payload volume is only ever read from stock, where it is `/dev/ubi1_0`.)
* `ubi.block=0,1` — makes the **kernel** create the block device for volume 1 (`owrt-root`) before
  root is mounted (the `block ubiblock0_1: created from ubi0:1(owrt-root)` line is at t≈1.5 s,
  i.e. before userspace). `root=` then mounts it.
* fstools' `mount_root` finds the volume literally named `rootfs_data` and mounts it as the
  overlay, exactly as the stock firmware does with its own root.

### Install it

Do this from **stock** — our kernel has this volume mounted as its root, so it cannot rewrite it:

```sh
tar xf openwrt-*-jidu6801-squashfs-sysupgrade.bin -O sysupgrade-*/root > /tmp/root.squashfs
cat /proc/mtd | grep -w ubi2                       # stock: mtd6, usually already /dev/ubi1
ubimkvol /dev/ubi1 -N owrt-root -s 8MiB -t static
ubimkvol /dev/ubi1 -N rootfs_data -s 24MiB
ubiupdatevol /dev/ubi1_1 /tmp/root.squashfs        # NOT dd (EPERM on this firmware)
```

Then rebuild the payload with `--no-initrd` (section 4) and push it to `handoff` as before.
Volume **IDs are assigned in creation order**, so `owrt-root` must be volume 1 for
`ubi.block=0,1` (and `root=/dev/ubiblock0_1`) to be right.

### Upgrading just the rootfs later

Rewrite `owrt-root` **from stock** and reboot — `install/install-squashfs-root.sh <root.squashfs>` does
the whole job (creates the volumes if absent, writes the image, resets the guard, re-arms). The overlay
volume is untouched, so **config survives** — verified with a marker file and a uci change across a full
reboot.

That persistence cuts both ways: **a polluted overlay also survives.** A unit installed before the MAC
fix (section 3) keeps its `":"` macs through an upgrade, because the fix only applies when board
detection runs and that needs `/etc/board.json` to be *absent*. `files/etc/uci-defaults/99-jidu6801-fix-macaddr`
closes that: a one-shot migration that runs on the next boot after the upgrade. It does not re-derive the
MACs — it calls `board_detect` so the fixed `02_network` produces them, then propagates them into
`/etc/config/network`, so there is one source of truth for how MFG is read. It repairs both things the
old image got wrong: the `":"` on each LAN port, **and** the fact that it never created a `wan` device
section at all (the old `wan_mac` was empty so the guard skipped it), which left the WAN on the conduit's
random address. Fresh installs are unaffected: they find nothing to repair and exit silently.

After upgrading, confirm it actually landed rather than assuming — `/rom/etc/init.d/handoff-ack` should
contain `sync`, and the migration should be gone from `/etc/uci-defaults/` (consumed):

### The `rootfs_data` volume must be formatted BY STOCK

Our 6.18 kernel's UBIFS picks **zstd** as its default compressor when it auto-formats an empty
volume. The stock 5.4 kernel has no zstd, so it cannot mount it at all:

```
UBIFS error (ubi1:2 pid 10067): ubifs_mount: 'compressor "zstd" is not compiled in
```

That matters because the hook (running on stock) has to *read* this volume - see the failure
guard below. So create `rootfs_data` **empty and mount it once from stock** (the kernel formats
it with stock's own default, which our kernel also reads - verified both ways). A rootfs-only
upgrade must not touch it, and `install-squashfs-root.sh` recreates it automatically if it finds
one stock cannot read.

### Two things this mode changes

* **The disarm flag moves.** In initramfs mode our kernel's `ubi0:rootfs_data` *was* stock's
  rootfs_data, so the old recipe worked. Now our `/overlay` is a **different volume** from the
  one the hook writes to, so disarm from our kernel with
  `install/disarm-from-our-kernel.sh` (it attaches stock's UBI and sets the flag there).
* **The failure guard is counter-based, not clock-based.** The stock clock has no RTC and NTP
  sets it to whatever it likes (observed: `attempts` written at `epoch 36`, then the next boot
  saw a ~20000 s jump), which made the old 5-strike guard fire at random. The hook now keeps a
  *failure streak* and clears it when our kernel proves it booted, by bumping
  `/overlay/handoff-ack` (baked into the image as `/etc/init.d/handoff-ack`). Rebuild the image
  after changing that script — it only takes effect once the new rootfs is in `owrt-root`.

  **Both writes must be `sync`ed, and either one being lost breaks the guard in opposite
  directions.** Our token is written by `handoff-ack`; the hook keeps its own copy in
  `$DIR/ack`.
  * our token lost → the hook's `[ -n "$ACK" ]` test fails → the streak **never clears** → a
    successful handoff counts toward the 5, and repeated power cuts strand the box on stock;
  * the hook's copy lost → `LAST` always reads empty → the comparison **always fires** → the streak
    never accumulates, so the guard *looks* healthy and silently never disarms.

  Both were missing their `sync` (the window is ~30 s of page cache, `dirty_expire_interval`). Found
  by driving cold power cycles: the hook logged `attempt 2, failure streak 2 of 5` after a clean
  boot, with the counter reset from 10 to 1. Fixed, and verified over 6 cold cycles — every boot now
  logs `failure streak 1 of 5` with the counter climbing monotonically. When testing anything in this
  area, do **not** clear the streak from your harness: that masks exactly the behaviour under test.

---

## 7. Fire it and verify

First time, do it by hand with the hook disarmed so you keep control:

```sh
touch /overlay/jidu6801/handoff-off                 # disarm the boot hook
start-stop-daemon -S -b -m -p /tmp/mf.pid -x /bin/sh -- /overlay/jidu6801/manual_fire.sh
```

Watch the console. A good run looks like:

```
JIDU6801-HANDOFF: staging pa=0x49800000 size=0x6000000 ...
staged Image  0x49800000+0xe5d008 crc=1edacaab
staged initrd 0x4a800000+0x1a0e400 crc=97849dd5
FIRE go 3 0x49800000 0x4c400000 0x2c053c6
FIRING
Linux version 6.18.52 ... r0-820fbf4          <- our kernel
Run /init as init process
```

Then verify from the box (its own console is unambiguous — another device in a lab may share the
same LAN address):

```sh
uname -r; . /etc/openwrt_release; echo "$DISTRIB_DESCRIPTION"    # kernel + revision you built
ip -4 addr show br-lan | grep inet                               # both LAN addresses if baked in
ls /www/cgi-bin/luci; netstat -ltn | grep ':80 '                 # LuCI + web server

# the MACs are stable and are the factory ones, not random (section 3)
for i in br-lan lan1 wan; do printf '%-8s %s\n' $i "$(cat /sys/class/net/$i/address)"; done
# expect br-lan/lanN = MFG+1, wan = MFG+0

# both radios present, and an AP actually beacons
ls /sys/class/ieee80211/                                        # phy0 phy1
wifi up; iwinfo                                                 # ESSID + Mode: Master + HE20

# the guard's own state: our token, and the hook's copy should trail it by one boot
cat /overlay/handoff-ack

# on the STOCK side, the console line that matters after a successful fire:
#   our kernel booted (ack '<prev>' -> '<new>') - failure streak cleared
#   attempt 1, failure streak 1 of 5
# an *incrementing* streak after a clean boot means the token is not surviving (section 6)
```

A good run in **squashfs** mode looks like this on the console (no initrd line):

```
JIDU6801-HANDOFF: staging pa=0x49c00000 size=0x6000000 ...
VERIFIED: staging holds the payload (...) and zeros beyond
FIRING
JIDU6801-HANDOFF: CPU_ON accepted, cpu3 is starting our kernel; ...
Linux version 6.18.52 ... r0-820fbf4          <- our kernel
block ubiblock0_1: created from ubi0:1(owrt-root)
VFS: Mounted root (squashfs filesystem) readonly on device 254:0.
mount_root: switching to ubifs overlay
```

**Capture, not assumption.** The serial console is the record (`tools/uart_broker.py`, log
`/tmp/uart_broker.log`): it is continuous, lives on the host, and is timing-independent — which
matters because it is the *only* channel that shows a panic before userspace exists, and only because
`earlycon` is in the payload's bootargs. `pstore`/ramoops can also carry a trace to the next boot
(`/sys/fs/pstore/`), but do not rely on it: after our kernel panics **stock boots again and its own
console output goes into the same 64 KiB ring**, which wraps and can overwrite the record, and a cold
power cycle wipes DRAM with it. Note also that during the fire **two kernels share the UART**, so the
log garbles around that point — `CPU_ON` can appear as `CP[U_ON`.

Once that is good, **re-arm** and test the real thing:

```sh
rm -f /overlay/jidu6801/handoff-off /overlay/jidu6801/attempts
reboot            # or a full power cycle: it should come back in our OpenWrt, unattended
```

LuCI then answers on the box's LAN address (and on any secondary address you baked in).

---

## 8. Getting back to stock, and removing it

Any one of these:

1. **During the 30 s window** on any boot: `ssh root@<stock-ip>` then
   `touch /overlay/jidu6801/handoff-off`.
2. **From our kernel** (no network config needed). In **squashfs** installs our `/overlay` is a
   *different* volume from the hook's, so use the helper — it attaches stock's UBI first:
   ```sh
   install/disarm-from-our-kernel.sh
   ```
   In **initramfs** installs our `ubi0:rootfs_data` *is* stock's volume, so the direct form works:
   ```sh
   mount -t ubifs ubi0:rootfs_data /mnt
   touch /mnt/jidu6801/handoff-off
   umount /mnt
   ```
3. **Wait** — it self-disarms after 5 attempts **that never reached our kernel** and says so on the
   console. (Our kernel bumps `/overlay/handoff-ack` each boot; the hook clears the streak when it
   sees that counter move, so ordinary reboots do not count toward the 5.)

**Re-arm:** `rm /overlay/jidu6801/handoff-off` (and `rm attempts` to reset the counter).

**Remove everything:**

```sh
rm -f /overlay/upper/etc/rc.d/S99zhandoff /overlay/upper/etc/init.d/handoff
rm -rf /overlay/jidu6801
ubirmvol /dev/ubi1 -N handoff
```

The signed partitions (BL2, FIP, `kernel`, `rootfs`, `u-boot-env`) are never touched — verified
byte-identical against pre-work backups at every stage.

---

## 9. Troubleshooting — things that cost real time

| Symptom | Cause / fix |
|---|---|
| Boot resets with **no console output** | An early panic before the console exists. Check the reset reason in BL2's log: `Software reset (reboot)` = the kernel panicked and ran its reset path; a watchdog reset means it hung instead. **Put `earlycon` in the handoff DTB** so the panic prints a trace. |
| Old kernel panics with `dc` / cache abort | A cache-maintenance walk hit a DTB **`no-map`** region (`secmon@43000000`, `ramoops`, the wmcpu/wo buffers). Those have no linear mapping (and `secmon` is secure). Skip them. |
| New kernel dies in `mt7915_mac_reset_work` | The old kernel's WiFi was still live: the new kernel's `mt7915e` cannot get the patch semaphore, then its reset workqueue walks a half-initialised TXQ list. Fix = **rmmod `mt7915e mt76_connac_lib mt76 mac80211 cfg80211`** and bring the links down before firing (what the hook does). The `status = "disabled"` DTB patch is **not** part of the fix — see below. |
| `PSCI_FEATURES` **hangs the whole SoC** | This BL31 does not tolerate it. Never call it. (`PSCI_VERSION` is safe.) |
| `CPU_OFF` returns NOT_SUPPORTED | Only the **SMC32** ID works here: `0x84000002` with the state in w1 (`0x10000`). The SMC64 form `0xC4000002` is refused. |
| `dd` to a UBI volume → EPERM | Use `ubiupdatevol` instead. |
| Boot hook "does nothing" | It is probably in the wrong overlay layer — check the upperdir is `/overlay/upper`. |
| A script on the device mysteriously does nothing | **Verify the file after writing it.** A 0-byte `manual_fire.sh` (bad redirect) produced exactly the same symptoms as a failed handoff: empty log, box still on stock. |
| Browser can't reach LuCI | The image uses its own default LAN (`192.168.1.1`); the stock firmware used a different one. Also note other devices may share an address — confirm identity with the kernel/revision strings, not the web page. |
| `verify dtb: got 96b8a058 want 00000000` on a no-initrd payload | The stager mapped each part's expected CRC **by index**; with no ramdisk the DTB lands in slot 1 and was checked against the absent initrd's CRC. Fixed: each descriptor carries its own expected CRC (`jidu6801_stage.c`). |
| Box comes up on a **random MAC**, new every boot | The `":"` MAC bug (section 3): the pre-fix `02_network` emitted `":"` because its `label_mac` was empty. Fix the tree, and on an already-installed unit let `99-jidu6801-fix-macaddr` repair the overlay (section 6). Diagnostic: `/etc/board.json` contains `"macaddr": ":"`. |
| Hook logs `attempt 2, failure streak 2 of 5` after a **clean** boot | The boot token is not surviving (section 6). Both the token write and the hook's copy need a `sync`. |
| Interface has no MAC / `macaddr ':'` in the config | Same bug. `uci show network \| grep macaddr` shows it; expect MFG+1 on the LAN ports and MFG+0 on `wan`. |
| A new file under `files/` never appears in the image, or never in git | Two silent traps — see the call-out in section 3. Force the image (`touch` the image makefiles) and force the add (`git add -f`), then verify inside the built root. |
| A cold power cycle is needed for testing | Drive the relay at `http://<relay-ip>` (`/restart?seconds=N`, token in `creds/relay_token`); `tools/coldcycle_loop.sh` automates cold cycles and records the PHY count, `tools/handoff_loop.sh` does warm reboots. `/restart` is self-restoring. |
| `scp` to stock: `ash: /usr/libexec/sftp-server: not found` | The stock firmware has no sftp server. Stream instead: `ssh root@<ip> 'cat > /path/file' < local_file` — then compare `md5sum` on both sides. |
| On our kernel `/overlay/jidu6801/` is empty | Expected in **squashfs** mode: the hook's files are on stock's `rootfs_data`, a different volume. Only in initramfs mode is it the same volume. |
| Our kernel's LAN address does not answer from the host | The host may route that subnet elsewhere. Use `192.168.1.1` (the image's own default) for our kernel and `192.168.31.1` for stock. |
| Rewriting `owrt-root` from our kernel | Impossible — it is mounted as the running root. Do it from stock (section 6). |
| Hook logs nothing about the ack, streak never clears | `ubifs_mount: 'compressor "zstd" is not compiled in` — our kernel auto-formatted `rootfs_data` with zstd and stock cannot read it. Recreate the volume empty and mount it once from stock (section 6). |
| Hook cannot mount our `rootfs_data` at all | It must mount **rw**: our kernel never unmounts cleanly (the handoff is a raw reset), so the volume always needs journal recovery, and a read-only UBIFS mount refuses to do it. |

**On the old "about 1 failure in 8 fires" note — it does not hold for the current artifact.** That
figure came from a *single* failure in ~8 fires, so its 95% confidence interval is roughly 0.3%–52%;
it was never a measurement, and it belongs to the **initramfs** payload era. Everything since the
squashfs/`--no-initrd` switch is clean: ~40 fires (including 27 relay-driven **cold** cycles), which
bounds the current artifact's failure rate at **≤ ~7% (95%)** — and at n=40 a 12.5% rate is rejected at
about 0.5%. Best evidence that none was a silent miss: the boot-token counter climbs monotonically
across a run, and only a boot of our kernel writes it.

If it does recur, capture first and theorise second. On failure the kernel panics early and the guard
falls back to stock within a few attempts; `earlycon` is in the payload so the serial log should carry
a real trace. An SMP-bringup failure is an early boot failure and is a plausible shape — which is what
`--cpu0-off` (section 4) exists to test — but it has **no** supporting evidence yet, and the prior
park-vs-CPU_OFF experiment argues against blaming CPU0's disposal. Record the trace in
`logs/` and note which payload it came from: two payloads are easy to conflate here, and a reliability
claim is meaningless without saying which one it belongs to.

---

## 10. Files produced by this work

This whole tree except `openwrt/`, `backup/`, `creds/` and the build outputs is under version control
(its own repo), and `MANIFEST_DEPLOYED.txt` pins every deployed binary by hash — they are deliberately
not stored, being tens of MB each.

```
~/jidu6801/
├── openwrt/                     the cloned + patched OpenWrt tree (main, r0-820fbf4 at time of writing)
│   ├── target/.../02_network        the MFG MAC fix (section 3) — required
│   └── files/                       baked into the image (all three, section 3)
│       ├── etc/init.d/handoff-ack       our boots bump /overlay/handoff-ack (must sync)
│       ├── etc/rc.d/S99handoff-ack
│       ├── etc/uci-defaults/99-jidu6801-lan         keeps 192.168.31.1
│       └── etc/uci-defaults/99-jidu6801-fix-macaddr repairs an upgraded overlay
├── autostart/
│   ├── mkpayload.py             image → handoff payload (patch DTB; --no-initrd / --wifi-off / --cpu0-off)
│   ├── build.sh                 pack payload.bin + manifest + stager
│   ├── jidu6801_stage.c         device-side stager source
│   ├── install/                 stock-side files: handoff.sh, handoff.init, README.txt,
│   │   │                        install-squashfs-root.sh, disarm-from-our-kernel.sh
│   ├── out_sq/                  squashfs payload pieces + the rootfs builds
│   ├── out_sq_wifi/             ← DEPLOYED payload (wifi enabled)
│   ├── out_sq_cpu0/             variant: wifi enabled + cpu@0 disabled (section 4)
│   └── out_v2/                  older initramfs payload pieces
├── kmod/handoff/                jidu6801_handoff.ko (staging module) + source (JH_PARK_CPU0)
├── tools/                       uart_broker.py (serial capture) · deploy_payload.sh (payload+stager swap,
│                                md5-gated, from our kernel) · coldcycle_loop.sh · handoff_loop.sh ·
│                                _jiducreds.py
├── MANIFEST_DEPLOYED.txt        deployed artifacts pinned by hash + the superseded ones
├── RESUME_*.md                  session records (state, traps, what was measured)
├── logs/                        serial captures + the cold-cycle runs (REDACTION_NOTE.txt explains the
│                                markers where credentials were replaced)
└── creds/credentials.txt        unit credentials — git-ignored, the only copy
```

On the router: payload volume `handoff` on `ubi2` (`/dev/ubi1_0`), squashfs root in `owrt-root`
(volume 1), overlay in `rootfs_data` (volume 2); the hook's files in **stock's**
`rootfs_data` at `/overlay/jidu6801/`, boot hook at `/overlay/upper/etc/rc.d/S99zhandoff`.
