#    Copyright 2025 Genesis Corporation.
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

from restalchemy.api import routes

from gcl_sdk.agents.universal.status_api import controllers


class UniversalAgentsRoute(routes.Route):
    """Handler for /v1/agents/ endpoint"""

    __allow_methods__ = [
        routes.GET,
        routes.CREATE,
        routes.UPDATE,
        routes.DELETE,
    ]

    __controller__ = controllers.UniversalAgentsController


class ResourcesRoute(routes.Route):
    """Handler for /v1/kind/<name>/resources/ endpoint"""

    __allow_methods__ = [
        routes.GET,
        routes.CREATE,
        routes.UPDATE,
        routes.DELETE,
    ]

    __controller__ = controllers.ResourcesController


class KindRoute(routes.Route):
    """Handler for /v1/kind/ endpoint"""

    __allow_methods__ = [routes.FILTER, routes.GET]
    __controller__ = controllers.KindController

    resources = routes.route(ResourcesRoute, resource_route=True)


class RefreshSecretAction(routes.Action):
    """Handler for /v1/nodes/<uuid>/actions/refresh_secret/invoke endpoint"""

    __controller__ = controllers.NodesController


class NodeVerifierVerifyAction(routes.Action):
    """Handler for /v1/node_verifiers/<uuid>/actions/verify endpoint"""

    __controller__ = controllers.NodeVerifierController


class NodesRoute(routes.Route):
    """Handler for /v1/nodes/ endpoint"""

    __allow_methods__ = [routes.GET]
    __controller__ = controllers.NodesController

    refresh_secret = routes.action(RefreshSecretAction, invoke=True)


class NodeVerifiersRoute(routes.Route):
    """Handler for /v1/node_verifiers/ endpoint"""

    __allow_methods__ = [routes.GET]
    __controller__ = controllers.NodeVerifierController

    verify = routes.action(NodeVerifierVerifyAction)
