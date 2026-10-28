# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

import abc
import dataclasses
import importlib.metadata
from urllib.parse import urlsplit
import uuid


@dataclasses.dataclass(frozen=True)
class CloudConfig:
    connection: uuid.UUID
    auth_url: str
    auth: dict = dataclasses.field(repr=False)
    auth_type: str = "v3applicationcredential"
    driver: str = "openstack"
    region: str | None = None
    interface: str = "public"

    def __post_init__(self):
        url = urlsplit(self.auth_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Keystone auth_url must be an HTTP(S) URL without credentials"
            )
        if self.interface not in {"public", "internal", "admin"}:
            raise ValueError("Invalid service catalog interface")
        if not isinstance(self.auth, dict) or any(
            not isinstance(v, str) or not v.strip() for v in self.auth.values()
        ):
            raise ValueError("Authentication values must be nonempty strings")
        if self.auth_type == "v3applicationcredential":
            required = {"application_credential_id", "application_credential_secret"}
            valid = set(self.auth) == required
        elif self.auth_type == "v3password":
            allowed = {
                "username",
                "user_id",
                "password",
                "user_domain_id",
                "user_domain_name",
                "project_id",
                "project_name",
                "project_domain_id",
                "project_domain_name",
            }
            valid = (
                not set(self.auth) - allowed
                and "password" in self.auth
                and (
                    "user_id" in self.auth
                    or (
                        "username" in self.auth
                        and bool(
                            {"user_domain_id", "user_domain_name"} & self.auth.keys()
                        )
                    )
                )
                and (
                    "project_id" in self.auth
                    or (
                        "project_name" in self.auth
                        and bool(
                            {"project_domain_id", "project_domain_name"}
                            & self.auth.keys()
                        )
                    )
                )
            )
        else:
            raise ValueError(
                "Supported auth types are v3applicationcredential and v3password"
            )
        if not valid:
            raise ValueError("Invalid or incomplete OpenStack authentication fields")


class CloudDriver(abc.ABC):
    """Bootstrap contract for OpenStack-family provider drivers.

    The current compute/network adapters also use the OpenStackDriver API;
    extension entry points must subclass OpenStackDriver. This is not a
    portable interface for unrelated cloud APIs.

    Core supplies credentials directly; they never belong to a pool target.
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
