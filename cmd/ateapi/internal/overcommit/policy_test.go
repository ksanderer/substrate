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

package overcommit

import (
	"testing"
	"time"

	"go.ate.dev/substrate/api/ateapi/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	metricsv1 "k8s.io/metrics/pkg/apis/metrics/v1beta1"
	"sigs.k8s.io/yaml"
)

func TestPolicy(t *testing.T) {
	const mi = 1 << 20
	var cfg config
	if err := yaml.UnmarshalStrict([]byte(`workerPool: lab/dense
startupCPU: 100m
startupMemory: 128Mi
targetUtilizationPercent: 70
metricsInterval: 5s
metricsMaxAge: 45s
`), &cfg); err != nil {
		t.Fatal(err)
	}
	p, err := newPolicy(cfg)
	if err != nil {
		t.Fatal(err)
	}
	worker := &ateapi.Worker{
		WorkerNamespace: "lab", WorkerPool: "dense", WorkerPod: "dense-worker",
		Status: &ateapi.WorkerStatus{Capacity: &ateapi.WorkerResources{
			CpuMillicores: 2000, MemoryBytes: 2500 * mi, Actors: 1000,
		}},
	}
	limits := &ateapi.Resources{CpuMillicores: 2000, MemoryBytes: 4096 * mi}
	booking := p.Booking(worker, limits)
	if booking.GetCpuMillicores() != 100 || booking.GetMemoryBytes() != 128*mi {
		t.Fatalf("booking = %v", booking)
	}
	if limits.GetCpuMillicores() != 2000 || limits.GetMemoryBytes() != 4096*mi {
		t.Fatalf("booking changed the sandbox limits: %v", limits)
	}
	if got, ok := p.Capacity(worker); ok {
		t.Fatalf("worker with no sample has capacity %v", got)
	}
	sample := metricsv1.PodMetrics{
		ObjectMeta: metav1.ObjectMeta{Name: "dense-worker"},
		Timestamp:  metav1.Now(),
		Containers: []metricsv1.ContainerMetrics{{
			Name: "worker",
			Usage: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("700Mi"),
			},
		}},
	}
	p.update([]metricsv1.PodMetrics{sample})
	got, ok := p.Capacity(worker)
	if !ok || got.GetCpuMillicores() != 1200 || got.GetMemoryBytes() != 1050*mi || got.GetActors() != 1000 {
		t.Fatalf("capacity = %v, %v", got, ok)
	}
	sample.Timestamp = metav1.NewTime(time.Now().Add(-time.Minute))
	p.update([]metricsv1.PodMetrics{sample})
	if got, ok := p.Capacity(worker); ok {
		t.Fatalf("worker with stale sample has capacity %v", got)
	}
	worker.WorkerPool = "stock"
	if got, ok := p.Capacity(worker); !ok || got != worker.Status.Capacity {
		t.Fatalf("stock capacity = %v, %v", got, ok)
	}
	if got := p.Booking(worker, limits); got != limits {
		t.Fatalf("stock booking = %v", got)
	}
}
