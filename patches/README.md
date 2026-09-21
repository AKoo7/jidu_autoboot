# OpenWrt tree patches

The OpenWrt source changes this project depends on, as patches you can apply to a clean upstream tree
instead of copy-pasting code blocks out of `INSTALL_GUIDE.md` §3.

They are exported from the tree that booted the bench unit, against upstream base
**`820fbf4`**. Apply them **in numeric order** — 0002 and 0004 touch files that 0001 introduces.

| # | what it does |
|---|---|
| 0001 | Adds `Device/jiorouter_ax6000-jidu6801` (reusing the upstream `jidu6101` DTS — the hardware is identical) and bakes in the three files the image needs: the `99-jidu6801-lan` uci-default that keeps `192.168.31.1`, the `handoff-ack` init script the stock-side hook reads to clear its failure streak, and the `99-jidu6801-fix-macaddr` migration. |
| 0002 | **Required.** Fixes MAC retrieval in `board.d/02_network` for this board. Its `u-boot-env` MTD is a decoy with no `mac=` field and the live env carries none either, so `label_mac` came back empty and `macaddr_add` emitted the literal `":"` — which is non-empty, so the downstream guard passed it through and the box ran a **random MAC, new every boot**. Reads the factory MAC from `MFG` instead, gated on the model string. |
| 0003 | Makes the handoff boot token survive an abrupt power cut: the init script writes it without a `sync`, so a power cut loses it, the hook cannot see that our kernel booted, and it counts *successful* boots toward its 5-strike disarm — eventually stranding the box on stock. Adds the `sync` and guards that `/overlay` is the real mount. |
| 0004 | The upgrade path. A rootfs-only upgrade keeps `/overlay` by design, so a unit installed before 0002 keeps its `":"` MACs forever. This one-shot uci-defaults migration repairs that on the next boot, without re-deriving the values — it calls `board_detect` so the fixed `02_network` produces them, then propagates them into `/etc/config/network`. |

## Applying

```sh
git clone --depth 1 https://github.com/openwrt/openwrt.git openwrt && cd openwrt
git am /path/to/patches/*.patch        # keeps the commit messages and authorship
# or:  for p in /path/to/patches/*.patch; do git apply "$p"; done
```

Then build per `INSTALL_GUIDE.md` §3.

> ⚠️ **Upstream's `.gitignore` has `/files`** (it is a per-developer local overlay there), so the four files
> 0001/0003/0004 create under `files/` are **not tracked** after applying — `git status` looks clean while
> the files sit there untracked. If you want them in your own commit or fork, force-add them:
> `git add -f files/`. This is not cosmetic: it is also why a *change* under `files/` does not invalidate
> the image target, so `make` can regenerate `root.squashfs` byte-identical and silently ship the old file.
> Touch `target/linux/mediatek/image/{Makefile,filogic.mk}` to force it, and verify the marker **inside the
> built root**, not in the tree.

## Verified

Applied in order to a clean `820fbf4` worktree: all four apply without fuzz, and **all six paths they touch
are byte-identical to the tree that booted the unit** (the four `files/` additions plus
`base-files/etc/board.d/02_network` and `image/filogic.mk`).

These patches are from the publication copy of this repo, so unit-identifying values in the commit
messages use the same length-preserving markers as the rest of the tree (`[redact-mac-0x27]`, …). That is
why they still apply: a marker is exactly as wide as the value it replaced, so no diff context shifted.
