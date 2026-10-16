#    Copyright 2025-2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
from __future__ import annotations

import typing as tp
import uuid as sys_uuid

from gcl_iam import algorithms as iam_algorithms
from restalchemy.dm import models as ra_models
from restalchemy.dm import properties
from restalchemy.dm import types as ra_types
from restalchemy.dm import types_dynamic
from restalchemy.dm import types_network as ra_nettypes

from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.infra.dm import models as infra_models
from gcl_sdk.secret import constants as sc


class AbstractSecretConstructor(
    types_dynamic.AbstractKindModel, ra_models.SimpleViewMixin
):
    def build(self, plain_secret: str) -> str:
        raise NotImplementedError()


class PlainSecretConstructor(AbstractSecretConstructor):
    KIND = "plain"

    def build(self, plain_secret: str) -> str:
        return plain_secret


class Secret(
    ra_models.ModelWithUUID,
    ra_models.ModelWithTimestamp,
    ra_models.ModelWithProject,
    ra_models.ModelWithNameDesc,
    ua_models.TargetResourceMixin,
):
    """The base model for all the secret kinds.

    The model is intentionally storage and quota agnostic, the same way
    as the infra models are: a consumer that keeps secrets in SQL or
    accounts them against quotas adds `orm.SQLStorableMixin`,
    `ua_models.TargetResourceSQLStorableMixin` or a quota mixin on its
    own subclasses. The `__tablename__` of the concrete models is the
    canonical table name the consumer storage is expected to use.
    """

    status = properties.property(
        ra_types.Enum([s.value for s in sc.SecretStatus]),
        default=sc.SecretStatus.NEW.value,
    )
    constructor = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(PlainSecretConstructor),
        ),
        required=True,
        default=PlainSecretConstructor,
    )


class Password(
    Secret,
    ra_models.ModelWithTags,
):
    __tablename__ = "secret_passwords"

    method = properties.property(
        ra_types.Enum([s.value for s in sc.SecretMethod]),
        default=sc.SecretMethod.AUTO_HEX.value,
    )
    default_length = properties.property(
        ra_types.Integer(min_value=1, max_value=512),
        default=32,
    )
    value = properties.property(
        ra_types.AllowNone(ra_types.String(min_length=1, max_length=512)),
        default=None,
    )

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        fields = {
            "method",
            "constructor",
            "name",
            "project_id",
            "uuid",
            "description",
            "default_length",
        }

        if sc.SecretMethod.MANUAL == self.method:
            fields.add("value")

        return fields


class AbstractCertificateMethod(
    types_dynamic.AbstractKindModel, ra_models.SimpleViewMixin
):
    pass


class DNSCoreCertificateMethod(AbstractCertificateMethod):
    KIND = "dns_core"


class Certificate(
    Secret,
    ra_models.ModelWithTags,
):
    __tablename__ = "secret_certificates"

    method = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(DNSCoreCertificateMethod),
        ),
        required=True,
        default=DNSCoreCertificateMethod,
    )
    expiration_at = properties.property(
        ra_types.AllowNone(ra_types.UTCDateTimeZ()),
        default=None,
    )
    email = properties.property(ra_types.Email())
    domains = properties.property(
        ra_types.TypedList(ra_nettypes.RecordNameWithWildcard()),
        required=True,
    )
    key = properties.property(
        ra_types.AllowNone(ra_types.String(min_length=1, max_length=10240)),
        default=None,
    )
    cert = properties.property(
        ra_types.AllowNone(ra_types.String(min_length=1, max_length=10240)),
        default=None,
    )
    # Count of days before expiration when the certificate should be renewed
    expiration_threshold = properties.property(
        ra_types.Integer(min_value=0), default=14
    )
    # Two meanings:
    # - CP: ability to overcome the expiration threshold. `True` means it's
    #       possible to overcome and the certificate won't be renewed.
    # - DP: Is the threshold overcame?
    overcome_threshold = properties.property(ra_types.Boolean(), default=False)

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return {
            "method",
            "email",
            "domains",
            "constructor",
            "name",
            "description",
            "project_id",
            "uuid",
            "expiration_threshold",
            "overcome_threshold",
        }


