# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

from restalchemy.dm import properties
from restalchemy.dm import types

from gcl_sdk.agents.universal.drivers import exceptions
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.cloud import openvpn


class OpenVPNLink(meta.MetaDataPlaneModel):
    configuration = properties.property(types.Dict(), default=dict)
    generation = properties.property(types.String(), default="")
    status = properties.property(types.String(), default="IN_PROGRESS")

    def get_meta_model_fields(self):
        return {"uuid", "generation"}

    def dump_to_dp(self):
        openvpn.install(self.uuid, self.configuration)

    def update_on_dp(self):
        self.dump_to_dp()

    def restore_from_dp(self):
        try:
            self.configuration = openvpn.read_bundle(self.uuid)
        except FileNotFoundError:
            raise exceptions.ResourceNotFound(resource=self)
        self.status = (
            "ACTIVE"
            if openvpn.connected(self.uuid, self.configuration)
            else "IN_PROGRESS"
        )

    def delete_from_dp(self):
        openvpn.uninstall(self.uuid)


class OpenVPNCapabilityDriver(meta.MetaFileStorageAgentDriver):
    __model_map__ = {"cloud_net_link": OpenVPNLink}

    def __init__(self, meta_file="/var/lib/exordos/universal_agent/openvpn.json"):
        super().__init__(meta_file=meta_file)
