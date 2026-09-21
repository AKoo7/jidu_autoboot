#!/usr/bin/env python3
"""Convert a built OpenWrt *-initramfs-kernel.bin (FIT) into handoff payload pieces.

Output (ready for autostart/build.sh): Image, part1.bin (=initrd), handoff.dtb
The DTB is prepared so the on-device stager can patch it in RAM at each boot:
  - wifi@18000000 disabled  (the old kernel's WiFi must not be inherited)
  - bootargs with loglevel + earlycon (so an early panic is never silent again)
  - linux,initrd-start/end present as 8-byte placeholders (the stager overwrites them)
"""
import lzma, os, struct, subprocess, sys, tempfile

def run(*a, **kw):
    return subprocess.run(a, check=True, capture_output=True, text=kw.pop('text', True))

def main(fit, outdir, no_initrd=False):
    os.makedirs(outdir, exist_ok=True)
    # 1. identify FIT parts by type
    listing = run("dumpimage", "-l", fit).stdout
    cur, types = None, {}
    for line in listing.splitlines():
        if line.startswith(" Image "):
            cur = int(line.split()[1])
        elif line.strip().startswith("Type:") and cur is not None:
            types[cur] = line.split(":",1)[1].strip()
    k = next(i for i,t in types.items() if "Kernel" in t)
    r = next((i for i,t in types.items() if "RAMDisk" in t), None)
    f = next(i for i,t in types.items() if "Tree" in t)
    print("FIT parts: kernel=%d initrd=%s fdt=%d" % (k, r, f))
    parts = {}
    for i in (k, r, f):
        if i is None: continue
        p = os.path.join(outdir, "part%d.bin" % i)
        run("dumpimage", "-T", "flat_dt", "-p", str(i), "-o", p, fit)
        parts[i] = open(p, "rb").read()
        print("  part%d: %d bytes" % (i, len(parts[i])))
    # 2. kernel -> uncompressed arm64 Image
    kd = parts[k]
    if kd[:3] == b'\x6d\x00\x00' or kd[:1] == b'\x5d':
        kd = lzma.decompress(kd, format=lzma.FORMAT_ALONE)
        print("  kernel: lzma-decompressed ->", len(kd))
    elif kd[:4] == b'\xfd7zXZ':          # xz
        kd = lzma.decompress(kd); print("  kernel: xz-decompressed ->", len(kd), )
    if kd[56:60] != b'ARM\x64':
        sys.exit("not an arm64 Image (magic %r at +56)" % kd[56:60])
    to, isz = struct.unpack('<QQ', kd[8:24])
    print("  Image: text_offset=0x%x image_size=0x%x" % (to, isz))
    open(os.path.join(outdir, "Image"), "wb").write(kd)
    # 3. initrd (unless we are booting a squashfs root, which needs none)
    if no_initrd:
        print("  initrd: skipped (squashfs-root mode)")
        rd = b""
    else:
      rd = parts[r]
    # the ramdisk is itself a compressed cpio (OpenWrt xz's it); the kernel can
    # unpack a compressed initramfs only if built with the matching RD_* option,
    # so hand it over as a plain cpio instead.
    if rd and rd[:6] == b'\xfd7zXZ\x00':
        rd = lzma.decompress(rd); print("  initrd: xz-decompressed ->", len(rd))
    elif rd and rd[:2] == b'\x1f\x8b':
        import gzip; rd = gzip.decompress(rd); print("  initrd: gunzipped ->", len(rd))
    elif rd and rd[:4] == b'\x28\xb5\x2f\xfd':
        import subprocess as sp
        rd = sp.run(["zstd","-dc"], input=rd, capture_output=True, check=True).stdout
        print("  initrd: zstd-decompressed ->", len(rd))
    if rd and rd[:6] not in (b'070701', b'070702', b'070707'):
        sys.exit("initrd is not a plain cpio after decompression (magic %r)" % rd[:6])
    if rd: print("  initrd: plain cpio ok")
    if rd:
        open(os.path.join(outdir, "part1.bin"), "wb").write(rd)
    else:
        p1 = os.path.join(outdir, "part1.bin")
        if os.path.exists(p1): os.unlink(p1)      # keep build.sh's detection honest
    # 4. device tree -> source -> patched -> dtb
    with tempfile.NamedTemporaryFile(suffix=".dtb", delete=False) as t:
        t.write(parts[f]); dtbpath = t.name
    src = run("fdtdump", "-s", dtbpath).stdout
    src = src[src.index("/dts-v1/;"):]                    # drop fdtdump's banner line

    def node_block(text, name):
        """Return (start, end) of the named node's body, found by brace nesting."""
        i = text.index(name + " {")
        j = text.index("{", i); depth = 0
        while True:
            if text[j] == "{": depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0: return i, j
            j += 1

    # The wifi node used to be force-disabled here, because the new kernel's
    # mt7915e failed ("Failed to get patch semaphore") and then panicked in
    # mt7915_mac_reset_work ~20s in whenever the old kernel's WiFi was still
    # live.  That disable was added together with the fix that actually matters
    # - the hook rmmod'ing mt7915e/mt76/mac80211/cfg80211 and bringing the links
    # down before firing - so the two were never isolated from each other.
    # 2026-09-21: tested with the node left ENABLED (identical payload otherwise,
    # kernel Image byte-identical) -> both firmware images load, phy0+phy1
    # register, board_detect generates a full 2-band /etc/config/wireless, and
    # an AP beacons at HE20/20dBm.  No semaphore error, no panic.  So the rmmod
    # is what makes the handoff safe and disabling the node just cost us both
    # radios.  Opt in with --wifi-off; default is to LEAVE IT ENABLED.
    if "--wifi-off" in sys.argv:
        try:
            a, b = node_block(src, "wifi@18000000")
            body = src[a:b]
            if "status = " in body:
                import re
                body2 = re.sub(r'status = "okay";', 'status = "disabled";', body, count=1)
                if body2 == body:
                    body2 = re.sub(r'status = "[a-z]+";', 'status = "disabled";', body, count=1)
            else:
                body2 = body + '            status = "disabled";\n'
            src = src[:a] + body2 + src[b:]
            print("  wifi@18000000 -> disabled (--wifi-off)")
        except ValueError:
            print("  WARNING: no wifi@18000000 node found (nothing disabled)")
    else:
        print("  wifi@18000000 -> left ENABLED (default)")

    import re
    # cpu@0 is the core the stock kernel was running on, and the handoff module
    # powers it off before the fire.  Left enabled in the DT, our kernel's SMP
    # bringup calls PSCI CPU_ON on it - the only PSCI call in the boot that
    # targets a core whose power state stock just changed; the resume once
    # claimed this core was off "by design" while the DT in fact re-enabled it.
    #
    # OPT-IN and off by default: the artifact we ship boots with cpu@0 ENABLED
    # (4 CPUs), which has a 40-fire clean record behind it, and an A/B cannot be
    # powered without a baseline failure.  Turning it on here would make a plain
    # rebuild silently change the boot path - the exact trap that cost time when
    # the module source was left on its park variant.  Build with --cpu0-off to
    # get the variant; artifacts for it live in autostart/out_sq_cpu0/.
    if "--cpu0-off" in sys.argv:
        try:
            a, b = node_block(src, "cpu@0")
            body = src[a:b]
            if "status = " in body:
                body2 = re.sub(r'status = "[a-z]+";', 'status = "disabled";', body, count=1)
            else:
                # body ends with the closing brace's indentation; drop it so the
                # inserted property and that brace keep their own alignment
                body2 = body.rstrip() + '\n            status = "disabled";\n        '
            src = src[:a] + body2 + src[b:]
            print("  cpu@0 -> disabled (--cpu0-off)")
        except ValueError:
            print("  WARNING: no cpu@0 node found (nothing disabled)")
    else:
        print("  cpu@0 -> left ENABLED (default)")

    if no_initrd:
        # squashfs-root boot: the kernel mounts our own UBI volume as root and
        # finds the writeable overlay by name.  ubi.mtd=5 is ubi2 in this DTB's
        # partition numbering; only that device is attached (nothing in the DTB
        # carries a linux,ubi marker, so the stock UBI is never touched).
        bootargs = ('bootargs = "dm-mod.create=\'\' loglevel=7 '
                    'earlycon=uart8250,mmio32,0x11002000 '
                    'ubi.mtd=5 ubi.block=0,1 root=/dev/ubiblock0_1 '
                    'rootfstype=squashfs rootwait";')
    else:
        bootargs = ('bootargs = "dm-mod.create=\'\' loglevel=7 '
                    'earlycon=uart8250,mmio32,0x11002000";')
    if re.search(r'bootargs = "[^"]*";', src):
        src = re.sub(r'bootargs = "[^"]*";', bootargs, src, count=1)
    if no_initrd:
        pass          # no ramdisk: the root comes from the squashfs volume
    elif "linux,initrd-start" not in src:
        add = ("    chosen {\n"
               "        linux,initrd-start = <0x0 0x0>;\n"
               "        linux,initrd-end = <0x0 0x0>;\n")
        if 'bootargs' not in src[src.index("chosen {"):src.index("chosen {")+400]:
            add += "        " + bootargs + "\n"
        src = src.replace("    chosen {\n", add, 1)
    # the chosen node must carry the (8-byte) initrd properties for the stager
    ca, cb = node_block(src, "chosen")
    if not no_initrd:
        assert "linux,initrd-start = <0x0 0x0>" in src[ca:cb], "initrd placeholders missing"
    else:
        print("  bootargs:", [l.strip() for l in src[ca:cb].splitlines() if "bootargs" in l][:1])
    dts = os.path.join(outdir, "handoff.dts")
    open(dts, "w").write(src)
    run("dtc", "-I", "dts", "-O", "dtb", "-o", os.path.join(outdir, "handoff.dtb"), dts)
    os.unlink(dtbpath)
    print("  handoff.dtb: %d bytes" % os.path.getsize(os.path.join(outdir, "handoff.dtb")))
    print("OK -> %s" % outdir)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], "--no-initrd" in sys.argv)
