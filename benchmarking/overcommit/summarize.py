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

import json
import pathlib
import re
import statistics
import sys

def rows(root, name):
    file = root / f"{name}.jsonl"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

def summary_latency(events):
    times = sorted(e["ms"] for e in events)
    if not times:
        return {}
    return dict(n=len(times), p50_ms=round(statistics.median(times), 2),
                p95_ms=round(statistics.quantiles(times, n=100, method="inclusive")[94] if len(times) > 1 else times[0], 2),
                max_ms=round(times[-1], 2))

def measure(start, end, samples):
    series = [r for r in samples if start <= r["t"] < end and "error" not in r]
    if not series:
        return {}
    out = {"samples": len(series)}
    for obj in ("node", "worker"):
        for dimension, key, divisor in (("cpu_cores", ("cpu","usageNanoCores"), 10**9),
                                        ("working_set_mib", ("memory","workingSetBytes"), 2**20)):
            values = [s[obj][key[0]][key[1]] / divisor for s in series if key[1] in s[obj][key[0]]]
            if values:
                out[f"{obj}_{dimension}"] = {stat: round(fn(values), 2)
                                            for stat, fn in (("mean", statistics.mean), ("max", max))}
    raw = [r["cgroup"].splitlines() for r in series]
    current = [int(e[e.index("memory.current") + 1]) / (2**20) for e in raw]
    out["worker_cgroup_memory_mib"] = dict(mean=round(statistics.mean(current), 2), peak=round(max(current), 2))
    out["worker_cgroup_memory_max_mib"] = int(raw[-1][raw[-1].index("memory.max") + 1]) / (2**20)
    out["worker_cgroup_cpu_max"] = raw[-1][raw[-1].index("cpu.max")+1]
    for name in ("usage_usec", "nr_throttled", "oom", "oom_kill", "oom_group_kill"):
        first, last = (next(int(e.split()[1]) for e in s if e.startswith(name + " ")) for s in (raw[0], raw[-1]))
        out[name + "_delta"] = last - first
    return out

for path in sys.argv[1:]:
    root = pathlib.Path(path)
    starts = rows(root, "start")
    ops = rows(root, "ops")
    samples = rows(root, "sample")
    phases = {e["phase"]: e["t"] for e in rows(root, "phase")}
    out = dict(run=root.name, attempted=len(starts), successful_starts=summary_latency([e for e in starts if not e["status"]]),
               rejected=[{"actor":e["actor"],"error":e["error"].strip()} for e in starts if e["status"]],
               failed_ops=[e for e in ops if not e["valid"]],
               metrics_error_count=sum("error" in s for s in samples))
    for op in sorted({r["op"] for r in ops}):
        out[op] = summary_latency([e for e in ops if e["op"] == op and e["valid"]])
    out["files"] = summary_latency([e for e in ops if e["op"] in ("read","write","list","search") and e["valid"]])
    if "end" in phases:
        out["startup_resources"] = measure(phases["start"], phases["idle"], samples)
        out["idle_resources"] = measure(phases["idle"], phases["work"], samples)
        out["work_resources"] = measure(phases["work"], phases["end"], samples)
        out["whole_run"] = measure(0, phases["end"] + 8, samples)
    print(json.dumps(out))
