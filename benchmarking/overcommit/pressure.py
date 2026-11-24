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

"""Check live working-set admission with a single 352 MiB allocation."""

import concurrent.futures
import contextlib
import http.client
import json
import os
import pathlib
import re
import subprocess
import threading
import time

lab = pathlib.Path(os.environ["OVERCOMMIT_LAB_DIR"]).resolve()
env = os.environ | {"KUBECONFIG": str(lab / "kubeconfig")}
kubectl = ["kubectl", "--context=kind-wabe-dense"]
ate = [str(lab / "kubectl-ate"), "--context=kind-wabe-dense"]
space = "overcommit-lab"
t0 = time.monotonic()

def command(argv, expected=True):
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=90)
    if expected and proc.returncode:
        raise RuntimeError(proc.stderr)
    return proc

def emit(**event):
    print(json.dumps({"t": time.monotonic() - t0, **event}), flush=True)

def call(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=125)
    try:
        conn.request("GET", "/burst?mib=352&ms=0&hold=58s")
        res = conn.getresponse()
        emit(event="burst-response", status=res.status, body=res.read().decode().strip())
        if res.status != 200:
            raise RuntimeError("burst failed")
    finally:
        conn.close()

def proxy(actor, stack):
    proxylog = stack.enter_context((lab / f"pressure-{actor}.log").open("w+"))
    proc = subprocess.Popen(ate + ["proxy", "actor", actor, "0:80", "-a", space],
                            env=env, stdout=proxylog, stderr=proxylog)
    def cleanup():
        proc.terminate()
        proc.wait(timeout=5)
    stack.callback(cleanup)
    until = time.monotonic() + 15
    while time.monotonic() < until:
        proxylog.seek(0)
        match = re.search(r"Proxying 127\.0\.0\.1:(\d+)", proxylog.read())
        if match:
            return int(match[1])
        time.sleep(.1)
    raise TimeoutError(actor)

def sample():
    output = command(kubectl + ["get", "--raw", "/apis/metrics.k8s.io/v1beta1/namespaces/overcommit-lab/pods"]).stdout
    pod = next(p for p in json.loads(output)["items"] if p["metadata"]["name"].startswith("dense-"))
    use = next(c["usage"] for c in pod["containers"] if c["name"] == "worker")
    memory = quantity(use["memory"], {"": 1, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30})
    cpu = quantity(use["cpu"], {"": 1, "m": 1e-3, "u": 1e-6, "n": 1e-9})
    return pod["timestamp"], memory / 2**20, cpu

def quantity(value, units):
    match = re.fullmatch(r"(\d+)(\D*)", value)
    if match is None or match[2] not in units:
        raise ValueError(f"unexpected Kubernetes quantity: {value}")
    return int(match[1]) * units[match[2]]

def main():
    actors = [f"pressure-live-{i}" for i in range(5)]
    candidate = "pressure-candidate"
    for actor in actors + [candidate]:
        p = command(ate + ["create", "actor", actor, "-a", space, "--template", "dense"], expected=False)
        if p.returncode and "AlreadyExists" not in p.stderr:
            raise RuntimeError(p.stderr)
    def resume(actor):
        end = time.monotonic() + 45
        while True:
            resp = command(ate + ["resume", "actor", actor, "-a", space], expected=False)
            emit(event="admission", actor=actor, status=resp.returncode, sample=sample())
            if not resp.returncode:
                return
            if "ResourceExhausted" not in resp.stderr or time.monotonic() >= end:
                raise RuntimeError(resp.stderr)
            time.sleep(2)
    for actor in actors + [candidate]:
        resume(actor)
    command(ate + ["delete", "actor", candidate, "-a", space])
    command(ate + ["create", "actor", candidate, "-a", space, "--template", "dense"])
    emit(event="before-pressure", sample=sample())
    with contextlib.ExitStack() as stack, concurrent.futures.ThreadPoolExecutor(max_workers=1) as tasks:
        port = proxy(actors[0], stack)
        request = tasks.submit(call, port)
        end = time.monotonic() + 100
        while True:
            timestamp, memory, cpu = sample()
            emit(event="wait-for-pressure", sample=[timestamp, memory, cpu])
            if memory >= 700 and cpu < .29:
                break
            if time.monotonic() >= end:
                raise TimeoutError("PodMetrics did not show pressure")
            time.sleep(2)
        # The control plane polls PodMetrics every 3 seconds in this lab.
        time.sleep(4)
        resp = command(ate + ["resume", "actor", candidate, "-a", space], expected=False)
        emit(event="under-pressure", code=resp.returncode, error=resp.stderr.strip())
        if resp.returncode == 0 or "ResourceExhausted" not in resp.stderr:
            raise RuntimeError("expected capacity rejection")
        request.result()
    for actor in actors:
        command(ate + ["delete", "actor", actor, "-a", space])
    end = time.monotonic() + 45
    while True:
        timestamp, memory, cpu = sample()
        emit(event="wait-for-release", sample=[timestamp, memory, cpu])
        if memory < 500:
            break
        if time.monotonic() >= end:
            raise TimeoutError("PodMetrics did not show release")
        time.sleep(2)
    time.sleep(4)
    resume(candidate)
    emit(event="retry", status="running")
    command(ate + ["delete", "actor", candidate, "-a", space])

if __name__ == "__main__":
    main()
