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


# --- FORWARD-chain accept (shared filter chain, via iptables) ---------------


class _FakeRun:
    """Records _run() calls and returns canned output for `iptables -S FORWARD`."""

    def __init__(self, listing=""):
        self.calls = []
        self._listing = listing

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:2] == [border.IPTABLES_BIN, "-S"]:
            return self._listing
        return ""


_FWD = {
    "proto": "tcp",
    "listen_port": 443,
    "to_host": "192.168.100.2",
    "to_port": 443,
}


def test_forward_accept_specs_shape():
    specs = _border(forwards=[_FWD])._forward_accept_specs()
    assert specs == [
        [
            "-p", "tcp",
            "-d", "192.168.100.2",
            "--dport", "443",
            "-m", "comment", "--comment", "exordos_border",
            "-j", "ACCEPT",
        ]
    ]  # fmt: skip


def test_forward_accept_specs_empty_without_forwards():
    assert _border()._forward_accept_specs() == []


def test_apply_forward_accepts_lists_then_inserts(monkeypatch):
    fake = _FakeRun(listing="")
    monkeypatch.setattr(border, "_run", fake)

    _border(
        forwards=[
            _FWD,
            {
                "proto": "udp",
                "listen_port": 53,
                "to_host": "192.168.100.2",
                "to_port": 5300,
            },
        ]
    )._apply_forward_accepts()

    # Lists the chain first (to find anything stale -- none here), then
    # inserts each accept at position 1. Nothing to delete.
    assert fake.calls[0] == [border.IPTABLES_BIN, "-S", "FORWARD"]
    inserts = [c for c in fake.calls if c[1:4] == ["-I", "FORWARD", "1"]]
    assert len(inserts) == 2
    assert inserts[0] == [
        border.IPTABLES_BIN,
        "-I", "FORWARD", "1",
        "-p", "tcp",
        "-d", "192.168.100.2",
        "--dport", "443",
        "-m", "comment", "--comment", "exordos_border",
        "-j", "ACCEPT",
    ]  # fmt: skip
    assert not any(c[1] == "-D" for c in fake.calls if len(c) > 1)


def test_apply_forward_accepts_inserts_new_before_deleting_stale(monkeypatch):
    # An old forward (port 80) is no longer wanted; a new one (port 443) is.
    listing = "\n".join(
        [
            "-P FORWARD ACCEPT",
            "-A FORWARD -d 192.168.100.2/32 -p tcp -m tcp --dport 80 "
            '-m comment --comment "exordos_border" -j ACCEPT',
        ]
    )
    fake = _FakeRun(listing=listing)
    monkeypatch.setattr(border, "_run", fake)

    _border(forwards=[_FWD])._apply_forward_accepts()

    kinds = [
        "insert"
        if c[1:3] == ["-I", "FORWARD"]
        else "delete"
        if c[1] == "-D"
        else "list"
        for c in fake.calls
    ]
    # Listing (to find what's stale), then every insert, then every
    # delete -- the old port-80 accept is never absent from the chain
    # at the same time the new port-443 one is missing too.
    assert kinds == ["list", "insert", "delete"]
    delete_call = fake.calls[kinds.index("delete")]
    assert "80" in delete_call


def test_clear_forward_accepts_removes_only_our_tagged_rules(monkeypatch):
    listing = '-P FORWARD ACCEPT\n-A FORWARD -d 192.168.100.2/32 -p tcp -m tcp --dport 443 -m comment --comment "exordos_border" -j ACCEPT\n-A FORWARD -o virbr1 -j REJECT'
    fake = _FakeRun(listing=listing)
    monkeypatch.setattr(border, "_run", fake)

    _border()._clear_forward_accepts()

    deletes = [c for c in fake.calls if len(c) > 1 and c[1] == "-D"]
    assert len(deletes) == 1
    assert "exordos_border" in deletes[0]  # comment survives shlex (unquoted)
    assert "REJECT" not in " ".join(deletes[0])


def test_apply_forward_accepts_missing_iptables_is_noop(monkeypatch):
    def _raise(cmd):
        raise FileNotFoundError()

    monkeypatch.setattr(border, "_run", _raise)
    # Must not raise when iptables is absent.
    _border(forwards=[_FWD])._apply_forward_accepts()


def test_full_nat_forward_masquerades_the_dnated_flow():
    b = border.Border(
        uuid=sys_uuid.uuid4(),
        forwards=[
            {
                "proto": "tcp",
                "public_ip": None,
                "listen_port": 2222,
                "to_host": "10.20.0.2",
                "to_port": 22,
                "full_nat": True,
            }
        ],
        snat_rules=[],
    )
    nft = b._render_nft()
    assert "tcp dport 2222 dnat to 10.20.0.2:22" in nft
    # The reply path: the forwarded flow is masqueraded so the target
    # answers through this border, not directly to the client.
    assert "ip daddr 10.20.0.2 tcp dport 22 masquerade" in nft
