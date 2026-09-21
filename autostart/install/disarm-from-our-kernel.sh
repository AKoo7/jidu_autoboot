#!/bin/sh
# Disarm the JIDU6801 kernel handoff FROM OUR KERNEL (squashfs-root mode).
#
# In squashfs mode our kernel's /overlay IS the rootfs_data volume on the 90 MiB
# "ubi2" partition, while the boot hook's files live in STOCK's rootfs_data
# volume on the 140 MiB "ubi" partition.  This kernel does not attach that UBI,
# so the old "mount ubi0:rootfs_data" recipe silently writes to the wrong
# volume.  Attach stock's UBI (by mtd index, found by name - the numbering
# differs between the stock and our kernel), flag the hook off, detach.
set -e
MTD=$(sed -n 's/^mtd\([0-9]*\):.*"ubi"[[:space:]]*$/\1/p' /proc/mtd)
[ -n "$MTD" ] || { echo "no \"ubi\" partition in /proc/mtd"; exit 1; }
echo "attaching stock's UBI (mtd$MTD) as ubi1"
ubiattach -m "$MTD" -d 1 2>/dev/null || true     # -d 1 is free: our volume is ubi0
mkdir -p /mnt/stock
mount -t ubifs ubi1:rootfs_data /mnt/stock
mkdir -p /mnt/stock/jidu6801
touch /mnt/stock/jidu6801/handoff-off
rm -f /mnt/stock/jidu6801/streak /mnt/stock/jidu6801/attempts
ls -l /mnt/stock/jidu6801/
sync
umount /mnt/stock
ubidetach -d 1
echo "handoff DISARMED - a reboot now lands on stock and stays there"
echo "re-arm from stock with:  rm /overlay/jidu6801/handoff-off"
