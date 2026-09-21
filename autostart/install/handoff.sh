#!/bin/sh
# JIDU6801 autonomous kernel handoff - the whole device-side sequence.
# Runs on STOCK linux only (our kernel has no such init).  FAIL-SAFE: any doubt
# exits 0 and stock continues.  Never writes flash.
DIR=/overlay/jidu6801
LOG=/tmp/handoff.log
exec >>$LOG 2>&1
say() { echo "[handoff] $*"; echo "$*" > /dev/console 2>/dev/null; }
say "=== attempt $(date 2>/dev/null) ==="

# ---- guards -----------------------------------------------------------
[ -x $DIR/jidu6801_stage ]  || { say "no stager"; exit 0; }
[ -f $DIR/jidu6801_handoff.ko ] || { say "no module"; exit 0; }
if [ -f $DIR/handoff-off ]; then say "disarmed ($DIR/handoff-off present)"; exit 0; fi

# failure-loop guard - deliberately CLOCK-FREE.  The stock clock has no RTC and
# NTP sets it to whatever it likes, so timestamps are useless here (an earlier
# version reset its counter on every boot because of that).
#
# streak = attempts since our kernel was last known to have booted.  Our kernel
# bumps /overlay/handoff-ack in its OWN rootfs_data volume (ubi1:2) on every
# boot; if that counter moved, the last handoff stuck and the streak clears.
# Only attempts that never reached our kernel count toward the 5.
mkdir -p /tmp/jhack
MTD=$(sed -n 's/^mtd\([0-9]*\):.*"ubi2".*/\1/p' /proc/mtd 2>/dev/null)
[ -n "$MTD" ] && ubiattach -m $MTD -d 1 2>/dev/null
STREAK=$DIR/streak; N=$(cat $STREAK 2>/dev/null || echo 0)
ACK=""
# mount rw: our kernel never gets to unmount cleanly (the handoff is a raw reset), so
# this volume always needs journal recovery, and a read-only UBIFS mount refuses to
# do it - it just fails.  Fall back to ro in case it is already clean.
if mount -t ubifs ubi1:rootfs_data /tmp/jhack 2>/dev/null || \
   mount -o ro -t ubifs ubi1:rootfs_data /tmp/jhack 2>/dev/null; then
	ACK=$(cat /tmp/jhack/handoff-ack 2>/dev/null || echo "")
	umount /tmp/jhack 2>/dev/null
fi
LAST=$(cat $DIR/ack 2>/dev/null || echo "")
# inequality, not ordering: the token carries a boot id, so it differs on every
# boot of our kernel even if that volume is ever wiped and the count restarts
if [ -n "$ACK" ] && [ "$ACK" != "$LAST" ]; then
	[ "$N" != 0 ] && say "our kernel booted (ack '$LAST' -> '$ACK') - failure streak cleared"
	N=0; echo "$ACK" > $DIR/ack
	# make our copy durable before the box can lose power.  If this write is
	# lost, LAST reads empty on the next boot, the comparison always fires, and
	# the streak can never accumulate - so the 5-strike auto-disarm would
	# silently never trigger (the guard would look healthy and do nothing).
	sync
fi
if [ "$N" -ge 5 ]; then
	say "$N attempts in a row never reached our kernel - DISARMED. delete $DIR/handoff-off to re-arm."
	echo disarmed > $DIR/handoff-off; echo 0 > $STREAK; exit 0
fi
N=$((N+1)); echo "$N" > $STREAK
say "attempt $N, failure streak $N of 5"

# ---- verify payload, load module, stage into RAM ----------------------
$DIR/jidu6801_stage --verify || { say "payload verify FAILED"; exit 0; }
insmod $DIR/jidu6801_handoff.ko || { say "insmod FAILED"; exit 0; }
$DIR/jidu6801_stage --stage > /tmp/stage.out 2>&1
cat /tmp/stage.out >> /dev/console 2>/dev/null
FIRE=$(sed -n 's/^FIRE //p' /tmp/stage.out)
if [ -z "$FIRE" ]; then
	say "staging FAILED:"
	while read -r l; do say "  $l"; done < /tmp/stage.out
	rmmod jidu6801_handoff; exit 0
fi
say "staged and verified; fire cmd: $FIRE"

# ---- escape window ----------------------------------------------------
# Wait until the LAN is actually up (so someone can SSH in and cancel), then
# hold the window open.  Without this the window can close before sshd exists.
i=0
while [ $i -lt 90 ]; do
	ip -4 addr show br-lan 2>/dev/null | grep -q "inet " && break
	i=$((i+3)); sleep 3
done
say "firing in 30s - to cancel: ssh root@192.168.31.1 ; touch $DIR/handoff-off"
sleep 30
if [ -f $DIR/handoff-off ]; then say "cancelled by handoff-off"; exit 0; fi

# ---- quiesce devices, park cores, fire --------------------------------
/etc/init.d/network stop >/dev/null 2>&1
wifi down >/dev/null 2>&1
for i in eth0 eth1 eth1-gmac lan1 lan2 lan3 lan4 br-lan home-ap-24 home-ap-50; do ip link set $i down 2>/dev/null; done
# the old WiFi must be dead or the new kernel's mt7915e dies in mac_reset_work
for m in mt7915e mt76_connac_lib mt76 mac80211 cfg80211; do rmmod $m 2>/dev/null; done
for c in 1 2 3; do echo 0 > /sys/devices/system/cpu/cpu$c/online 2>/dev/null; done
sleep 1
say "online cpus: $(cat /sys/devices/system/cpu/online)"
say "FIRING"
echo "$FIRE" > /proc/jidu6801_go
say "!!! module returned - it should never return !!!"
