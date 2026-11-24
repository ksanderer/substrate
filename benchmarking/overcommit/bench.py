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

"""Compare a fixed batch of Actor starts and live requests from outside kind."""

import argparse
import concurrent.futures
import contextlib
import datetime
import http.client
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse

lab = pathlib.Path(os.environ["OVERCOMMIT_LAB_DIR"]).resolve()
env = os.environ | {"KUBECONFIG": str(lab / "kubeconfig")}
kube = ["kubectl", "--context=kind-wabe-dense"]
ate = [str(lab / "kubectl-ate"), "--context=kind-wabe-dense"]
space = "overcommit-lab"
parser = argparse.ArgumentParser()
parser.add_argument("pool", choices=["stock", "dense"])
parser.add_argument("--actors", type=int, required=True)
parser.add_argument("--duration", type=int, default=60)
parser.add_argument("--run", required=True)
args = parser.parse_args()
prefix = f"{args.pool}-{args.run}"
logs = lab / f"run-{prefix}"
logs.mkdir()
start = time.monotonic()
lock = threading.Lock()
stop = threading.Event()

def command(argv, timeout=180):
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)

def run(argv, timeout=180):
    proc = command(argv, timeout)
    if proc.returncode:
        raise RuntimeError(f"{proc.args}: {proc.stderr}")
    return proc.stdout

def emit(file, **event):
    with lock, (logs / f"{file}.jsonl").open("a") as out:
        print(json.dumps({"t": time.monotonic() - start, **event}), file=out)

def call(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=65)
    t = time.monotonic()
    status, body = 0, ""
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        status, body = resp.status, resp.read().decode().strip()
    except (OSError, http.client.HTTPException) as exc:
        body = repr(exc)
    finally:
        conn.close()
    return status, body, (time.monotonic() - t) * 1000

def sample(node, worker, group):
    # Read the worker container cgroup directly plus the whole Kubernetes node.
    while not stop.is_set():
        try:
            raw = run(kube + ["get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary"], timeout=6)
            stats = json.loads(raw)
            pod = next(e for e in stats["pods"] if e["podRef"]["name"] == worker)
            ctr = next(e for e in pod["containers"] if e["name"] == "worker")
            raw = run(["docker", "exec", node, "sh", "-c",
                       'for file in memory.current memory.max memory.events memory.stat cpu.stat memory.pressure cpu.max; do printf "%s\\n" "$file"; cat "$1/$file"; done',
                       "read-cgroup", group], timeout=6)
            emit("sample", node=stats["node"], worker=ctr, cgroup=raw)
        except Exception as exc:
            emit("sample", error=repr(exc))
        stop.wait(2)

def agent(i, actor, port, deadline, gate):
    rng = random.Random(20261124 + i)
    gate.wait()
    while time.monotonic() < deadline:
        nap = min(rng.uniform(1, 5), max(0, deadline - time.monotonic()))
        time.sleep(nap)
        if time.monotonic() >= deadline:
            break
        op = rng.choice(["read", "write", "list", "search"])
        path = f"/fs?op={op}&i={rng.randrange(500)}"
        status, body, ms = call(port, path)
        valid = status == 200 and body.isdecimal() and (int(body) == 500 if op in ("list", "search") else int(body) > 0)
        emit("ops", actor=actor, op=op, status=status, valid=valid, body=body, ms=ms)

def burst(actor, port, mib, cpu_ms, hold, gate):
    gate.wait()
    query = urllib.parse.urlencode(dict(mib=mib, ms=cpu_ms, hold=hold))
    status, body, ms = call(port, f"/burst?{query}")
    emit("ops", actor=actor, op="burst", status=status, valid=status == 200, body=body, ms=ms)

def proxy(actor, stack):
    proxylog = stack.enter_context((logs / f"{actor}.proxy.log").open("w+"))
    proc = subprocess.Popen(ate + ["proxy", "actor", actor, "0:80", "-a", space],
                            env=env, stdout=proxylog, stderr=proxylog)
    def cleanup():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    stack.callback(cleanup)
    until = time.monotonic() + 45
    while time.monotonic() < until:
        content = (logs / f"{actor}.proxy.log").read_text()
        match = re.search(r"Proxying 127\.0\.0\.1:(\d+)", content)
        if match is not None:
            return int(match[1])
        if proc.poll() is not None:
            raise RuntimeError(content)
        time.sleep(.1)
    raise TimeoutError(f"proxy {actor}: {content}")

