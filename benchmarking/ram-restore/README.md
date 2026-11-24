# Guest RAM sharing on KVM

This benchmark restores eight 512 MiB guests from one snapshot per configuration. A guest init program writes one nonzero byte per page in a 64 MiB payload before snapshotting, then checksums it on request over vsock. After all guests verify the original payload, one guest writes to its first 32 MiB. The benchmark checks the resulting checksum in every guest and records the VMM process memory before and after the write.

The test uses the arm64 Kata 4.3.1 guest kernel from the Substrate v1.0.1 installation assets. The guest uses an initramfs, no disks or virtio-fs. The Cloud Hypervisor shared-RAM runs test the same guest with the shared-RAM setting used by the stock Kata worker. Firecracker's control uses independently copied snapshot files with distinct inodes, with file copies outside the measured interval.

## Run

From the repository root on a Linux arm64 host with KVM and the pinned binaries:

~~~sh
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -o /var/tmp/ram-guest ./benchmarking/ram-restore
sudo benchmarking/ram-restore/make-initramfs.sh /var/tmp/ram-guest /var/tmp/ram-initrd.cpio

sudo python3 benchmarking/ram-restore/compare.py --engine ch \
  --binary "$CLOUD_HYPERVISOR" --kernel "$KATA_KERNEL" \
  --initramfs /var/tmp/ram-initrd.cpio --output /var/tmp/ram-ch-private \
  --modes Copy CopyOnWrite OnDemandNoBackgroundFetchPages

sudo python3 benchmarking/ram-restore/compare.py --engine ch --shared \
  --binary "$CLOUD_HYPERVISOR" --kernel "$KATA_KERNEL" \
  --initramfs /var/tmp/ram-initrd.cpio --output /var/tmp/ram-ch-shared

sudo python3 benchmarking/ram-restore/compare.py --engine fc \
  --binary "$FIRECRACKER" --kernel "$KATA_KERNEL" \
  --initramfs /var/tmp/ram-initrd.cpio --output /var/tmp/ram-fc
~~~

Use fresh output directories on the Linux host's local filesystem. The test used a Lima guest with nested KVM, 8 vCPUs, 16 GiB, and Linux 7.0.0-38-generic on arm64. The ready time runs from sending the restore command until the guest responds to a ping. VMM process startup and Firecracker's control file copies happen before timing. Payload check time includes the first full 64 MiB scan. The PSS figures sum the eight VMM processes after the scan. They exclude unmapped page cache and host infrastructure. [Local microVM setup](../../docs/dev/microvm-local.md)

| Runtime and restore | Total RSS | Total PSS | Median restore and resume | Median ready | Median payload check |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cloud Hypervisor v55.0 private RAM, Copy | 4,137 MiB | 4,106 MiB | 681 ms | 699 ms | 856 ms |
| Cloud Hypervisor v55.0 private RAM, CopyOnWrite | 831 MiB | 114 MiB | 16 ms | 397 ms | 2,318 ms |
| Cloud Hypervisor v55.0 private RAM, OnDemandNoBackgroundFetchPages | 584 MiB | 552 MiB | 11 ms | 222 ms | 12,099 ms |
| Cloud Hypervisor v55.0 shared RAM, Copy | 883 MiB | 852 MiB | 63 ms | 180 ms | 2,096 ms |
| Cloud Hypervisor v55.0 shared RAM, CopyOnWrite requested | 883 MiB | 852 MiB | 34 ms | 150 ms | 5,116 ms |
| Firecracker v1.18.0, separate snapshot files | 665 MiB | 648 MiB | 6 ms | 557 ms | 2,196 ms |
| Firecracker v1.18.0, one snapshot file | 727 MiB | 100 MiB | 7 ms | 639 ms | 3,766 ms |

The shared-file runs showed private writable mappings of one Linux inode across all eight VMMs. Cloud Hypervisor's eight RAM mappings accounted for 104 MiB of PSS and Firecracker's accounted for 95 MiB. The writes in one guest added about 32 MiB of private dirty RAM to its mapping. Every other guest retained the original checksum. A repeat of the Firecracker comparison used 651 and 100 MiB PSS. The cache was not flushed between runs. A fast restore API response can leave more page faults for the first read, and the latency depends on cache state and concurrency.

## Agent Substrate integration

Agent Substrate v1.0.1 ships Cloud Hypervisor v53.0. Its worker requests eager restore on this version and uses shared RAM for the out-of-process virtio-fs daemon. Cloud Hypervisor v55.0 logs a fallback to Copy when asked for CoW with this setting. Both shared-RAM modes above used approximately 852 MiB of PSS. The stock Kata worker needs a different filesystem device to use private guest RAM. [Pinned worker configuration](https://github.com/agent-substrate/substrate/blob/20778a6dbb01f13024642851bb592bf67c9fec05/cmd/ateworker-microvm/run.go#L815-L835) · [Cloud Hypervisor v55.0 restore modes](https://github.com/cloud-hypervisor/cloud-hypervisor/blob/v55.0/docs/snapshot_restore.md#copy-on-write-restore)

For shared snapshots, atelet currently creates one sparse copy per microVM. A CoW worker needs to reuse and retain the same immutable cache inode for guest RAM. Checkpointing must write separate state and account for the hypervisor's CoW snapshot format. The default worker's fallback for a snapshot that is not self-contained assumes OnDemand's delta semantics. [Snapshot cache](../../cmd/atelet/snapshotcache.go) · [Checkpoint](../../cmd/ateworker-microvm/checkpoint.go)

One integration path keeps Cloud Hypervisor and offers an alternate sandbox configuration backed by a guest block device. The worker would still need a way to construct OCI root filesystems and expose durable data to the guest. A Firecracker worker also fits behind the existing WorkerPool image and worker RPC boundary. Its implementation would need guest lifecycle, vsock, networking, volume handling, and snapshot compatibility tests. This experiment modifies neither worker. [Worker protocol](../../internal/proto/ateworkerpb/ateworker.proto) · [Firecracker snapshot format](https://github.com/firecracker-microvm/firecracker/blob/v1.18.0/docs/snapshotting/snapshot-support.md)
