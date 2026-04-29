# Follow-up: Belkin RT3200 with corrupted `factory` partition after KOD recovery

## Status

Worked around in `targets/linksys_e8450.yaml` by replacing the U-Boot
`dhcp` init command with `tftpboot $loadaddr $bootfile`. This sidesteps
the corrupted-MAC failure mode for any Belkin RT3200 unit whose
`factory` SPI-NAND partition was damaged by the layout 1.0 KOD episodes
(see `pi-lime-packages/docs/followups/belkin_rt3200_layout_1_0_dtb_patch.md`
for the layout 1.0 / 24.10 root cause and the DTB patch that prevents
new KODs).

The change is local to this repo (lab-side test runner config) and
needs no firmware rebuild. Once any affected unit gets its `factory`
partition restored (see "Restoring `factory` manually" below), the
workaround keeps working — `tftpboot` with pre-set `serverip`/`ipaddr`
is strictly faster and more deterministic than `dhcp`.

## Symptom (CI run 25061360737, `belkin_rt3200_3`)

> `dtb_force_legacy_partitions: true` is in effect (no more KOD), the
> CI build is healthy and identical to the artifacts that booted on
> `belkin_rt3200_1` and `belkin_rt3200_2` in the same run, but
> `belkin_rt3200_3` consistently times out at the `transition("shell")`
> step. The labgrid log shows the U-Boot prompt is reached, the
> strategy injects `setenv serverip/ipaddr/bootfile/bootargs/bootconf`
> successfully, then on `dhcp` the DUT loops:
>
>     BOOTP broadcast 1
>     BOOTP broadcast 2
>     ...
>     BOOTP broadcast 17
>     Retry time exceeded; starting again
>
> ~30 s later the MT7622 watchdog (which U-Boot enables but cannot pet
> during a BOOTP retry storm) fires, the device resets, the strategy's
> one-shot interrupt-spam window has closed, and the U-Boot autoboot
> proceeds uninterrupted to the on-flash OpenWrt 23.05.5 recovery
> firmware. The strategy keeps sending U-Boot commands like `dhcp`
> while the DUT is now in a Linux shell that just echoes them and
> emits its `root@OpenWrt:/#` banner — eventually
> `pexpect.exceptions.TIMEOUT` matches `MT7622>` against
> `root@OpenWrt:/#` and `transition("shell")` fails.
>
> Other Belkin units (1 and 2) on the SAME CI run completed `dhcp` in
> ~40 ms and passed all tests.

## Root cause

The `factory` MTD partition on Belkin RT3200 layout 1.0 lives at
`0x1c0000-0x2c0000` and stores the OEM-provisioned WiFi calibration
EEPROM plus the two factory MAC addresses at offsets `0x7fff4`
(`mac@7fff4` → `eth0` / `wmac`) and `0x7fffa` (`mac@7fffa` → `wmac1`).
On any unit that survived a 24.10 KOD episode before
`dtb_force_legacy_partitions: true` shipped, the kernel's
attached-at-`0x80000` UBI scribbled UBI EC headers across that whole
range. `mtk_uartboot` plus the U-Boot recovery menu (options 7 + 8 in
the rescue procedure documented at
`pi-lime-packages/docs/followups/belkin_rt3200_layout_1_0_dtb_patch.md`)
only re-flashes `bl2` and `fip` from the 23.05.5 release; it does NOT
touch `factory`. So the recovered unit boots to a healthy U-Boot,
but `nvmem-cells` resolved against `factory` see UBI noise instead of
real OEM bytes.

Confirmation from the failing CI run's kernel log — the second
autoboot (the one the strategy did NOT manage to interrupt) reports:

    [    0.893924] mtk_soc_eth 1b100000.ethernet: generated random MAC address a2:97:fe:14:8a:b3
    [    6.872330] mt7622-wmac 18000000.wmac: Invalid MAC address, using random address c6:11:c5:18:25:3d

`generated random MAC address` for `mtk_soc_eth` is the Linux-side
smoking gun: the kernel could not find a usable MAC bytestring at the
nvmem offsets the DTS points to. Belkin 1 and 2 in the same run do
NOT print that line — their `factory` is intact, so `mtk_soc_eth`
silently picks up the OEM MAC.

The U-Boot side reads from the same MTD region via `local-mac-address`
in `&snand`'s `partitions { factory: ... }` subnode. With UBI EC
headers occupying those offsets the resulting MAC is essentially a
hash of UBI's internal counters — not a manufacturer-allocated OUI.
The lab's dnsmasq is configured with per-MAC reservations
(`dhcp-host=<MAC>,<IP>` for each registered DUT, which is how
`belkin_rt3200_2` ends up at `192.168.101.179` on every boot), so a
DHCP request from an unregistered MAC gets no reply — hence the
17-broadcast retry storm on Belkin 3.

## Why the workaround works

`strategies/tftpstrategy.py::_prepare_uboot_commands` ALREADY injects
both `setenv serverip <exporter_ip>` and `setenv ipaddr <exporter_ip+1>`
before the per-target `init_commands` run, and `bootfile` is set
from the staged path. With those three env vars in place,
`tftpboot $loadaddr $bootfile` only needs:

1. ARP for `serverip` — works fine even with a random/zero source
   MAC; the exporter responds based on Layer-3 dst IP and the lab
   switch is L2-transparent for outbound frames regardless of source
   MAC byte values.
