# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

import abc
import dataclasses
import importlib.metadata
import uuid


@dataclasses.dataclass(frozen=True)
class CloudConfig:
    connection: uuid.UUID
    cloud: str
    driver: str = "openstack"
    region: str | None = None


class CloudDriver(abc.ABC):
    """Bootstrap contract for OpenStack-family provider drivers.

    The current compute/network adapters also use the OpenStackDriver API;
    extension entry points must subclass OpenStackDriver. This is not a
    portable interface for unrelated cloud APIs.

    Credentials are resolved from the controller's clouds.yaml. Only the
    configuration name, connection identity and region cross the adapter API.
    """

    def __init__(self, config: CloudConfig):
        self.config = config

    @abc.abstractmethod
    def ensure_network(self, resource, name):
        raise NotImplementedError

    @abc.abstractmethod
    def ensure_subnet(self, resource, network, cidr, **attributes):
        raise NotImplementedError

    @abc.abstractmethod
    def ensure_port(self, resource, subnet, **attributes):
        raise NotImplementedError

    @abc.abstractmethod
    def ensure_server(self, resource, **attributes):
        raise NotImplementedError

    @abc.abstractmethod
    def ensure_volume(self, resource, **attributes):
        raise NotImplementedError


def load_driver(config: CloudConfig) -> CloudDriver:
    from gcl_sdk.cloud.drivers.openstack import OpenStackDriver

    if config.driver == "openstack":
        return OpenStackDriver(config)
    for entry in importlib.metadata.entry_points(group="gcl_sdk_cloud_driver"):
        if entry.name == config.driver:
            driver = entry.load()
            if not isinstance(driver, type) or not issubclass(driver, OpenStackDriver):
                raise TypeError("Cloud adapters require an OpenStackDriver subclass")
            return driver(config)
    raise ValueError(f"Unknown cloud driver: {config.driver}")
