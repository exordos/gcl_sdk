# SDN dataplane integration tests

Fast, VM-less integration tests for the `ovs_evpn` NF dataplane. They drive
the **real** agent code — the `evpn` driver's flow installers and the
`evpn_nf` responder (run as a subprocess) — against **real OVS + real
network namespaces**, so DHCP, DNS, the netboot/metadata proxy, security
groups and the per-guest patch/VLAN/metadata wiring (and its GC) are
exercised end to end in seconds. No VMs; forwarding through the external
`evpn_connector` is out of scope (that is the slow, stand-based tier).

Each test builds an isolated `br-int` fabric under a unique prefix and tears
everything down afterwards, so runs are parallel-safe-ish and leave nothing
behind.

## Requirements

Root, plus `ovs-vsctl`, `ovs-ofctl`, `ip`, `conntrack`, `dhclient` and
`dig` on the host. The suite auto-skips (per test / per session) when these
are missing, so a normal non-root `tox`/`pytest` run simply reports them
skipped. The proxy binds `:80` inside each VNI's namespace, so a host that
already serves `:80` does not collide with it.

Root runs **must** use `../run-isolated.sh`. It creates private mount, network,
PID and UTS namespaces, copies all of `/etc` (with a regular private
`resolv.conf`), hides the entire host `/run`, makes `/opt` and `/var/lib`
read-only per mount, isolates writable DHCP state, and starts
its own OVS database and switch. Stock DHCP hooks can then write DNS state
and invoke system tools without reaching the host's resolved/DBus sockets.
The fixtures reject root execution outside this runner. Merely naming bridges
or entering a network namespace does **not** isolate DHCP's filesystem writes.

Driver paths are additionally redirected into each test's temporary directory:
bridge names, the VLAN map, NF records, client configs and published addresses.
A new driver path must stay under that directory too.

## Running

```bash
# Build the environment without running networking tests.
tox -e sdn-integration --notest
sudo -E SDN_REQUIRE_ENV=1 gcl_sdk/tests/integration/run-isolated.sh \
  .tox/sdn-integration/bin/python -m pytest \
  gcl_sdk/tests/integration/test_run_isolated.py \
  gcl_sdk/tests/integration/sdn -q
```

The runner additionally requires `unshare`, mount tools, `ovsdb-tool`,
`ovsdb-server`, `ovs-vswitchd` and `/usr/share/openvswitch/vswitch.ovsschema`.
`test_run_isolated.py` checks the boundary before running a stock DHCP script
with fake DNS. It restores its private changes before the tier continues.
Do not run this tier with bare root `pytest` or root `tox`.

## What each file covers

Every module's docstring says what it is for and, where it matters, what it
deliberately does *not* claim; the table is an index, not a substitute.

### The wiring

 file | covers
------|--------
 `test_patch_gc.py`    | per-guest patch + metadata creation and full GC
 `test_resolve_guest_port.py` | libvirt contract: guest resolution by `iface-id` only (never `attached-mac`), retriable when unplugged
 `test_many_ports_on_one_host.py` | the host's pool of 4094 local VLANs: no collisions, numbers returned when a guest leaves, a full host refusing rather than reusing, and eight real ports coexisting
 `test_reapply_is_a_no_op.py` | applying what is already applied changes nothing
 `test_restart_does_not_disturb.py` | an agent or responder that restarts does not renumber ports or rebuild what it finds wired
 `test_restore_drift.py` | the snapshot reads the host it is given

### What a guest is told

 file | covers
------|--------
 `test_nf_dhcp.py`     | static mac→ip lease from a CP record; no record ⇒ no lease; netboot filename; a lease survives default-deny
 `test_nf_dhcp_options.py` | the rest of the lease: the overlay MTU, the resolver, the lifetime, and a guest holding a stale address being corrected
 `test_nf_dns.py`      | internal A/PTR zone, per-VLAN scoping, fail-closed on unknown guest
 `test_nf_no_interruption.py` | the responder's own reconcile does not interrupt what it serves
 `test_nf_proxy.py`    | netboot cache serving, allowlist (404 + traversal), overlapping-CIDR isolation
 `test_metadata_identity.py` | a guest is told about its own machine and refused its neighbour's — with both at the same address in different segments, so only the segment's records can tell them apart

### What a guest may do

 file | covers
------|--------
 `test_security_group.py` | stateful allowlist on real traffic, reconcile, established return, per-guest scoping, and no gap while a group's rules are rewritten
 `test_sg_services.py` | DHCP, DNS and netboot behind an explicit SG; CIDR ingress filtering
 `test_identity_group.py` | a rule that names a group compiles to the mark and names no address (enforcement between guests lives in the fabric tier)
 `test_established_sessions.py` | what revoking a rule reaches — and, recorded rather than judged, what it leaves running
 `test_related_traffic.py` | the errors a permitted conversation generates reach its sender
 `test_port_security.py` | a guest may only be itself: neighbour's address, neighbour's MAC, forged ARP, and IPv6 the allow-list cannot describe
 `test_conntrack_zones.py` | one tenant's open connection is not another's way in, at identical five-tuples

### Where a guest's traffic goes

 file | covers
------|--------
 `test_vni_egress.py`  | per-VNI DVR egress: snat namespace, double masquerade (conntrack-asserted), idempotency, the guard, published addresses, GC
 `test_egress_nat.py`  | the round trip through both masquerades, and two guests on one source port told apart
 `test_path_mtu.py`    | a packet too big for the path is answered with the size that fits, and the guest records it
 `test_overlapping_cidr_isolation.py` | the same address in two segments answers from its own
 `test_fip_ingress.py` | a floating address: who answers for it, what it lets in, and what it does not

## Conventions worth keeping

*   **Every case fails on an unexpected driver log line.** `conftest.py`
  attaches a collector to the `gcl_sdk` logger for every test in the tier;
  anything at WARNING or above that is not on the short allow-list fails the
  case that provoked it. A test that means to provoke one says so with
  `@pytest.mark.expect_logs(...)`. Borrowed from OVN, whose every test fails
  on an unexpected line in a daemon log.
*   **A case measured as silence needs a control.** "The neighbour heard
  nothing" is also what a broken port produces, so the modules that assert a
  refusal also assert that the same guest, behaving, gets through.
*   **Prove the case can fail.** Several modules record that they were checked
  against the defect they exist for — port security with the pinning off,
  the related-traffic cases without `+rel`, the zone cases compiled into one
  shared zone. If a new case cannot be made to fail, it is not yet a test.

`harness.py` is the fixture/helper layer (`SdnHarness`, `Guest`);
`conftest.py` provides the `fabric` fixture, the environment skip-guard and
the log collector.
