# SDN full-fabric integration tests

The heaviest, most complete SDN tier: the **entire** EVPN control and data
plane on a single host, all real components — no VMs, no mocks.

Where the NF tier (`../sdn`) exercises the agent's flow installers and the
`evpn_nf` responder but stops short of EVPN forwarding, this tier adds the
real external forwarder stack and drives traffic through it:

- an **RR `gobgpd`** — config rendered by the real `BgpRr` renderer
  (passive dynamic-neighbors, GR/LLGR) — on a dedicated underlay address;
- a **host `gobgpd`** — config from the real `EvpnHost` renderer;
- the **real `evpn_connector` daemon** — config from the real `EvpnHost`
  connector renderer — owning the test EVPN bridge and consuming the
  client configs the real `EvpnPort` driver writes.

Guests are wired by the **real `EvpnPort` driver** (`dump_to_dp`): the veth
lands on `br-int` with `external_ids:iface-id` set (as libvirt's
openvswitch virtualport does), the driver resolves it, assigns the
per-guest VLAN, builds the patch pair, writes the client config/NF record
and the per-VNI DVR egress namespace.

`test_full_datapath.py` asserts, on this stack:

- the host↔RR BGP session establishes and the guests' EVPN Type-5 `/32`s
  reach the RIB;
- the connector compiles the clients into flows on the EVPN bridge;
- guests forward **both ways** through the connector's L3 pipeline
  (ARP-proxy router MAC, per-guest patches) and reach the egress gateway.

`test_full_datapath.py` is a single host with a single connector, so every
prefix is local. Cross-host EVPN forwarding and the fail-static guarantee
(which only bite on *remote*, reflected routes) are covered by
`test_two_host.py`.

## Two-host cross-host forwarding (`test_two_host.py`)

Two full hosts on this one machine, plus an RR, all real. To let two
otherwise-identical stacks coexist in one kernel, each host runs in its own
network namespace with its **own** OVS instance (a private `ovsdb-server` +
`ovs-vswitchd` on a per-host `OVS_RUNDIR`, hence a private per-netns
datapath), its own `gobgpd`, and the real `evpn_connector`. The namespaces
isolate everything, so both hosts reuse identical names/ports (`evpn`,
`br-int`, gRPC `:50051`, the connector's `vxlan_out`); only filesystem
paths differ. A third namespace holds the RR; the underlay is a plain Linux
bridge in the root namespace. Guests are wired by the **real `EvpnPort`
driver**, executed inside the owning host's namespace (`_port_apply`) so
every OVS/ip command lands in that host's namespace and OVS.

`test_cross_host_forwarding_and_fail_static` asserts:

- both host `gobgpd`s peer the RR and each learns the *other* host's guest
  as a remote, RR-reflected route;
- a guest on host A reaches a guest on host B over VXLAN, both ways;
- after the RR is killed with a BGP NOTIFICATION, cross-host forwarding
  **survives** (fail-static keeps the last-known-good remote routes).

`test_a_guest_that_is_deleted_stops_being_reachable` asserts the other half
of that guarantee: fail-static is only safe while the fabric can still tell
"the reflector is gone" from "the guest is gone". A port deleted on host B
is withdrawn through the RR, disappears from host A's RIB, and stops being
reachable — while a second guest of host B keeps both its route and its
reachability, so the withdrawal is shown to reach exactly one port. The
deleted guest's namespace is left in place, so a ping that stops has to stop
because the fabric forgot the route and not because nobody was left to
answer.

The tests use gateway-less subnets on purpose: same-subnet cross-host
traffic is pure connector L3 (per-guest ARP proxy + `/32` over VXLAN), so
no per-VNI DVR egress is created — its OVS/netns names are per-VNI and
would collide between the two hosts sharing one kernel; egress is covered
by the single-host tier instead.

It additionally needs `ovsdb-server`/`ovsdb-tool`/`ovs-vswitchd` and the
OVS schema to run the private OVS instances; it auto-skips without them.

## One-time setup

The prepare script builds an `evpn_connector` venv on the host python and
fetches the gobgp binaries into `~/.cache/exordos-sdn-fabric` (override with
`SDN_FABRIC_CACHE`). The version is pinned to the one `exordos_core`'s
`install.sh` ships, and the tarball is checksummed the same way — **keep the
two in step**. A tier that proves the fabric on a different daemon than the
one deployed proves it about nothing, and the gRPC service was renamed
between gobgp 3 and 4, so a mismatched pair does not behave differently, it
does not talk at all:

```bash
EVPN_CONNECTOR_PATH=/path/to/evpn_connector \
  gcl_sdk/tests/integration/sdn_fabric/prepare.sh
```

The checkout has to carry what this tier exercises — fail-static, the GBP
carrier, and the dependency band that installs on a current interpreter.
Those live on the connector's `sdn-min` branch until they land on its
`master`, which is also what CI checks out.

## Running

```bash
sudo .tox/sdn-fabric/bin/pytest gcl_sdk/tests/integration/sdn_fabric
# or, directly against a venv that has the test deps:
sudo .venv/bin/python -m pytest gcl_sdk/tests/integration/sdn_fabric
```

Requires root plus `ovs-vsctl`/`ovs-ofctl`/`ip`/`conntrack` and the cache
above; the suite auto-skips (per session) when any are missing, so an
ordinary non-root `tox`/`pytest` run simply reports it skipped.

Deviations from production: the harness supervises the daemons directly
(systemd's job on a real host), and the single-host tier moves the gobgp
gRPC channel off the port the renderer names.

That last one is not cosmetic. The rendered channel is the port a deployed
installation's own `gobgpd` holds, so on a machine that runs one, this
tier's daemon could not bind, died at once, and every query answered from
the *live* daemon — which has its own peers, its own RIB, its own prefixes.
Everything passed, about a fabric the tier had not built, and it stayed
silent until an unrelated version skew made the same collision fail loudly.
So the daemons now take ports the kernel says are free, the connector's
config is redirected at the one line naming the channel (the renderer's own
value is asserted first, so production's channel stays tested), and nothing
is asserted until the processes we started are confirmed running.

If this tier ever fails in a way that makes no sense, suspect that first:
check what else on the machine holds the ports and paths it wants.
