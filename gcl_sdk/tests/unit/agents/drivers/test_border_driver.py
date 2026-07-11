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

import uuid as sys_uuid

from gcl_sdk.agents.universal.drivers import border


def _border(snat_rules=None, forwards=None):
    return border.Border(
        uuid=sys_uuid.uuid4(),
        snat_rules=snat_rules or [],
        forwards=forwards or [],
    )


def test_masquerade_snat():
    nft = _border(snat_rules=[{"source_cidr": "192.168.100.0/24"}])._render_nft()
    assert "ip saddr 192.168.100.0/24 masquerade" in nft


def test_explicit_snat():
    nft = _border(
        snat_rules=[
            {"source_cidr": "10.0.0.0/24", "mode": "snat", "snat_to": "203.0.113.5"}
        ]
    )._render_nft()
    assert "ip saddr 10.0.0.0/24 snat to 203.0.113.5" in nft
    assert "masquerade" not in nft


def test_snat_mode_without_snat_to_falls_back_to_masquerade():
    # Defensive: mode=snat but snat_to missing -> masquerade branch, never a
    # malformed `snat to None` rule.
    nft = _border(
        snat_rules=[{"source_cidr": "10.0.0.0/24", "mode": "snat"}]
    )._render_nft()
    assert "ip saddr 10.0.0.0/24 masquerade" in nft
    assert "snat to" not in nft


def test_dnat_without_public_ip():
    nft = _border(
        forwards=[
            {
                "proto": "tcp",
                "listen_port": 22001,
                "to_host": "192.168.100.2",
                "to_port": 22,
            }
        ]
    )._render_nft()
    assert "tcp dport 22001 dnat to 192.168.100.2:22" in nft
    assert "ip daddr" not in nft


def test_dnat_with_public_ip():
    nft = _border(
        forwards=[
            {
                "proto": "tcp",
                "public_ip": "203.0.113.5",
                "listen_port": 443,
                "to_host": "192.168.100.2",
                "to_port": 443,
            }
        ]
    )._render_nft()
    assert "ip daddr 203.0.113.5 tcp dport 443 dnat to 192.168.100.2:443" in nft


def test_udp_forward():
    nft = _border(
        forwards=[
            {
                "proto": "udp",
                "listen_port": 53,
                "to_host": "192.168.100.2",
                "to_port": 5300,
            }
        ]
    )._render_nft()
    assert "udp dport 53 dnat to 192.168.100.2:5300" in nft


def test_proto_defaults_to_tcp():
    nft = _border(
        forwards=[{"listen_port": 80, "to_host": "192.168.100.2", "to_port": 80}]
    )._render_nft()
    assert "tcp dport 80 dnat to 192.168.100.2:80" in nft


def test_table_is_atomically_replaced_with_both_nat_chains():
    nft = _border()._render_nft()
    # Owned table, add-then-delete idempotent header, both NAT hooks present
    # even with no rules.
    assert "add table ip exordos_border" in nft
    assert "delete table ip exordos_border" in nft
    assert "table ip exordos_border {" in nft
    assert "type nat hook prerouting priority dstnat" in nft
    assert "type nat hook postrouting priority srcnat" in nft


def test_multiple_rules_all_rendered():
    nft = _border(
        snat_rules=[{"source_cidr": "192.168.100.0/24"}],
        forwards=[
            {
                "proto": "tcp",
                "listen_port": 443,
                "to_host": "192.168.100.2",
                "to_port": 443,
            },
            {
                "proto": "tcp",
                "listen_port": 22001,
                "to_host": "192.168.100.2",
                "to_port": 22,
            },
        ],
    )._render_nft()
    assert nft.count("dnat to") == 2
    assert "ip saddr 192.168.100.0/24 masquerade" in nft
