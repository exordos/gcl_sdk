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

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
from unittest.mock import patch
import uuid

import pytest

pytest.importorskip("libvirt")
rawstor = pytest.importorskip("rawstor")
from gcl_sdk.agents.universal.drivers import exordos_hyper  # noqa: E402
from gcl_sdk.agents.universal.drivers import pool  # noqa: E402


@pytest.mark.skipif(
    not all(
        (
            os.environ.get(name)
            for name in ("RAWSTOR_TEST_OST", "RAWSTOR_TEST_MDS", "RAWSTOR_TEST_VHOST")
        )
    )
    or not shutil.which("qemu-system-x86_64")
    or (not hasattr(rawstor.Target, "resize")),
    reason="Requires Rawstor resize bindings, native daemons and QEMU",
)
def test_live_resize_preserves_data_and_notifies_qemu():
    """Exercise the SDK grow, vhost refresh and I/O beyond the old end of disk."""
    env = dict(
        os.environ, RAWSTOR_OPTS_IO_ATTEMPTS="1", RAWSTOR_MDS_OPTS_INFO_INTERVAL="50"
    )
    processes = []
    streams = []

    def port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def start(name, args, work):
        f = (work / (name + ".log")).open("w")
        streams.append(f)
        p = subprocess.Popen(args, env=env, stdout=f, stderr=subprocess.STDOUT)
        processes.append(p)
        return p

    def connect(path):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert all((p.poll() is None for p in processes)), [
                (p.args, p.poll()) for p in processes
            ]
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(str(path))
                s.settimeout(10)
                return s
            except (FileNotFoundError, ConnectionRefusedError):
                s.close()
                time.sleep(0.05)
        raise TimeoutError(path)

    with tempfile.TemporaryDirectory(prefix="rawstor-live-resize-") as temp:
        work = Path(temp)
        try:
            op, mp = (port(), port())
            back = work / "data"
            back.mkdir()
            start(
                "ost",
                [
                    os.environ["RAWSTOR_TEST_OST"],
                    "--bind",
                    f"127.0.0.1:{op}",
                    "--workers",
                    "1",
                    f"file://{back}",
                ],
                work,
            )
            topology = work / "topology"
            topology.write_text(
                f"{uuid.uuid4()} ost://127.0.0.1:{op} 100 data,witness server\n"
            )
            start(
                "mds",
                [
                    os.environ["RAWSTOR_TEST_MDS"],
                    "--bind",
                    f"127.0.0.1:{mp}",
                    "--db",
                    str(work / "mds.db"),
                    "--topology",
                    str(topology),
                    "--workers",
                    "1",
                ],
                work,
            )
            endpoint = f"mds://127.0.0.1:{mp}/"
            target = endpoint + str(uuid.uuid4())
            for _ in range(100):
                try:
                    rawstor.Target(target).create(
                        size=2 << 30, width=1, chunk_size=1 << 30, failure_domain="ost"
                    )
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("MDS did not become ready")
            vhost = start(
                "vhost",
                [
                    os.environ["RAWSTOR_TEST_VHOST"],
                    "--socket-path",
                    str(work / "disk.sock"),
                    target,
                ],
                work,
            )
            deadline = time.monotonic() + 10
            while not (work / "disk.sock").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            bios = bytearray(b"\xff" * 65536)
            bios[-16:-12] = bytes([250, 244, 235, 253])
            (work / "bios.bin").write_bytes(bios)
            start(
                "qemu",
                [
                    "qemu-system-x86_64",
                    "-machine",
                    "q35,accel=tcg",
                    "-m",
                    "64M",
                    "-nodefaults",
                    "-bios",
                    str(work / "bios.bin"),
                    "-display",
                    "none",
                    "-S",
                    "-qmp",
                    f"unix:{work}/qmp,server=on,wait=off",
                    "-object",
                    "memory-backend-memfd,id=mem,size=64M,share=on",
                    "-numa",
                    "node,memdev=mem",
                    "-qtest",
                    f"unix:{work}/qtest,server=on,wait=off",
                    "-chardev",
                    f"socket,id=raw,path={work}/disk.sock",
                    "-device",
                    "vhost-user-blk-pci,chardev=raw,id=disk,addr=05.0,num-queues=4",
                ],
                work,
            )
            sock = connect(work / "qtest")
            reader = sock.makefile("rb")

            def q(command):
                sock.sendall((command + "\n").encode())
                reply = reader.readline().decode().strip()
                assert reply.startswith("OK"), (command, reply)
                return int(reply.split()[1], 0) if len(reply.split()) > 1 else None

            def cfg(offset, value=None):
                q(f"outl 0xcf8 {2147493888 + offset:#x}")
                return q("inl 0xcfc") if value is None else q(f"outl 0xcfc {value:#x}")

            assert cfg(0) & 65535 == 6900, hex(cfg(0))
            cfg(32, 268435456)
            cfg(36, 0)
            cfg(4, 6)
            cap = cfg(52) & 255
            device = None
            while cap:
                header = cfg(cap)
                if header & 255 == 9 and header >> 24 == 4:
                    device = (cfg(cap + 4) & 255, cfg(cap + 8))
                    break
                cap = header >> 8 & 255
            assert device and device[0] == 4, device
            address = 268435456 + device[1]
            common = 268435456
            q(f"writeb {common + 20:#x} 0")
            q(f"writeb {common + 20:#x} 3")
            q(f"writel {common + 8:#x} 1")
            q(f"writel {common + 12:#x} 1")
            q(f"writel {common + 8:#x} 0")
            q(f"writel {common + 12:#x} 0")
            q(f"writeb {common + 20:#x} 11")
            assert q(f"readb {common + 20:#x}") & 8
            q(f"writew {common + 22:#x} 0")
            q(f"writew {common + 24:#x} 8")
            for offset, value in ((32, 1048576), (40, 1052672), (48, 1056768)):
                q(f"writeq {common + offset:#x} {value:#x}")
            q(f"writew {common + 28:#x} 1")
            q(f"writeb {common + 20:#x} 15")
            qmp = connect(work / "qmp")
            qreader = qmp.makefile("rb")
            qreader.readline()
            for command in ("qmp_capabilities", "cont"):
                qmp.sendall((json.dumps({"execute": command}) + "\n").encode())
                while "return" not in json.loads(qreader.readline()):
                    pass
            time.sleep(0.2)
            before = q(f"readq {address:#x}")
            assert before == 2 * (1 << 30) // 512, before
            available_index = 0

            def disk_io(write, offset, payload=b"\x00" * 512):
                nonlocal available_index
                header = 1060864
                data = 1069056
                status = 1064960

                def memory(addr, value):
                    q(f"write {addr:#x} {len(value):#x} 0x{value.hex()}")

                memory(header, struct.pack("<IIQ", 1 if write else 0, 0, offset // 512))
                memory(data, payload)
                q(f"writeb {status:#x} 255")
                descriptors = (
                    struct.pack("<QIHH", header, 16, 1, 1)
                    + struct.pack("<QIHH", data, 512, 1 if write else 3, 2)
                    + struct.pack("<QIHH", status, 1, 2, 0)
                )
                memory(1048576, descriptors)
                q(f"writew {1052676 + available_index % 8 * 2:#x} 0")
                available_index += 1
                q(f"writew 0x101002 {available_index}")
                q("writew 0x10003000 0")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if q("readb 0x104000") != 255:
                        break
                    time.sleep(0.01)
                assert q("readb 0x104000") == 0, "virtio I/O failed"
                return q("read 0x105000 512").to_bytes(512, "big")

            original = b"before-resize".ljust(512, b"!")
            disk_io(True, 0, original)
            driver = exordos_hyper.ExordosLocalHyperDriver(
                pool.MachinePool(
                    name="test",
                    driver_spec=pool.ExordosLocalHyperDriverSpec(
                        connection_uri="test:///default", node=uuid.uuid4()
                    ),
                )
            )
            driver._vhost_drop_in_dir = lambda volume_uuid: str(
                work / "drop-ins" / str(volume_uuid)
            )
            volume = pool.MachineVolume(
                uuid=uuid.UUID(target.rsplit("/", 1)[-1]),
                project_id=uuid.uuid4(),
                size=3,
                storage_location=endpoint,
            )

            def notify(args):
                assert args[:4] == [
                    "systemctl",
                    "kill",
                    "--kill-whom=main",
                    "--signal=HUP",
                ], args
                os.kill(vhost.pid, signal.SIGHUP)

            with (
                patch.object(
                    exordos_hyper.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                patch.object(
                    exordos_hyper.subprocess, "check_call", side_effect=notify
                ),
            ):
                driver.resize_volume(volume)
            assert rawstor.Target(target).spec().size == 3 << 30
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                after = q(f"readq {address:#x}")
                if after == 3 * (1 << 30) // 512:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError(f"QEMU capacity not refreshed: {after}")
            assert disk_io(False, 0) == original
            grown = b"new-capacity".ljust(512, b"?")
            disk_io(True, (2 << 30) + 512, grown)
            assert disk_io(False, (2 << 30) + 512) == grown
            print(
                f"QEMU virtio disk capacity: {before * 512} -> {after * 512} bytes; same vhost pid {vhost.pid}",
                flush=True,
            )
        except Exception:
            for f in streams:
                f.flush()
                print(f.name + "\n" + Path(f.name).read_text()[-5000:], flush=True)
            raise
        finally:
            for p in reversed(processes):
                if p.poll() is None:
                    p.terminate()
            for p in reversed(processes):
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            for f in streams:
                f.close()