2. UDP TFTP transfer to the staged file — the TFTP server replies to
   the IPv4 source address (`ipaddr`), not to a DHCP-allocated lease.

There is no DHCP request, no MAC reservation lookup, no BOOTP retry
storm and no watchdog-triggered reset. The TFTP transfer also runs
~3-4 s faster on a healthy unit because there is no BOOTP round-trip
to wait for, which is a small free win for Belkin 1 and 2 on top of
unblocking Belkin 3.

The rest of the boot flow (the `ubi part ubi || true` /
`ubi remove rootfs_data || true` cleanup, then `bootm
$loadaddr#$bootconf`) is unchanged.

## Why we did NOT replicate this on `openwrt_one` and `bananapi_bpi-r4`

Both targets keep `dhcp` for now. Their `factory`/MAC NVMEM regions
have never been touched — they were never in KOD — and `dhcp`
completes in ~40 ms there, which is the same wall-clock as
`tftpboot` because the bottleneck on those units is the post-link-up
STP convergence on the lab switch, not the DHCP round-trip itself.
Touching them now would be premature change for no observable
benefit and would risk regressing two healthy paths to fix one
unhealthy one. If those units ever exhibit a `factory`-side problem
(or if dnsmasq config drifts), the same one-line replacement is the
known-good remediation.

## Restoring `factory` manually (optional, for full WiFi calibration recovery)

The CI workaround is sufficient for the test runner. The unit can
still boot LibreMesh, and the kernel/wmac fall back to a random MAC
which is acceptable for `test_libremesh.py` / `test_base.py` /
`test_lan.py` where wireless calibration is not exercised. If/when a
real WiFi test on the affected unit is needed, the persistent fix is:

1. Boot the unit into the OpenWrt 23.05.5 recovery initramfs (the
   `openwrt-23.05.5-mediatek-mt7622-linksys_e8450-ubi-initramfs-recovery.itb`
   file used during `mtk_uartboot` recovery).

2. From the recovery shell, identify the `factory` MTD partition:

       cat /proc/mtd

   On layout 1.0 the relevant entry is `factory` at MTD index 2
   (offsets `0x1c0000-0x2c0000`, size `0x100000`).

3. If you have a backup of that unit's original `factory` blob (taken
   before the KOD), restore it verbatim:

       mtd write /path/to/factory.bin /dev/mtd2

   You almost certainly do NOT have such a backup — the lab never
   captured it pre-KOD. In that case proceed with synthetic restore
   below.

4. Synthetic restore (good enough to make `dhcp` work again with the
   lab's dnsmasq and to give wmac a stable, lab-known MAC; WiFi
   regulatory calibration cannot be reconstructed without the OEM
   blob and the unit will continue to operate at default TX power /
   default rates):

       # Erase the partition first.
       mtd erase /dev/mtd2

       # Pick TWO locally-administered, lab-unique MACs. The
       # locally-administered bit is the second-least-significant of
       # the first byte (so 02:..). Avoid 00:00:00:00:00:00 (rejected
       # by some switches) and avoid the OUI ranges of any
       # equipment already in the lab.
       MAC0="02:11:22:33:44:53"   # eth0 / mtk_soc_eth   (offset 0x7fff4)
       MAC1="02:11:22:33:44:54"   # wmac                 (offset 0x7fffa)

       # Write the two 6-byte MACs at the offsets the DTS points at.
       printf "$(printf '\\x%s' ${MAC0//:/ })" |
           dd of=/dev/mtd2 bs=1 seek=$((0x7fff4)) conv=notrunc
       printf "$(printf '\\x%s' ${MAC1//:/ })" |
           dd of=/dev/mtd2 bs=1 seek=$((0x7fffa)) conv=notrunc

   The `eeprom_factory_0` (`0x0-0x4da8`) and `eeprom_factory_5000`
   (`0x5000-0x5e00`) regions stay erased (`0xff` after `mtd erase`).
   `mt76` driver treats an all-`0xff` EEPROM as "no calibration",
   prints `Invalid MAC` (now harmless because we wrote `mac@7fffa`
   above), and falls back to safe defaults — no panic, no probe
   failure.

5. Register the new MACs with the lab's dnsmasq if you want DHCP to
   work for that unit (this is what unblocks `dhcp` again, but the
   CI workaround above keeps working regardless):

       dhcp-host=02:11:22:33:44:53,192.168.10X.YYY,belkin_rt3200_N

   where X is the per-place subnet (100/101/102 for B1/B2/B3 in this
   lab) and YYY is the static IP you want assigned.

6. Power-cycle the unit through `mtk_uartboot` recovery one more time
   if you want to also re-flash the on-flash recovery firmware to a
   known state. Not required: the on-flash 23.05.5 we currently have
   is fine; we just don't autoboot to it because the CI strategy
   always TFTP-boots its own initramfs.

## Future cleanup

When all Belkin units in the lab are confirmed restored and registered
in dnsmasq, the `tftpboot $loadaddr $bootfile` line can stay (it is
strictly an improvement) or be reverted to `dhcp` for symmetry with
the other targets — both paths are fine. Reverting is a one-line
change and would re-test the dnsmasq reservation as a side effect.

If we later move the lab to a fully isolated TFTP-only subnet (no
DHCP server at all), `tftpboot` becomes the only option that works
and the same change should be applied to `openwrt_one.yaml` and
`bananapi_bpi-r4.yaml`. That is out of scope for this fix.