def resume(actor, gate):
    gate.wait()
    t = time.monotonic()
    p = command(ate + ["resume", "actor", actor, "-a", space, "-o", "json"])
    result = dict(actor=actor, status=p.returncode, ms=(time.monotonic() - t) * 1000, error=p.stderr)
    if not p.returncode:
        result["response"] = json.loads(p.stdout)
    emit("start", **result)
    return not p.returncode

def actor_states(file):
    actors = run(ate + ["get", "actors", "-a", space, "-o", "json"])
    (logs / file).write_text(actors)

def main():
    pod = json.loads(run(kube + ["-n", space, "get", "pods", "-l", f"ate.dev/worker-pool={args.pool}", "-o", "json"]))["items"]
    assert len(pod) == 1, pod
    pod = pod[0]
    worker, node, uid = pod["metadata"]["name"], pod["spec"]["nodeName"], pod["metadata"]["uid"]
    container = next(s for s in pod["status"]["containerStatuses"] if s["name"] == "worker")
    cid = container["containerID"].split("://")[1]
    group = run(["docker", "exec", node, "find", "/sys/fs/cgroup",
                 "-type", "d", "-name", f"cri-containerd-{cid}.scope"]).strip()
    assert group, f"worker cgroup not found for {worker} {uid}"
    emit("config", worker=worker, node=node, cgroup=group, actors=args.actors, duration=args.duration,
         when=datetime.datetime.now(datetime.UTC).isoformat())
    workers = threading.Thread(target=sample, args=(node, worker, group), daemon=True)
    workers.start()
    actors = [f"{prefix}-{i:02}" for i in range(args.actors)]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(actors)) as tasks:
            def create(actor):
                return run(ate + ["create", "actor", actor, "-a", space, "--template", args.pool])
            list(tasks.map(create, actors))
            actor_states("before.json")
            gate = threading.Event()
            futures = [tasks.submit(resume, actor, gate) for actor in actors]
            emit("phase", phase="start")
            gate.set()
            active = [actor for actor, future in zip(actors, futures) if future.result()]
            actor_states("after-start.json")
        print(f"{args.pool}: {len(active)} / {len(actors)} running", flush=True)
        if not active:
            sys.exit(1)
        with contextlib.ExitStack() as stack, concurrent.futures.ThreadPoolExecutor(max_workers=2 * len(active)) as tasks:
            ports = list(tasks.map(lambda actor: proxy(actor, stack), active))
            gate = threading.Event()
            for actor, port in zip(active, ports):
                status, body, ms = call(port, "/readyz")
                emit("ops", actor=actor, op="ready", status=status, body=body, valid=status == 200, ms=ms)
                if status != 200:
                    raise RuntimeError(f"{actor}: {status} {body}")
            emit("phase", phase="idle")
            stop.wait(16)
            emit("phase", phase="work")
            deadline = time.monotonic() + args.duration
            requests = [tasks.submit(agent, i, actor, port, deadline, gate)
                        for i, (actor, port) in enumerate(zip(active, ports))]
            gate.set()
            if args.duration >= 30:
                stop.wait(args.duration / 3)
                emit("phase", phase="burst")
                burst_gate = threading.Event()
                bursts = [tasks.submit(burst, actor, port, 32, 180, "8s", burst_gate)
                          for actor, port in zip(active, ports)]
                burst_gate.set()
                for request in bursts:
                    request.result()
            for request in requests:
                request.result()
        emit("phase", phase="end")
        actor_states("after-work.json")
        (logs / "worker-pod.json").write_text(run(kube + ["-n", space, "get", "pod", worker, "-o", "json"]))
        (logs / "events.json").write_text(run(kube + ["-n", space, "get", "events", "-o", "json"]))
    finally:
        stop.set()
        workers.join(timeout=8)

if __name__ == "__main__":
    main()
