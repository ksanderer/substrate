#!/usr/bin/env python3
# Copyright 2026 The Agent Substrate Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Restore identical guest RAM, measure memory sharing and verify write isolation."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
import http.client
import json
from pathlib import Path
import re
import shutil
import socket
import statistics
import subprocess
import time

MEMORY_FIELDS = ("Rss", "Pss", "Shared_Clean", "Shared_Dirty", "Private_Clean", "Private_Dirty")
MAPPING = re.compile(r"^[0-9a-f]+-[0-9a-f]+\s")
GUEST_PORT = 9000


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=60)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def api(vm, operation, data=None, method="PUT"):
    with closing(UnixHTTP(vm / "api.sock")) as client:
        headers = {"Content-Type": "application/json"} if data is not None else {}
        client.request(method, operation, json.dumps(data) if data is not None else None, headers)
        response = client.getresponse()
        body = response.read().decode()
    if response.status >= 300:
        raise RuntimeError(f"{operation} HTTP {response.status}: {body}")


def guest(vm, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        client.connect(str(vm / "guest.sock"))
        client.sendall(f"CONNECT {GUEST_PORT}\n".encode())
        with client.makefile("rb") as stream:
            ack = stream.readline()
            if not ack.startswith(b"OK "):
                raise ConnectionError(f"vsock handshake {ack!r}")
            client.sendall((command + "\n").encode())
            return stream.readline().decode().strip()


def ready(vm):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if guest(vm, "ping") == "ready":
                return
        except (OSError, ConnectionError):
            pass
        time.sleep(0.025)
    raise RuntimeError(f"guest not ready: {vm}")


@contextmanager
def machine(args, path):
    path.mkdir()
    flags = ["--api-sock", "api.sock"] if args.engine == "fc" else ["--api-socket", "api.sock", "-v"]
    with (path / "vmm.log").open("wb") as log:
        process = subprocess.Popen([str(args.binary), *flags], cwd=path, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while not (path / "api.sock").is_socket():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f"cannot start {args.binary}: {path / 'vmm.log'}")
                time.sleep(0.01)
            yield path, process.pid
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def golden(args, vm, snapshot):
    if args.engine == "fc":
        api(vm, "/boot-source", {"kernel_image_path": str(args.kernel), "initrd_path": str(args.initramfs),
                                "boot_args": "console=ttyS0 reboot=k panic=1 pci=off rdinit=/init"})
        api(vm, "/machine-config", {"vcpu_count": 1, "mem_size_mib": args.ram_mib})
        api(vm, "/vsock", {"guest_cid": 3, "uds_path": "guest.sock"})
        api(vm, "/actions", {"action_type": "InstanceStart"})
    else:
        config = {
            "cpus": {"boot_vcpus": 1, "max_vcpus": 1},
            "memory": {"size": args.ram_mib << 20, "shared": args.shared},
            "payload": {"kernel": str(args.kernel), "initramfs": str(args.initramfs),
                        "cmdline": "console=hvc0 reboot=k panic=1 rdinit=/init"},
            "vsock": {"cid": 3, "socket": "guest.sock"},
            "console": {"mode": "File", "file": "guest.log"},
            "serial": {"mode": "Off"},
        }
        api(vm, "/api/v1/vm.create", config)
        api(vm, "/api/v1/vm.boot")
    ready(vm)
    checksum = guest(vm, "checksum")
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise RuntimeError(f"invalid golden checksum: {checksum!r}")
    snapshot.mkdir()
    if args.engine == "fc":
        api(vm, "/vm", {"state": "Paused"}, method="PATCH")
        api(vm, "/snapshot/create", {"snapshot_type": "Full", "snapshot_path": str(snapshot / "state"),
                                     "mem_file_path": str(snapshot / "ram")})
    else:
        api(vm, "/api/v1/vm.pause")
        api(vm, "/api/v1/vm.snapshot", {"destination_url": snapshot.as_uri()})
    return checksum


def restore(args, vm, snapshot, digest, mode):
    start = time.monotonic()
    if args.engine == "fc":
        backend = snapshot / "ram"
        if mode == "SeparateFiles":
            backend = vm / "ram-copy"
            if not backend.exists():
                raise RuntimeError(f"missing independent snapshot file {backend}")
        api(vm, "/snapshot/load", {"snapshot_path": str(snapshot / "state"),
                                   "mem_backend": {"backend_type": "File", "backend_path": str(backend)},
                                   "resume_vm": True})
    else:
        api(vm, "/api/v1/vm.restore", {"source_url": snapshot.as_uri(), "memory_restore_mode": mode})
        api(vm, "/api/v1/vm.resume")
    resumed = time.monotonic()
    ready(vm)
    reachable = time.monotonic()
    if guest(vm, "checksum") != digest:
        raise RuntimeError(f"restore checksum differs: {vm}")
    return {"resume_ms": (resumed - start) * 1000, "ready_ms": (reachable - start) * 1000,
            "checked_ms": (time.monotonic() - start) * 1000}


def sample(pid, pattern):
    rollup = Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()
    memory = Path(f"/proc/{pid}/smaps").read_text().splitlines()
    fields = {key: 0 for key in MEMORY_FIELDS}
    maps = []
    selected = False
    for line in memory:
        if MAPPING.match(line):
            selected = line.rstrip().endswith(pattern)
            if selected:
                maps.append(" ".join(line.split()[1:]))
        elif selected:
            name, _, value = line.partition(":")
            if name in fields:
                fields[name] += int(value.split()[0])
    total = {}
    for line in rollup:
        name, _, value = line.partition(":")
        if name in MEMORY_FIELDS:
            total[name] = int(value.split()[0])
    return {"total_kib": total, "file_kib": fields, "file_maps": maps}


def run_mode(args, path, snapshot, digest, mode):
    path.mkdir()
    with ExitStack() as stack, ThreadPoolExecutor(args.vms) as tasks:
        vms = [stack.enter_context(machine(args, path / f"vm{i}")) for i in range(args.vms)]
        if mode == "SeparateFiles":
            for vm, _ in vms:
                subprocess.run(["cp", "--sparse=always", "--reflink=never",
                                snapshot / "ram", vm / "ram-copy"], check=True)
                assert (vm / "ram-copy").stat().st_ino != (snapshot / "ram").stat().st_ino
        futures = [tasks.submit(restore, args, vm, snapshot, digest, mode) for vm, _ in vms]
        timings = [future.result() for future in futures]
        pattern = {"SeparateFiles": "/ram-copy", "SharedFile": "/ram"}.get(mode, "/memory-ranges")
        intact = [sample(pid, pattern) for _, pid in vms]
        mapping_ids = [entry.split()[2:4] for s in intact for entry in s["file_maps"]]
        expects_shared = mode == "SharedFile" or (mode == "CopyOnWrite" and not args.shared)
        if expects_shared and (len(mapping_ids) != args.vms or any(m != mapping_ids[0] for m in mapping_ids)):
            raise RuntimeError("VMs did not map a single shared RAM file")
        changed = guest(vms[0][0], "mutate")
        if changed == digest or guest(vms[0][0], "checksum") != changed:
            raise RuntimeError("mutating guest did not retain its own writes")
        if any(guest(vm, "checksum") != digest for vm, _ in vms[1:]):
            raise RuntimeError("guest write changed another VM")
        after = [sample(pid, pattern) for _, pid in vms]
        print(json.dumps({"mode": mode, "shared_memory_config": args.shared, "vms": args.vms,
                          "isolated_writes": True, "base_checksum": digest, "changed_checksum": changed,
                          "median_ms": {key: statistics.median(t[key] for t in timings) for key in timings[0]},
                          "timings_ms": timings, "before": intact, "after": after}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["ch", "fc"], required=True)
    for name in ("binary", "kernel", "initramfs", "output"):
        parser.add_argument(f"--{name}", type=lambda p: Path(p).resolve(), required=True)
    parser.add_argument("--vms", type=int, default=8)
    parser.add_argument("--ram-mib", type=int, default=512)
    parser.add_argument("--shared", action="store_true", help="use Cloud Hypervisor shared guest RAM")
    parser.add_argument("--modes", nargs="+")
    args = parser.parse_args()
    if args.vms < 2 or (args.shared and args.engine != "ch"):
        parser.error("need two or more VMs; --shared applies to Cloud Hypervisor")
    args.output.mkdir(parents=True)
    snapshot = args.output / "snapshot"
    with machine(args, args.output / "gold") as (vm, _):
        digest = golden(args, vm, snapshot)
    modes = args.modes or (["Copy", "CopyOnWrite"] if args.engine == "ch" else ["SeparateFiles", "SharedFile"])
    for mode in modes:
        run_mode(args, args.output / mode, snapshot, digest, mode)


if __name__ == "__main__":
    main()
