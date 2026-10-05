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
"""Shared rawstor admission budget, in bytes (pool figures are not additive)."""

DOMAIN_DEPTH = {"dc": 1, "row": 2, "rack": 3, "server": 4}


def domain(node, level):
    if level == "ost":
        return node["uuid"]
    parts = node["failure_domain_path"].split("/")
    # A short path omits outer levels; omitted domains are shared.
    parts = [""] * (4 - len(parts)) + parts
    return "/".join(parts[: DOMAIN_DEPTH[level]])


def available_by_policy(nodes, policy, pending=()):
    """Placement upper bound; MDS remains the final allocation authority.

    Full pending sizes are withheld from each OST that could receive a
    chunk. This is conservative until MDS publishes the allocated object.
    All pools use the same pending collection, irrespective of policy.
    """
    groups = {}
    pending_bytes = sum(pending)
    for node in nodes:
        budget = max(0, node["available"] - pending_bytes)
        key = domain(node, policy["failure_domain"])
        groups[key] = groups.get(key, 0) + budget
    copies = policy["mirrors"]
    if len(groups) < copies:
        return 0
    # A domain can hold at most one copy of each logical byte.
    lo, hi = 0, sum(groups.values()) // copies
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(min(free, mid) for free in groups.values()) >= copies * mid:
            lo = mid
        else:
            hi = mid - 1
    chunk = policy["chunk_size"]
    return lo // chunk * chunk


def default_policies(cluster_uuid):
    import uuid

    return {
        str(uuid.uuid5(cluster_uuid, name)): {
            "name": name,
            "speed": "WARM",
            "ephemeral": ephemeral,
            "mirrors": mirrors,
            "failure_domain": "server",
            "chunk_size": 1 << 30,
        }
        for name, ephemeral, mirrors in [
            ("persistent", False, 2),
            ("ephemeral", True, 1),
        ]
    }


def validate_policy(policy):
    if policy["failure_domain"] not in {"ost", "server", "rack", "row", "dc"}:
        raise ValueError("Unknown failure domain")
    if not 1 <= policy["mirrors"] <= 255:
        raise ValueError("Mirrors must be between 1 and 255")
    chunk = policy["chunk_size"]
    if chunk <= 0 or chunk & (chunk - 1) or chunk > 1 << 30:
        raise ValueError("Chunk size must be a power of two no larger than 1 GiB")
    if policy["speed"] not in {"COLD", "WARM", "HOT"}:
        raise ValueError("Unknown disk speed")
    if not isinstance(policy["ephemeral"], bool):
        raise ValueError("Ephemeral must be boolean")
