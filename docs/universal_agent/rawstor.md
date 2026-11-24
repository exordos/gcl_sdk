# Rawstor drivers

The SDK provides three capability drivers: `StorageClusterAgentDriver` runs MDS
on Core, `StorageNodeAgentDriver` runs OSTs on storage hosts, and
`ExordosLocalHyperDriver` attaches remote objects to virtual machines.

## Cluster and OST configuration

Each cluster runs a separate `rawstor-mds@<cluster-uuid>.service` with its own
persistent SQLite index and topology. The public
`RawstorStorageClusterDriverSpec` contains only `kind` and the MDS `endpoint`.
Core constructs a `RawstorStorageClusterAgentSpec` snapshot with UUID-keyed
`nodes` and `pools` from the separate node and pool resources.

Pool snapshots use a typed nested schema. `tier` and `failure_domain` are enums,
`ephemeral` is boolean, and `mirrors` ranges from 1 to 255. Chunk size is fixed
internally at 1 GiB and is not part of pool configuration. Invalid payloads fail during
parsing, before configuring services or querying capacity.

Drivers use the packaged `rawstor-mds@.service` and `rawstor-ost@.service`
templates, configured under `/etc/rawstor/mds` and `/etc/rawstor/ost`.
OSTs register themselves and send heartbeats to MDS. Their configuration uses
`MDS`, `ADVERTISE` and `DOMAIN_PATH`; the internal weight is fixed at 100.
The MDS database owns topology. There is no topology file or SIGHUP topology
reload. MDS uses `DB_URI=sqlite:///.../mds.db` and advertises its own witness.
Unregistering a cluster retains its database; stopping an OST sends a leaving
registration and retains backing data. Local agents check systemd liveness.

The OST driver prepares dedicated file-backed directories. Other native
backends, including `lvm://`, must be provisioned externally and use a root
service drop-in. Rawstor validates the native backing URI. File-backed OSTs
run as `rawstor`.

## Placement and capacity

OST failure-domain paths run from outermost to innermost
(`dc/row/rack/server`). Managed OST topology entries have roles `data,witness`.
The MDS is also advertised as `ost://` with role `witness`. Its topology UUID
is derived from the cluster UUID, while its server failure domain is the Core
node UUID. The witness advertises the same IP and port as the MDS listener.

The MDS witness contributes no data capacity. Two OSTs can host two data copies
with MDS supplying the witness vote for a writable majority.

Pool policies share physical OST capacity; their availability figures must not
be added together. The driver reports OST inventory and completed MDS objects.
`storage_capacity.available_by_policy` derives a conservative placement budget
accounting for mirrors, failure domains and pending disks. Unreachable OSTs and OSTs not yet registered with MDS
contribute zero available space. MDS remains the final allocation authority.

`MachineVolume.storage_policy` carries the assigned pool UUID, mirror count,
failure domain through reconciliation. The Core agent creates accounts and
objects through `storage_volume` resources, using a fixed 1 GiB chunk size.
The hypervisor waits for the object and only manages its vhost attachment. Remote disks do not consume the
hypervisor's local qcow2 budget. Implicit local pools and new disk requests
default to WARM ephemeral storage; explicitly configured attributes override
these defaults.

## Virtual machine lifecycle

Rawstor objects are attached through vhost-user-blk sockets served by
`rawstor-vhost@<volume-uuid>.service`. A per-volume drop-in records the remote
storage location so disks remain discoverable across agent restarts.

New ExordosLocalHyper domains use shared `memfd` memory, including machines
initially created with local disks. This permits later hotplug of a Rawstor
disk. Older domains without shared memory are upgraded before their next cold
start, or when attaching a disk while the domain is stopped. A running legacy
domain must be shut down and started before its first Rawstor hotplug; the
driver reports that requirement without restarting the VM automatically.

Before starting or resuming a VM, the driver starts the vhost backends recorded
in its disk configuration and waits for their sockets. Backend startup failure
prevents starting QEMU. Missing backend configuration is an error rather than
a fallback to a different storage location.

For grow-only resize, Core updates the remote object first. The hypervisor
then sends `SIGHUP` to an active vhost backend and acknowledges the new size.
Object deletion waits for the hypervisor to acknowledge detachment. Failed resize notifications are retried during discovery; an
inactive backend reads the new size when it starts. Guest partition and
filesystem growth remain separate operations.

## Integration tests

The integration uses bindings `0.3.7` from
[RawStor v0.3.7](https://github.com/rawstor/librawstor/releases/tag/v0.3.7).
Use native packages and bindings from the same build.

Set `RAWSTOR_TEST_MDS`, `RAWSTOR_TEST_OST` and `RAWSTOR_TEST_VHOST` to daemon paths
to run native integration tests. They cover multiple MDS instances, OSTs,
registration, resize and recovery. Set
`RAWSTOR_TEST_ZFS_LOCATION` to an isolated pre-provisioned dataset and run the
native node test as root to exercise a ZFS backend.

## Project ownership

Exordos maps each non-nil `project_id` to the same Rawstor account UUID.
Creating a disk registers its account in the MDS idempotently, then creates the
object through `mds://PROJECT_UUID@host:port/OBJECT_UUID`. Direct OST and file
backends record the same owner without an MDS registry. The vhost drop-in
retains the account-qualified location; discovery and retry after an agent
restart restore the original project from it. Nil project UUIDs are rejected
because Rawstor reserves nil for unfiltered listings rather than ownership.

Capacity and deletion guards use unfiltered `Location.objects()` inventories
covering every account. The previous iterable `Location` API is no longer
available. Removing a disk does not remove its account: Rawstor account removal
is cascading, and could delete other disks and versions of that project.
This integration runs Rawstor in explicit insecure mode; account UUIDs record
ownership and filter listings, without authenticating callers.

This build changes the handshake, wire frames and on-disk account metadata.
Clients, bindings, MDS, OST and vhost must use the same compatible build. MDS
rejects a database created before accounts, and Rawstor provides no migration.
Recreate disposable development storage instead of mixing the old and new
formats; package updates do not convert or erase existing data.

## Transport mode

The integration currently starts MDS, OST and vhost with explicit
`EXTRA_ARGS="--insecure"`. Connections are plaintext and peers are not
authenticated. TLS and SPIFFE issuance are maintained in a separate branch
layered on the storage integration.

Agents and the API explicitly call `rawstor.initialize(insecure=True)` before
loading native `Target` or `Location` types. This matches the service transport
mode; setting any `RAWSTOR_TLS_*` options in insecure mode is rejected.

## TLS credentials

The TLS integration requires the matching Core branch. Core supplies a private
TLS bundle with storage-cluster, storage-node and remote-volume targets. The
public driver specifications remain free of PEM material. Missing or pending
credentials prevent configuration; there is no insecure fallback.

Runtime service credentials are published under `/etc/rawstor/tls/` as immutable
generations with an atomic `current` symlink. Rawstor service keys are readable
only by root and the rawstor group; control credentials are root-only. Renewal
sends `SIGUSR1` to MDS, OST or the affected vhost without restarting them. A
failed signal is retried. Existing sessions retain their original TLS context;
new connections use the renewed pair. SIGHUP remains reserved for topology and
resize operations.

Bindings perform control operations in isolated child processes, selecting the
intended admin credentials for each operation. Vhost receives a different
certificate with access to only its own account and volume. This avoids binding
all workloads in an agent process to the first certificate it happened to load.
Stop old insecure workloads before enabling this integration; an active insecure
vhost cannot be converted without a maintenance stop.
