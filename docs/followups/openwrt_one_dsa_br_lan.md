# Follow-up: OpenWrt One DSA bridge bug (`br-lan` does not include physical LAN ports)

## Status

Out of scope for the "CI builds real LibreMesh" hardening (that work added
manifest assertions in `tools/ci/build_image.sh`, a stricter `ShellDriver.prompt`
in `targets/*.yaml`, and an `assert_libremesh_runtime` guard in
`tests/conftest.py`). This page exists only as a parking lot to revisit once
those guards are green and the OpenWrt One actually boots the LibreMesh image
emitted by CI.

## Symptom (expected once we boot real LibreMesh on OpenWrt One)

On DSA-driven targets like the OpenWrt One, LibreMesh's default
`lime-proto-anygw` / `lime-network` plumbing creates `br-lan` but does **not**
attach the physical LAN switch ports to it. As a result:

- `br-lan` comes up with the LibreMesh address (e.g. `10.13.0.0/16`) but no
  packets ever cross from the LAN ports to the bridge — `ip -br link show
  master br-lan` is empty.
- `configure_fixed_ip` (single-node tests) sees the address on `br-lan` but
  ARP never resolves through the lab switch, so the SSH ProxyCommand via
  `labgrid-bound-connect <vlan>` times out at the TCP layer (the same banner
  exchange timeout we previously misattributed to lab/VLAN issues).
- `bat0` may still come up, so multi-node tests that rely solely on the
  batman-adv overlay (no LAN-port traffic) might pass even when single-node
  tests fail.

## Upstream context

- [libremesh/lime-packages PR #1203](https://github.com/libremesh/lime-packages/pull/1203)
  (DSA bridge fixes) and [PR #1214](https://github.com/libremesh/lime-packages/pull/1214)
  (followup) propose the canonical fix: teach `lime-network` how to enumerate
  DSA user ports per device profile and attach them to `br-lan`.
- The PRs are not merged at the time of writing. Upstream review surfaced
  device-specific edge cases (custom hostnames, profiles whose DSA ports are
  not named `lan*`, OEM bridges that should remain separate from `br-lan`).

## Options to evaluate (decision deferred until Fase 1 is green)

1. **Vendor the patch in `pi-lime-packages`.**
   - Pro: tests run against the same code we plan to ship; we control roll
     forward / roll back.
   - Con: maintenance debt — every rebase against upstream lime-packages
     needs a conflict pass on `lime-network`.
2. **Runtime workaround in `configure_fixed_ip`.**
   - After `lime-config` settles, detect any unmastered `lan*` interfaces
     and `bridge link set dev lan* master br-lan` from the serial console
     before attempting the SSH leg.
   - Pro: zero churn in `pi-lime-packages`; keeps the bug visible.
   - Con: hides a real LibreMesh bug behind test infrastructure; if anyone
     runs LibreMesh on OpenWrt One outside CI they still hit the original
     issue.
3. **Drop OpenWrt One from single-node matrix; keep it in mesh.**
   - Mesh tests exercise `bat0` over the radios, which is unaffected by the
     `br-lan`-without-LAN-ports bug.
   - Pro: clean separation, no patch to maintain.
   - Con: regressions in OpenWrt One's wired path go undetected by CI.

## When to revisit

After [.cursor/plans/ci_builds_real_libremesh_9dc03fcf.plan.md](../../.cursor/plans/ci_builds_real_libremesh_9dc03fcf.plan.md)
is fully implemented and green, re-run `tests/test_libremesh.py` against
OpenWrt One. The runtime guard added in `tests/conftest.py` will confirm we
booted real LibreMesh; if `test_br_lan_has_mesh_ip` then fails specifically
because `br-lan` has no member ports (visible via `bridge link show`), this
follow-up is the next thing to tackle. Otherwise (test passes), close this
page as obsolete.

## Diagnostic snippets

Confirm the bug is the DSA one and not something else:

```sh
# from the DUT serial console, post lime-config:
bridge link show
ip -br link show master br-lan
swconfig list 2>/dev/null   # empty on DSA targets, expected
ls /sys/class/net/ | grep -E '^(lan|wan)[0-9]*$'
```

Confirm `bat0` is up regardless (so mesh would still work):

```sh
batctl if
ip -br addr show bat0
```
