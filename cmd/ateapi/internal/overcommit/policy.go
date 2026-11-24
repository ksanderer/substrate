// Copyright 2026 The Agent Substrate Authors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Package overcommit provides an opt-in placement policy for one worker pool.
package overcommit

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

	"go.ate.dev/substrate/api/ateapi/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/rest"
	metricsv1 "k8s.io/metrics/pkg/apis/metrics/v1beta1"
	metricsclient "k8s.io/metrics/pkg/client/clientset/versioned"
	"sigs.k8s.io/yaml"
)

type config struct {
	WorkerPool      string            `json:"workerPool"`
	StartupCPU      resource.Quantity `json:"startupCPU"`
	StartupMemory   resource.Quantity `json:"startupMemory"`
	TargetPercent   int64             `json:"targetUtilizationPercent"`
	MetricsInterval metav1.Duration   `json:"metricsInterval"`
	MetricsMaxAge   metav1.Duration   `json:"metricsMaxAge"`
}

type usage struct {
	at     time.Time
	cpu    int64
	memory int64
}

// Policy bounds placements by observed usage and the sum of startup bookings.
// A nil policy leaves every worker on the default limits-based scheduler.
type Policy struct {
	namespace     string
	pool          string
	startupCPU    int64
	startupMemory int64
	targetPercent int64
	maxAge        time.Duration

	mu   sync.RWMutex
	pods map[string]usage
}

// Start loads the policy and polls the Kubernetes resource metrics API. Only an
// opt-in pool needs access to PodMetrics.
func Start(ctx context.Context, configPath string) (*Policy, error) {
	if configPath == "" {
		return nil, nil
	}
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("read overcommit configuration: %w", err)
	}
	var cfg config
	if err := yaml.UnmarshalStrict(data, &cfg); err != nil {
		return nil, fmt.Errorf("decode overcommit configuration: %w", err)
	}
	p, err := newPolicy(cfg)
	if err != nil {
		return nil, err
	}
	restConfig, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("get Kubernetes metrics configuration: %w", err)
	}
	client, err := metricsclient.NewForConfig(restConfig)
	if err != nil {
		return nil, fmt.Errorf("create Kubernetes metrics client: %w", err)
	}
	pods := client.MetricsV1beta1().PodMetricses(p.namespace)
	go wait.UntilWithContext(ctx, func(ctx context.Context) {
		callCtx, cancel := context.WithTimeout(ctx, cfg.MetricsInterval.Duration)
		defer cancel()
		metrics, err := pods.List(callCtx, metav1.ListOptions{})
		if err != nil {
			slog.WarnContext(ctx, "Could not refresh overcommit usage", "workerPool", cfg.WorkerPool, "error", err)
			return
		}
		p.update(metrics.Items)
	}, cfg.MetricsInterval.Duration)
	return p, nil
}

func newPolicy(cfg config) (*Policy, error) {
	namespace, pool, ok := strings.Cut(cfg.WorkerPool, "/")
	if !ok || len(validation.IsDNS1123Label(namespace)) != 0 || len(validation.IsDNS1123Subdomain(pool)) != 0 {
		return nil, fmt.Errorf("overcommit workerPool must be a namespace/name")
	}
	cpu, memory := cfg.StartupCPU.MilliValue(), cfg.StartupMemory.Value()
	if cpu <= 0 || memory <= 0 {
		return nil, fmt.Errorf("overcommit startupCPU and startupMemory must be positive")
	}
	if cfg.TargetPercent <= 0 || cfg.TargetPercent >= 100 {
		return nil, fmt.Errorf("overcommit targetUtilizationPercent must be between 1 and 99")
	}
	if cfg.MetricsInterval.Duration <= 0 || cfg.MetricsMaxAge.Duration <= cfg.MetricsInterval.Duration {
		return nil, fmt.Errorf("overcommit metricsMaxAge must be greater than a positive metricsInterval")
	}
	return &Policy{
		namespace: namespace, pool: pool, startupCPU: cpu, startupMemory: memory,
		targetPercent: cfg.TargetPercent, maxAge: cfg.MetricsMaxAge.Duration,
	}, nil
}

func (p *Policy) applies(worker *ateapi.Worker) bool {
	return p != nil && worker.GetWorkerNamespace() == p.namespace && worker.GetWorkerPool() == p.pool
}

// Booking returns the estimated guest startup demand. Scheduling adds runtime
// overhead based on the full guest size.
func (p *Policy) Booking(worker *ateapi.Worker, limits *ateapi.Resources) *ateapi.Resources {
	if !p.applies(worker) {
		return limits
	}
	return &ateapi.Resources{
		CpuMillicores: min(limits.GetCpuMillicores(), p.startupCPU),
		MemoryBytes:   min(limits.GetMemoryBytes(), p.startupMemory),
	}
}

// Capacity removes measured use from the placement target. The scheduler
// subtracts all persisted startup bookings from the result, so concurrent
// starts are accounted for even when the latest metrics do not include them.
func (p *Policy) Capacity(worker *ateapi.Worker) (*ateapi.WorkerResources, bool) {
	capacity := worker.GetStatus().GetCapacity()
	if !p.applies(worker) {
		return capacity, true
	}
	p.mu.RLock()
	use, ok := p.pods[worker.GetWorkerPod()]
	p.mu.RUnlock()
	age := time.Since(use.at)
	if !ok || age < 0 || age > p.maxAge {
		return nil, false
	}
	return &ateapi.WorkerResources{
		CpuMillicores: max(scale(capacity.GetCpuMillicores(), p.targetPercent)-use.cpu, 0),
		MemoryBytes:   max(scale(capacity.GetMemoryBytes(), p.targetPercent)-use.memory, 0),
		Actors:        capacity.GetActors(),
	}, true
}

func scale(value, percent int64) int64 {
	return value/100*percent + value%100*percent/100
}

func (p *Policy) update(metrics []metricsv1.PodMetrics) {
	pods := make(map[string]usage, len(metrics))
	for _, pod := range metrics {
		for _, ctr := range pod.Containers {
			if ctr.Name != "worker" {
				continue
			}
			cpu, hasCPU := ctr.Usage[corev1.ResourceCPU]
			memory, hasMemory := ctr.Usage[corev1.ResourceMemory]
			if hasCPU && hasMemory && cpu.Sign() >= 0 && memory.Sign() > 0 {
				pods[pod.Name] = usage{at: pod.Timestamp.Time, cpu: cpu.MilliValue(), memory: memory.Value()}
			}
		}
	}
	p.mu.Lock()
	p.pods = pods
	p.mu.Unlock()
}
