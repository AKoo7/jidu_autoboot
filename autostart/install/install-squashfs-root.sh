#!/bin/sh
# Install or upgrade the squashfs root for the JIDU6801 handoff.
# RUN THIS ON STOCK - our kernel has this volume mounted as its own root, so it
# cannot rewrite it.  Usage:  ./install-squashfs-root.sh <root.squashfs> [size]
set -e
SQ=${1:?usage: $0 <root.squashfs> [owrt-root size, default 8MiB]}
SZ=${2:-8MiB}
[ -f "$SQ" ] || { echo "no such file: $SQ"; exit 1; }

MTD=$(sed -n 's/^mtd\([0-9]*\):.*"ubi2".*/\1/p' /proc/mtd)
[ -n "$MTD" ] || { echo "no \"ubi2\" partition in /proc/mtd"; exit 1; }
echo "ubi2 is mtd$MTD"
ubiattach -m "$MTD" -d 1 2>/dev/null || true
[ -c /dev/ubi1_0 ] || { echo "ubi1 (the payload volume) is not attached - nothing to do"; exit 1; }

# note: names only appear with -a (plain `ubinfo /dev/ubi1` prints device info only)
ubinfo /dev/ubi1 -a | grep -q "Name: *owrt-root"   || ubimkvol /dev/ubi1 -N owrt-root -s "$SZ" -t static

# rootfs_data must be FORMATTED BY STOCK.  Our kernel's UBIFS picks zstd as its
# default compressor, and the stock 5.4 kernel has no zstd - it fails with
# `ubifs_mount: compressor "zstd" is not compiled in`, which breaks the hook's
# ability to read this volume.  Empty volume + a mount here = stock's own format.
mkdir -p /tmp/jhfmt
if ubinfo /dev/ubi1 -a | grep -q "Name: *rootfs_data"; then
	if mount -t ubifs ubi1:rootfs_data /tmp/jhfmt 2>/dev/null; then
		umount /tmp/jhfmt
		echo "rootfs_data: already readable by stock"
	else
		echo "rootfs_data is not readable by stock (zstd?) - recreating it"
		ubirmvol /dev/ubi1 -N rootfs_data
		ubimkvol /dev/ubi1 -N rootfs_data -s 24MiB
		mount -t ubifs ubi1:rootfs_data /tmp/jhfmt && umount /tmp/jhfmt
		echo "rootfs_data: recreated and formatted by stock"
	fi
else
	ubimkvol /dev/ubi1 -N rootfs_data -s 24MiB
	mount -t ubifs ubi1:rootfs_data /tmp/jhfmt && umount /tmp/jhfmt
	echo "rootfs_data: created and formatted by stock"
fi

NAME=$(ubinfo /dev/ubi1_1 2>/dev/null | sed -n 's/.*Name: *//p')
[ "$NAME" = owrt-root ] || { echo "/dev/ubi1_1 is '$NAME', expected owrt-root (volume IDs are creation order)"; exit 1; }

echo "writing $(stat -c %s "$SQ" 2>/dev/null || wc -c <"$SQ") bytes into owrt-root"
ubiupdatevol /dev/ubi1_1 "$SQ"          # NOT dd - dd returns EPERM on this firmware
ubinfo /dev/ubi1_1 | grep -E "Name|Size|data bytes"

# a fresh rootfs is not a continuation of any failure streak
rm -f /overlay/jidu6801/streak /overlay/jidu6801/ack /overlay/jidu6801/attempts
rm -f /overlay/jidu6801/handoff-off     # re-arm
echo "installed.  reboot to boot it; a rootfs-only upgrade keeps /overlay untouched"
