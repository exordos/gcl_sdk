#    Copyright 2026 Genesis Corporation.
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

from http import client as http_client

from gcl_iam.middlewares import EndpointComparator
from restalchemy.api.middlewares import contexts as mw_contexts
from restalchemy.api.middlewares import errors as mw_errors

from gcl_sdk.agents.universal.api import contexts
from gcl_sdk.agents.universal.api import packers


class SdkContextMiddleware(mw_contexts.ContextMiddleware):
    def __init__(
        self,
        application,
        context_class=contexts.SdkEncryptionInformationContext,
        context_kwargs=None,
        skip_sdk_endpoints: list = None,
    ):
        """
        Initialize the middleware with a context class.

        :param application: The next application down the WSGI stack.
        :type application: callable
        :param context_class: The class used to construct a request context.
        :type context_class: gcl_sdk.agents.universal.api.contexts.
                             SdkEncryptionInformationContext
        :param context_kwargs: Additional keyword arguments to pass to the
            context class constructor.
        :type context_kwargs: dict
        :param skip_sdk_endpoints: The list of endpoints to skip check the SDK
            encryption.
        :type skip_sdk_endpoints: list
        """
        super().__init__(
            application=application,
            context_class=context_class,
            context_kwargs=context_kwargs,
        )
        self._skip_sdk_endpoints = (
            [EndpointComparator(e) for e in skip_sdk_endpoints]
            if skip_sdk_endpoints
            else []
        )

    def _construct_context(self, req):
        """
        Constructs a context for the given request.

        This method initializes and returns an instance of the context
        class specified during the middleware initialization. The context
        is used to manage request-specific state and operations.

        :param req: The request object for which the context is being
            constructed.
        :return: An instance of the context class.
        """

        return self._context_class(req, **self._context_kwargs)

    def _should_skip_sdk(self, req) -> bool:
        for endpoint in self._skip_sdk_endpoints:
            if endpoint.compare(req):
                return True
        return False

    def _get_response(self, ctx, req):
        if (
            not self._should_skip_sdk(req)
            and ctx.encryption_information.is_requires_encryption()
        ):
            if req.content_type != packers.ENCRYPTED_JSON_CONTENT_TYPE:
                return req.ResponseClass(
                    status=http_client.BAD_REQUEST,
                    json=mw_errors.exception2dict(
                        ValueError("Response content type should be encrypted")
                    ),
                )

        return super()._get_response(ctx, req)
