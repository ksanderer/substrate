# Experimental actor overcommit

`ateapi --overcommit=/path/to/overcommit.yaml` opts one WorkerPool into overcommit. Worker and ActorTemplate resource limits still enforce the runtime ceilings.

```yaml
workerPool: lab/dense
startupCPU: 100m
startupMemory: 128Mi
targetUtilizationPercent: 70
metricsInterval: 5s
metricsMaxAge: 45s
```

The quantities above are example inputs. Measure startup demand for the images and sandbox class used by the pool. The ateapi service account needs permission to list `pods` in the `metrics.k8s.io` API group in the worker namespace. The policy keeps a cache of the worker container's CPU and memory working set. A missing or expired sample prevents placement into this pool.

The scheduler books the smaller of the configured startup estimate and the Actor limit, plus runtime overhead. It subtracts both the latest measured usage and persisted startup bookings from the utilization target. Counting all bookings covers starts that have yet to appear in telemetry. It is conservative for actors already reflected in the sample. The check runs again under the existing worker row lock before committing an assignment. The actor-slot limit and placement restrictions still apply.

For this pool, the worker's reported allocations and assignment resources show placement bookings. ActorTemplate limits and the limits passed to the guest still show the requested capacity.

Configure every ateapi replica before using the pool. Drain the pool to zero assignments before changing or removing the policy. Active assignments keep the booking they received on placement, so mixing policies during a rollout can produce inconsistent admission decisions.

PodMetrics reports delayed CPU averages and memory working set. Correlated bursts and snapshot writes can still pressure the worker cgroup. Workers keep their configured limits. Use a dedicated pool and compare cgroup memory, memory pressure, errors, and latency while actors stay running. This experiment is scoped to placement. See the upstream discussions on [actor resource requests](https://github.com/agent-substrate/substrate/issues/3997), [worker overcommit](https://github.com/agent-substrate/substrate/issues/3441), and [memory pressure](https://github.com/agent-substrate/substrate/issues/3944).
