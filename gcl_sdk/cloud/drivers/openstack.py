# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

import base64
import gzip
import logging
import uuid
import weakref

from gcl_sdk.cloud import images
from gcl_sdk.cloud.drivers.base import CloudDriver


class OpenStackDriver(CloudDriver):
    """Manage only resources bearing this connection's ownership marker."""

    def __init__(self, config, connection=None):
        super().__init__(config)
        owned_connection = connection is None
        if connection is None:
            import openstack

            connection = openstack.connect(
                cloud=config.cloud,
                region_name=config.region,
                app_name="exordos",
                app_version="1",
                api_timeout=30,
            )
        self.api = connection
        if owned_connection:
            weakref.finalize(self, connection.close)
        logger = logging.getLogger("gcl_sdk.cloud.http")
        logger.setLevel(logging.WARNING)
        for service in ("compute", "network", "block_storage", "image"):
            getattr(self.api, service).logger = logger

    def marker(self, kind, resource):
        return f"exordos:{self.config.connection}:{kind}:{uuid.UUID(str(resource))}"

    def identity(self, obj, kind):
        marker = obj.get("description", "") or ""
        if kind in {"server", "volume"}:
            marker = (obj.get("metadata") or {}).get("exordos", "")
        elif kind == "image":
            marker = (obj.get("properties") or {}).get("description", "")
        prefix = f"exordos:{self.config.connection}:{kind}:"
        if not marker.startswith(prefix):
            return None
        try:
            return uuid.UUID(marker[len(prefix) :])
        except ValueError:
            return None

    def _find(self, objects, kind, resource):
        matches = [
            o for o in objects if self.identity(o, kind) == uuid.UUID(str(resource))
        ]
        if len(matches) > 1:
            raise RuntimeError(f"Duplicate managed {kind} identity: {resource}")
        return matches[0] if matches else None

    def _require(self, obj, kind):
        if obj is None or self.identity(obj, kind) is None:
            raise ValueError(f"Refusing unmanaged {kind} operation")
        return obj

    def networks(self):
        return [n for n in self.api.network.networks() if self.identity(n, "network")]

    def ensure_network(self, resource, name):
        found = self._find(self.networks(), "network", resource)
        if found:
            return found
        return self.api.network.create_network(
            name=name,
            description=self.marker("network", resource),
            is_router_external=False,
            is_shared=False,
        )

    def delete_network(self, network):
        self._require(network, "network")
        self.api.network.delete_network(network.id, ignore_missing=True)

    def subnets(self, network):
        self._require(network, "network")
        return [
            s
            for s in self.api.network.subnets(network_id=network.id)
            if self.identity(s, "subnet")
        ]

    def ensure_subnet(self, resource, network, cidr, **attributes):
        self._require(network, "network")
        found = self._find(self.subnets(network), "subnet", resource)
        if found:
            if found.cidr != str(cidr):
                raise ValueError("Managed subnet CIDR is immutable")
            return found
        return self.api.network.create_subnet(
            network_id=network.id,
            name=str(resource),
            cidr=str(cidr),
            ip_version=4,
            description=self.marker("subnet", resource),
            **attributes,
        )

    def delete_subnet(self, subnet):
        self._require(subnet, "subnet")
        self.api.network.delete_subnet(subnet.id, ignore_missing=True)

    def ports(self, network, subnet=None):
        self._require(network, "network")
        return [
            p
            for p in self.api.network.ports(network_id=network.id)
            if self.identity(p, "port")
            and (
                subnet is None
                or any(ip["subnet_id"] == subnet.id for ip in p.fixed_ips)
            )
        ]

    def ensure_port(self, resource, subnet, **attributes):
        self._require(subnet, "subnet")
        network = self.api.network.get_network(subnet.network_id)
        found = self._find(self.ports(network), "port", resource)
        if found:
            return found
        ip = attributes.pop("ip_address", None)
        fixed = {"subnet_id": subnet.id}
        if ip is not None:
            fixed["ip_address"] = str(ip)
        return self.api.network.create_port(
            network_id=subnet.network_id,
            name=str(resource),
            fixed_ips=[fixed],
            description=self.marker("port", resource),
            **attributes,
        )

    def delete_port(self, port):
        self._require(port, "port")
        self.api.network.delete_port(port.id, ignore_missing=True)

    def servers(self, pool=None):
        return [
            s
            for s in self.api.compute.servers(details=True)
            if self.identity(s, "server")
            and (pool is None or (s.metadata or {}).get("pool") == str(pool))
        ]

    def server(self, resource, pool=None):
        return self._find(self.servers(pool), "server", resource)

    def flavor(self, cores, ram):
        matches = [
            f for f in self.api.compute.flavors() if f.vcpus == cores and f.ram == ram
        ]
        if not matches:
            raise ValueError(f"No flavor with {cores} vCPUs and {ram} MiB RAM")
        return min(matches, key=lambda f: (f.disk, f.id))

    def ensure_server(
        self,
        resource,
        *,
        name,
        pool,
        project,
        cores,
        ram,
        image,
        ports,
        volumes=(),
        user_data=None,
        key_name=None,
    ):
        found = self.server(resource, pool)
        if found:
            return found
        for port in ports:
            self._require(port, "port")
        for volume in volumes:
            self._require(volume, "volume")
        flavor = self.flavor(cores, ram)
        attributes = dict(
            name=name,
            flavor_id=flavor.id,
            networks=[{"port": port.id} for port in ports],
            metadata={
                "exordos": self.marker("server", resource),
                "pool": str(pool),
                "project": str(project),
                "image": str(image),
            },
        )
        if key_name:
            attributes["key_name"] = key_name
        if user_data:
            # cloud-init accepts gzip content; compress the mesh bootstrap so
            # certificates and per-peer configuration fit Nova's user-data limit.
            encoded = base64.b64encode(
                gzip.compress(user_data.encode(), mtime=0)
            ).decode()
            if len(encoded) > 65535:
                raise ValueError(
                    "Cloud bootstrap exceeds Nova user-data size limit: "
                    f"{len(encoded)} base64 characters, maximum 65535"
                )
            attributes["user_data"] = encoded
        if volumes:
            attributes["block_device_mapping_v2"] = [
                {
                    "uuid": v.id,
                    "source_type": "volume",
                    "destination_type": "volume",
                    "boot_index": 0 if i == 0 else -1,
                    "delete_on_termination": False,
                }
                for i, v in enumerate(volumes)
            ]
        else:
            attributes["image_id"] = images.resolve(self, image).id
        return self.api.compute.create_server(**attributes)

    def delete_server(self, server):
        self._require(server, "server")
        self.api.compute.delete_server(server.id, ignore_missing=True)
        self.api.compute.wait_for_delete(server, wait=180)

    def volumes(self, pool=None):
        return [
            v
            for v in self.api.block_storage.volumes(details=True)
            if self.identity(v, "volume")
            and (pool is None or (v.metadata or {}).get("pool") == str(pool))
        ]

    def volume(self, resource, pool=None):
        return self._find(self.volumes(pool), "volume", resource)

    def ensure_volume(
        self,
        resource,
        *,
        name,
        size,
        pool,
        project,
        image=None,
        volume_type=None,
        index=0,
    ):
        found = self.volume(resource, pool)
        if found:
            return found
        attributes = dict(
            name=name,
            size=size,
            metadata={
                "exordos": self.marker("volume", resource),
                "pool": str(pool),
                "project": str(project),
                "image": image or "",
                "index": str(index),
            },
        )
        if image:
            attributes["image_id"] = images.resolve(self, image).id
        if volume_type:
            attributes["volume_type"] = volume_type
        return self.api.block_storage.create_volume(**attributes)

    def delete_volume(self, volume):
        self._require(volume, "volume")
        if volume.attachments:
            raise ValueError("Detach volume before deleting it")
        self.api.block_storage.delete_volume(volume.id, ignore_missing=True)
        self.api.block_storage.wait_for_delete(volume, wait=180)

    def ensure_router(self, resource, subnet, external_network=None):
        self._require(subnet, "subnet")
        found = self._find(self.api.network.routers(), "router", resource)
        if found is None:
            attrs = {
                "name": str(resource),
                "description": self.marker("router", resource),
            }
            if external_network:
                attrs["external_gateway_info"] = {"network_id": external_network}
            found = self.api.network.create_router(**attrs)
        attached = any(
            ip["subnet_id"] == subnet.id
            for p in self.api.network.ports(device_id=found.id)
            for ip in p.fixed_ips
        )
        if not attached:
            self.api.network.add_interface_to_router(found.id, subnet_id=subnet.id)
        return found

    def delete_router(self, router):
        self._require(router, "router")
        for port in self.api.network.ports(device_id=router.id):
            if port.device_owner.startswith("network:router_interface"):
                self.api.network.remove_interface_from_router(
                    router.id, port_id=port.id
                )
        self.api.network.delete_router(router.id, ignore_missing=True)

    def ensure_floating_ip(self, port, external_network):
        self._require(port, "port")
        existing = list(self.api.network.ips(port_id=port.id))
        if len(existing) > 1:
            raise ValueError("Multiple floating IPs on managed port")
        if existing:
            return self._require(existing[0], "floating_ip")
        return self.api.network.create_ip(
            floating_network_id=external_network,
            port_id=port.id,
            description=self.marker("floating_ip", self.identity(port, "port")),
        )

    def ensure_security_group(self, resource):
        found = self._find(
            self.api.network.security_groups(), "security_group", resource
        )
        if found is None:
            found = self.api.network.create_security_group(
                name=str(resource), description=self.marker("security_group", resource)
            )
        return found

    def security_group(self, resource, rules):
        found = self.ensure_security_group(resource)
        fields = (
            "direction",
            "ethertype",
            "protocol",
            "port_range_min",
            "port_range_max",
            "remote_ip_prefix",
            "remote_group_id",
        )

        def key(rule):
            view = {f: rule.get(f) or None for f in fields}
            view["ethertype"] = rule.get("ether_type") or view["ethertype"]
            if not view["remote_group_id"] and not view["remote_ip_prefix"]:
                view["remote_ip_prefix"] = (
                    "::/0" if view["ethertype"] == "IPv6" else "0.0.0.0/0"
                )
            return tuple(view[f] for f in fields)

        desired = {key(rule) for rule in rules}
        actual = {
            key(rule): rule
            for rule in self.api.network.security_group_rules(
                security_group_id=found.id
            )
        }
        for key in actual.keys() - desired:
            self.api.network.delete_security_group_rule(actual[key].id)
        for key in desired - actual.keys():
            self.api.network.create_security_group_rule(
                security_group_id=found.id,
                **{f: value for f, value in zip(fields, key) if value is not None},
            )
        return found
