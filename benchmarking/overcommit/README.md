# Actor placement and worker capacity

Compare two microVM WorkerPools on a local kind node with KVM. One uses the normal scheduler. The other opts into the [overcommit policy](../../docs/dev/actor-placement-overcommit.md). Both run one worker with a 2-vCPU, 2,560 MiB cgroup. Both ActorTemplates request 1 vCPU and 2 GiB per guest. Those limits also apply when restoring the templates' golden snapshots.

## Set up the isolated cluster

Use the [microVM local setup](../../docs/dev/microvm-local.md) with an independent kubeconfig and the context name `kind-wabe-dense`. The run used Substrate v1.0.1 system and worker images, Kata 4.3.1 and Cloud Hypervisor v53.0. Only the ateapi binary needs to be built from this branch. Use Metrics Server with a kubelet-compatible configuration and check that `metrics.k8s.io/v1beta1` serves samples.

Place `kubectl-ate` and the kubeconfig at `$OVERCOMMIT_LAB_DIR/kubectl-ate` and `$OVERCOMMIT_LAB_DIR/kubeconfig`. Point Docker at the isolated kind host. Build the guest OCI image from `workload/Dockerfile` with a Linux binary named `workload` in its build context. Push it to the kind registry and set `WORKLOAD_IMAGE` to the resulting digest reference. Build ateapi from `./cmd/ateapi` for the Linux host architecture. In the release ateapi image, replace `/ko-app/ateapi` and push that image to the same registry.

~~~sh
export KUBECONFIG="$OVERCOMMIT_LAB_DIR/kubeconfig"
kubectl --context=kind-wabe-dense apply -f benchmarking/overcommit/lab-rbac.yaml
kubectl --context=kind-wabe-dense -n ate-system create configmap overcommit-config \
  --from-file=benchmarking/overcommit/overcommit.yaml
~~~

Mount that ConfigMap in every ateapi replica. Append `--overcommit=/etc/overcommit/overcommit.yaml` to its existing args and deploy the new image. Wait for the Deployment to finish the rollout before creating the pools. Use the templates in this directory to create one WorkerPool and one ActorTemplate per pool. The ActorTemplate uses the same OCI image in each pool.

~~~sh
"$OVERCOMMIT_LAB_DIR/kubectl-ate" --context=kind-wabe-dense create atespace overcommit-lab
for pool in stock dense; do
  sed "s/POOL/$pool/g" benchmarking/overcommit/worker-pool.yaml.tmpl |
    kubectl --context=kind-wabe-dense apply -f -
done
kubectl --context=kind-wabe-dense -n overcommit-lab get pods
~~~

Wait for both worker pods to be ready. Then create the templates.

~~~sh
for pool in stock dense; do
  sed "s|WORKLOAD_IMAGE|$WORKLOAD_IMAGE|g; s/POOL/$pool/g" benchmarking/overcommit/actor-template.yaml.tmpl |
    "$OVERCOMMIT_LAB_DIR/kubectl-ate" --context=kind-wabe-dense create actor-template -f -
done
"$OVERCOMMIT_LAB_DIR/kubectl-ate" --context=kind-wabe-dense get actor-templates -a overcommit-lab
~~~

Wait for both golden snapshots. Run the client outside the cluster. Start all 10 prepared actors together. Run a minute of file operations with fixed random seeds and a synchronized burst after 20 seconds. Delete the stock actors before measuring the dense pool, then delete the dense actors before the pressure check.

~~~sh
python3 benchmarking/overcommit/bench.py stock --actors 10 --duration 60 --run a
"$OVERCOMMIT_LAB_DIR/kubectl-ate" --context=kind-wabe-dense get actors -a overcommit-lab
# Delete stock-a-00 through stock-a-09 with kubectl-ate delete actor and -a overcommit-lab.
python3 benchmarking/overcommit/bench.py dense --actors 10 --duration 60 --run a
python3 benchmarking/overcommit/summarize.py "$OVERCOMMIT_LAB_DIR"/run-{stock,dense}-a
# Delete dense-a-00 through dense-a-09 and wait for fresh idle PodMetrics.
python3 benchmarking/overcommit/pressure.py
~~~

## Results

One arm64 kind node on a Lima guest with nested KVM, 8 vCPUs and 16 GiB. The workload reads, writes, lists and searches a 500-file workspace. Each burst touches 32 MiB, runs a CPU loop with a 180 ms deadline, and holds the memory for eight seconds. The caller records end-to-end HTTP latency. Kubernetes node stats and the worker's cgroup were sampled about every two seconds.

| Metric | Stock | Overcommit |
| --- | ---: | ---: |
| Running after 10 parallel resumes | 1 | 7 |
| Capacity rejections | 9 | 3 |
| Successful file operations | 19 | 97 |
| File latency p50, whole minute | 31 ms | 32 ms |
| File latency p95, starts before burst | 502 ms | 558 ms |
| File latency p95, starts after burst | 477 ms | 14,568 ms |
| Resume latency p50 | 441 ms | 2,928 ms |
| Worker cgroup memory, idle mean | 113 MiB | 686 MiB |
| Worker cgroup memory, peak under load | 151 MiB | 942 MiB |
| Worker CPU, mean / peak under load | 0.04 / 0.10 cores | 0.76 / 2.00 cores |
| Whole node CPU, mean / peak under load | 0.43 / 0.49 cores | 1.24 / 2.32 cores |
| Whole node memory working set, peak under load | 2,928 MiB | 3,756 MiB |
| Worker throttling periods during workload | 0 | 220 |
| Failed file operations / cgroup OOM kills / worker restarts / evictions | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |

These are single-run figures. The successful dense Actor limits totaled 7 vCPUs and 14 GiB. Both workers had identical cgroup settings throughout. The rejections left Actors suspended. Seven concurrent bursts saturated CPU and produced long searches, while the worker's measured memory stayed below its limit.

## Live memory pressure

The pressure check first admitted a sixth Actor with five Actors already running. It deleted and re-created that candidate, then held a 352 MiB allocation in one of the five guests. With 853 MiB of worker working set and 0.14 cores in the metrics sample, the next resume returned ResourceExhausted. The measured memory and six bookings exceed the placement target, while the measured CPU and six bookings fit. The guest returned HTTP 200 after releasing the allocation. The rejected Actor remained suspended and successfully resumed after the load was removed.
