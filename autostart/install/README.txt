JIDU6801 autonomous kernel handoff   (planted 2026-09-21, squashfs root 2026-09-21b)
====================================================================================
On every boot, stock Linux hands off to our OpenWrt 6.18.52 kernel.  The kernel is
staged in RAM; the root filesystem is a squashfs on UBI with a persistent overlay,
so changes survive reboots.  Nothing in the signed chain is touched: the handoff
never flashes, and a power cycle returns to stock.

HOW IT WORKS (all verified live)
  /etc/rc.d/S99zhandoff -> ../init.d/handoff    boot hook (runs after idu-ssh)
  /overlay/jidu6801/handoff.sh                  the sequence:
      verify payload crcs -> insmod -> stage into RAM -> 30 s escape window
      -> stop netifd/wifi, rmmod the old wifi, park cpu1-3 -> fire
  /overlay/jidu6801/jidu6801_stage              device-side stager: reads the
      module's staging base, streams Image+DTB into /dev/mem (patching
      linux,initrd-* in RAM only when the payload carries a ramdisk), then reads
      the whole run back and verifies it (payload matches, gaps zero).  Flash is
      only ever opened read-only.
  /overlay/jidu6801/jidu6801_handoff.ko         staging module (owns the
      contiguous RAM run, drives PSCI CPU_ON/CPU_OFF)
  /overlay/jidu6801/streak                      attempts since our kernel last booted
  /tmp/handoff.log, /tmp/stage.out              logs of the last attempt

VOLUMES (all on mtd6/ubi2 = /dev/ubi1 on stock)
  0 "handoff"     dynamic 18 MiB  payload: Image | handoff.dtb  (no initrd)
  1 "owrt-root"   static   8 MiB  the squashfs root, mounted via root=/dev/ubiblock0_1
  2 "rootfs_data" dynamic 24 MiB  our kernel's overlay - THIS is what persists
  Payload:   ubiupdatevol /dev/ubi1_0 payload.bin        # not dd - dd gets EPERM
  Rootfs:    rewrite from STOCK only - our kernel has it mounted as its root.

FAILURE GUARD: clock-free.  Our kernel bumps "/overlay/handoff-ack" in ITS OWN
  rootfs_data (volume 2) on every boot; the hook mounts that volume read-only and
  clears its streak whenever the counter moved.  So only attempts that never
  reached our kernel count, and 5 of those in a row disarm the trigger.  (The
  stock clock has no RTC and NTP sets it to whatever it likes, which is why this
  is not timestamp-based.)

OUR KERNEL, once booted
  OpenWrt SNAPSHOT r0-820fbf4, kernel 6.18.52, 3 CPUs (cpu0 is powered off by
  design), ssh root@192.168.1.1 with NO password, console on the serial port.
  Note: in squashfs mode the /overlay here is volume 2 above - NOT this directory.

TO STOP IT / GET BACK TO STOCK  (any one of these)
  1. During the 30 s window after a boot starts (the console says so):
        ssh root@192.168.31.1
        touch /overlay/jidu6801/handoff-off
  2. From our kernel once booted, squashfs mode - it attaches stock's UBI first:
        install/disarm-from-our-kernel.sh
     (initramfs mode, where our ubi0:rootfs_data IS this volume:
        mount -t ubifs ubi0:rootfs_data /mnt; touch /mnt/jidu6801/handoff-off)
  3. Sit and wait: 5 attempts in a row that never reach our kernel disarm it.
RE-ARM:      rm /overlay/jidu6801/handoff-off   (and rm streak to reset the count)
REMOVE ALL:  rm -f /etc/rc.d/S99zhandoff /etc/init.d/handoff
             rm -rf /overlay/jidu6801
             ubirmvol /dev/ubi1 -N handoff; ubirmvol /dev/ubi1 -N owrt-root
             ubirmvol /dev/ubi1 -N rootfs_data
