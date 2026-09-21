#!/bin/sh
# Swap the handoff payload + its matching stager, run ON OUR KERNEL (no stock boot needed).
#
# Two different UBI devices are involved, and this is the whole trick:
#   - the payload volume ("handoff") is on the ubi2 partition = OUR ubi0, so it is
#     /dev/ubi0_0 here and /dev/ubi1_0 under stock's numbering
#   - the stager lives in STOCK's overlay (rootfs_data on the 140 MiB "ubi"
#     partition), which our kernel does not attach - so attach it, mount, copy.
#
# jidu6801_stage hardcodes /dev/ubi1_0, which is only right under stock's
# numbering: it CANNOT be verified from here. The hook runs `--verify` itself on
# the next stock boot, so a payload/stager mismatch fails safe (the box stays on
# stock and logs "payload verify FAILED" instead of staging something wrong).
#
# The two files must already be on the device (stock's busybox has no sftp-server,
# so push them with `ssh <ip> 'cat > <path>' < local`). This script checks their
# md5s against the values you pass, so a truncated push cannot get written.
#
# usage: deploy_payload.sh <dev_payload> <dev_stager> <want_md5_payload> <want_md5_stager>
set -e
NEW=${1:?usage: $0 <dev_payload> <dev_stager> <md5_payload> <md5_stager>}
STG=${2:?}
WANT_P=${3:?}
WANT_S=${4:?}

echo "=== verifying the pushed files before touching anything ==="
for pair in "$NEW:$WANT_P:payload" "$STG:$WANT_S:stager"; do
	f=${pair%%:*}; rest=${pair#*:}; want=${rest%%:*}; what=${rest#*:}
	[ -f "$f" ] || { echo "missing $f"; exit 1; }
	got=$(md5sum "$f" | cut -d' ' -f1)
	[ "$got" = "$want" ] || { echo "$what md5 MISMATCH: got $got want $want - aborting"; exit 1; }
	echo "  $what ok ($got, $(wc -c <"$f") bytes)"
done

# --- 1. payload -> the handoff volume (our ubi0:0) -------------------------
NAME=$(ubinfo /dev/ubi0_0 2>/dev/null | sed -n 's/.*Name: *//p')
[ "$NAME" = handoff ] || { echo "/dev/ubi0_0 is '$NAME', expected 'handoff' - refusing"; exit 1; }
echo "=== writing the payload volume (/dev/ubi0_0, was: $NAME) ==="
ubiupdatevol /dev/ubi0_0 "$NEW"          # never dd: EPERM on this firmware
ubinfo /dev/ubi0_0 | grep -E "Name|data bytes"

# --- 2. stager -> stock's overlay -----------------------------------------
MTD=$(sed -n 's/^mtd\([0-9]*\):.*"ubi"[[:space:]]*$/\1/p' /proc/mtd)
[ -n "$MTD" ] || { echo 'no "ubi" partition in /proc/mtd'; exit 1; }
echo "=== installing the stager into stock's overlay (attach mtd$MTD as ubi1) ==="
ubiattach -m "$MTD" -d 1 2>/dev/null || true
mkdir -p /mnt/stock
mount -t ubifs ubi1:rootfs_data /mnt/stock
echo "  old stager: $(md5sum /mnt/stock/jidu6801/jidu6801_stage 2>/dev/null | cut -d' ' -f1)"
cp "$STG" /mnt/stock/jidu6801/jidu6801_stage
chmod 755 /mnt/stock/jidu6801/jidu6801_stage
sync
echo "  new stager: $(md5sum /mnt/stock/jidu6801/jidu6801_stage | cut -d' ' -f1)"
ls -l /mnt/stock/jidu6801/
umount /mnt/stock
ubidetach -d 1

echo
echo "DONE. The hook verifies the payload on the next stock boot:"
echo "  success -> 'VERIFIED: staging holds the payload (...)' on the console"
echo "  mismatch -> box stays on stock, 'payload verify FAILED' in /tmp/handoff.log"
