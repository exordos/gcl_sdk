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
`ephemeral` is boolean, and `mirrors` ranges from 1 to 255. `chunk_size` must be
a positive power of two no larger than 1 GiB. Invalid payloads fail during
parsing, before configuring services or querying capacity.

Drivers use the packaged `rawstor-mds@.service` and `rawstor-ost@.service`
templates, configured under `/etc/rawstor/mds` and `/etc/rawstor/ost`.
Topology changes request an asynchronous MDS reload. Unregistering a cluster
retains its database; deleting an OST removes service configuration and retains
the backing data. Core waits for a successful MDS topology report before
stopping a removed OST.

The OST driver prepares dedicated file-backed directories. Other native
backends, including `lvm://`, must be provisioned externally and use a root
service drop-in. Rawstor validates the native backing URI. File-backed OSTs
run as `rawstor`.

## Placement and capacity

OST failure-domain paths run from outermost to innermost
(`dc/row/rack/server`). Managed OST topology entries have roles `data,witness`.
The MDS is also advertised as `ost://` with role `witness`. Its topology UUID
is derived from the cluster UUID, while its server failure domain is the Core
node UUID. An explicit `witness_endpoint` can use DNS independently of the bind
address; Core supplies `core.local.genesis-core.tech`.

The MDS witness contributes no data capacity. Two OSTs can host two data copies
with MDS supplying the witness vote for a writable majority.

Pool policies share physical OST capacity; their availability figures must not
be added together. The driver reports OST inventory and completed MDS objects.
`storage_capacity.available_by_policy` derives a conservative placement budget
accounting for mirrors, failure domains and pending disks. Unreachable OSTs
contribute zero available space. MDS remains the final allocation authority.

`MachineVolume.storage_policy` carries the assigned pool UUID, mirror count,
chunk size and failure domain through reconciliation. The hypervisor passes
these settings to `rawstor.Target.create()`. Remote disks do not consume the
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

Grow-only resize updates the remote object and sends `SIGHUP` to an active
vhost backend. Failed resize notifications are retried during discovery; an
inactive backend reads the new size when it starts. Guest partition and
filesystem growth remain separate operations.

## Integration tests

The pinned integration uses bindings `99.0.0+0.41a2714` from
[Actions run 43517230510](https://github.com/rawstor/librawstor/actions/runs/43517230510).
Use native packages and bindings from the same build.

Set `RAWSTOR_TEST_MDS`, `RAWSTOR_TEST_OST` and `RAWSTOR_TEST_VHOST` to daemon paths
to run native integration tests. They cover multiple MDS instances, OSTs,
replication, topology reload, resize and recovery. Set
`RAWSTOR_TEST_ZFS_LOCATION` to an isolated pre-provisioned dataset and run the
native node test as root to exercise a ZFS backend.

## Project ownership

Exordos maps each non-nil `project_id` to the same Rawstor tenant UUID.
Creating a disk registers its tenant in the MDS idempotently, then creates the
object through `mds://PROJECT_UUID@host:port/OBJECT_UUID`. Direct OST and file
backends record the same owner without an MDS registry. The vhost drop-in
retains the tenant-qualified location; discovery and retry after an agent
restart restore the original project from it. Nil project UUIDs are rejected
because Rawstor reserves nil for unfiltered listings rather than ownership.

Capacity and deletion guards use unfiltered `Location.objects()` inventories
covering every tenant. The previous iterable `Location` API is no longer
available. Removing a disk does not remove its tenant: Rawstor tenant removal
is cascading, and could delete other disks and versions of that project.
Tenant identity currently records ownership and filters listings; Rawstor does
not authenticate it or enforce rights. Authorization remains in Exordos.

This build changes the handshake, wire frames and on-disk tenant metadata.
Clients, bindings, MDS, OST and vhost must use the same compatible build. MDS
rejects a database created before tenants, and Rawstor provides no migration.
Recreate disposable development storage instead of mixing the old and new
formats; package updates do not convert or erase existing data.