class RSAKey(
    Secret,
    ra_models.ModelWithTags,
):
    __tablename__ = "secret_rsa_keys"

    private_key = properties.property(
        ra_types.String(min_length=1, max_length=32768),
        required=True,
    )
    public_key = properties.property(
        ra_types.String(min_length=1, max_length=16384),
        required=True,
    )
    bitness = properties.property(
        ra_types.Enum([2048, 3072, 4096]),
        default=2048,
    )

    def __init__(
        self,
        private_key: str | None = None,
        public_key: str | None = None,
        bitness: int | None = None,
        **kwargs: tp.Any,
    ):
        """Initialize RSA key secret.

        Rules:
        - If `private_key` is not provided, `bitness` must be provided.
          In this case, the private key is generated and `public_key` is derived.
        - If `private_key` is provided, `bitness` is computed from it.
          If `public_key` is not provided, it is derived from the private key.

        Note: inherited properties of base models may still be passed via `kwargs`.
        """

        bitness = bitness or 2048
        if private_key is None:
            private_key = iam_algorithms.generate_rsa_private_key_pem(bitness=bitness)
            public_key = iam_algorithms.generate_rsa_public_key_pem(
                private_key_pem=private_key
            )
        else:
            bitness = iam_algorithms.get_rsa_bitness_from_private_key_pem(
                private_key_pem=private_key
            )

            if public_key is None:
                public_key = iam_algorithms.generate_rsa_public_key_pem(
                    private_key_pem=private_key
                )

        super().__init__(
            private_key=private_key,
            public_key=public_key,
            bitness=bitness,
            **kwargs,
        )

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return {
            "constructor",
            "name",
            "description",
            "project_id",
            "uuid",
            "bitness",
        }


class SSHKey(
    Secret,
    ra_models.ModelWithTags,
):
    """SSH key secret to be placed on the target nodes.

    The model is the control plane counterpart of the universal agent
    ssh key driver (see `gcl_sdk.agents.universal.drivers.ssh_key`):
    the secret is spread over the target nodes as `ssh_key_target`
    resources each holding only the public part.
    """

    __tablename__ = "secret_ssh_keys"

    target = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(infra_models.NodeTarget),
            types_dynamic.KindModelType(infra_models.NodeSetTarget),
        ),
        required=True,
    )
    user = properties.property(ra_types.String(min_length=1, max_length=64))
    authorized_keys = properties.property(
        ra_types.String(min_length=1, max_length=256),
        default=sc.AUTHORIZED_KEYS_PATH,
    )
    target_public_key = properties.property(
        ra_types.String(max_length=10240),
        default="",
    )

    def target_nodes(self) -> list[sys_uuid.UUID]:
        return self.target.target_nodes()

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return {
            "uuid",
            "name",
            "description",
            "project_id",
            "constructor",
            "user",
            "authorized_keys",
            "target",
            "target_public_key",
        }

    def to_host_resource(
        self,
        master: sys_uuid.UUID,
        node: sys_uuid.UUID,
        status: sc.SecretStatus | None = None,
    ) -> ua_models.TargetResource:
        """Create a target resource for a specific host (node).

        This creates a 'slave' resource for a specific node, which is linked
        to the 'master' SSHKey secret.

        Args:
            master: The UUID of the master SSHKey secret.
            node: The UUID of the target node.
            status: The initial status for the host resource.

        Returns:
            A TargetResource instance for the host.
        """
        properties = {}

        # Copy properties
        for name in self.properties.properties.keys():
            if name not in SSHHostKey.properties.properties:
                continue
            properties[name] = getattr(self, name)

        # Correct UUID based on node UUID
        properties["uuid"] = sys_uuid.uuid5(self.uuid, str(node))
        host_ssh = SSHHostKey(**properties)

        resource = host_ssh.to_ua_resource(sc.SSH_KEY_TARGET_KIND, master=master)
        if status is not None:
            resource.status = status.value
        # Place the key on the node
        resource.agent = node

        return resource


class SSHHostKey(
    ra_models.ModelWithUUID,
    ua_models.TargetResourceMixin,
):
    """SSH host key model.

    The per-node projection of an `SSHKey` secret: it carries only the
    fields the agent needs to place the public key on a single host.
    """

    user = properties.property(ra_types.String(min_length=1, max_length=64))
    authorized_keys = properties.property(
        ra_types.String(min_length=1, max_length=256),
        default=sc.AUTHORIZED_KEYS_PATH,
    )
    target_public_key = properties.property(
        ra_types.String(max_length=10240),
        default="",
    )
