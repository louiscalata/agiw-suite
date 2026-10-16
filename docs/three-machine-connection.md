# Three machines, specialized roles

AGIW Suite is being developed to coordinate useful AI work across an Apple-silicon Mac, a Windows PC and a Linux Mac Pro. Each worker can use an appropriate local model; the workflow retains task results, quality checks and model-lifecycle evidence.

**Development checkpoint · 16 October 2026 UTC.** Selected Linux and PC code samples passed their bounded checks; the Mac advisory returned and unloaded, but its advice was rejected. Production routing across all three machines and a simultaneous throughput benchmark remain in progress. The downloadable macOS Inference Monitor is a separate release.

## The intended topology

<img src="assets/three-machine-architecture.svg" width="1280" alt="Tools feed Nisi and Jev, optional PAIR transport, then PC, Linux and Mac workers. Windows has separate NVIDIA RTX 3080 Ti and AMD RX 5700 XT lanes. Linux has RX 580 inference, NVMe workspace/cache and HDD backup storage. Results return through output checks and review.">

Nisi v0.2 and Jev provide the workflow and task-analysis layer. NVIDIA PAIR can transport independent requests to eligible engines; it does not combine the computers' RAM or VRAM into one model. The PC's two GPUs offer distinct placement options. Native admission and a measured workload must establish whether both can be used together for a given configuration.

| Machine | Resources and intended role | Current connection and acceptance state |
| --- | --- | --- |
| **Windows PC · two GPUs** | NVIDIA RTX 3080 Ti 12 GiB for CUDA; AMD RX 5700 XT 8 GiB for Vulkan. Specialized coding and fast advisory lanes. | One native Author sample and its owned cleanup passed. Central enrollment is **off**; complete four-role and dual-GPU routed acceptance remain open. |
| **Linux Mac Pro 5,1** | Dual Xeon X5675 CPUs; 12 physical / 24 logical cores; 128 GB installed RAM; RX 580 8 GiB. Samsung system SSD, separate NVMe workspace/cache and two HDD backup volumes. | Pinned SSH works after reboot without a desktop login. A transient background user service successfully opened the GPU render device, exited 0 and was collected, with zero inference calls. The old coordinator is disabled/inactive; the new service is **not installed**, with its source staged. Production inference before login remains unverified. |
| **Apple-silicon Mac** | M5 Pro and 64 GiB unified memory. Interactive work, model review and the monitor interface. | Recorded local runs produced answers and verified exact model unloading. Current process admission and complete four-role acceptance remain open. |

The Linux coding sample uses a reviewed SSE/Vulkan runtime suited to the older Xeons. It did not require AVX or AVX2 CPU support. Storage roles do not make the HDDs equivalent to NVMe speed or combine the drives into a RAID volume.

## Latest retained local samples · not concurrent

<img src="assets/three-machine-local-rates.svg" width="1280" alt="Separate local generation samples: Linux Qwen3.5-4B Q6_K 36.84 tokens per second with 16 semantic cases passed; Mac Qwen3.8-27B MLX 16.39 with semantic review rejected; PC Qwen3.8-27B Q4_K_M 2.06 with seven code cases passed. One observation per machine with different tasks and placement. These are not simultaneous cluster throughput or a hardware ranking.">

| Machine / observed date | Exact model and placement | Reported generation rate | Output and timing | Quality and release |
| --- | --- | ---: | --- | --- |
| **Linux · 16 Oct** | Qwen3.5-4B Q6_K; RX 580 Vulkan, 33/33 layers offloaded | **36.84 tokens/s** | 107 output tokens; 2.877 s reported generation; 9.587 s model-unit duration including startup/settlement | **16/16 strict semantic cases passed** on one bounded coding sample. Model/controller/child units and socket absent afterward. |
| **Mac · 15 Oct** | Qwen3.8-27B MLX 4-bit | **16.39 tokens/s** | 141 output tokens; 9.389 s full request | **Semantic review rejected the answer:** its suggested test addressed the wrong case. Exact model unload confirmed by fresh inventory. |
| **PC · 16 Oct** | Qwen3.8-27B Q4_K_M; Vulkan, six layers on AMD, CPU KV | **2.06 tokens/s** | 31 output tokens; 14.570 s reported generation; 35.212 s HTTP request | **7/7 bounded clamp cases passed; 13/13 owned cleanup checks passed.** This was not dual-GPU inference. |

All three rates come directly from their retained engine/server fields. Each row contains one observation, with no repeats or variance estimate. In the two llama.cpp records, the rate matches `(output tokens − 1) / generation seconds`: Linux uses 106 / 2.877274 and PC uses 30 / 14.570342. The total output counts remain 107 and 31. We preserve the reported fields instead of recalculating a different metric. They use different models, tasks, device placement and timing implementations. Tokenizers also differ. The values show what happened in those samples; they do not rank the machines, establish general accuracy, or predict a combined rate. Linux's sample ran with its coordinator stopped, the PC was a direct native qualification, and the Mac was a supervised local advisory.

The older [historical performance study](performance.md) remains available with its original methods and dates. Its earlier peaks are separate from this current checkpoint. [Download the sanitized current measurements](data/three-machine-checkpoint-20261016/metrics.json).

## Throughput during parallel orchestration

| Measurement from one shared run | Result |
| --- | --- |
| PC output tokens/s during parallel work | **Not yet measured** |
| Linux output tokens/s during parallel work | **Not yet measured** |
| Mac output tokens/s during parallel work | **Not yet measured** |
| Combined output tokens/s over the common run window | **Not yet measured** |
| Validated tasks/minute and failure rate | **Not yet measured** |

A shared benchmark follows service activation and role admission. It will use one controller's monotonic clock for dispatch and completion, prove overlapping generation intervals, and retain exact serving-machine/model attribution. Each node's output-token counts and generation time will be recorded beside task pass/fail results and verified model release.

The cluster rate will be **total completed output tokens divided by the batch wall time**, reported separately from engine generation rates. Cold starts, queue time and retries must remain visible. No aggregate is published until that common run exists; unavailable values are `null` in the data.

## Next checkpoints

1. Install and activate the prepared Linux coordinator, then verify real production inference and cleanup after boot without a desktop login.
2. Complete Mac process admission and PC enrollment, qualify the remaining model roles and preserve existing work.
3. Run the bounded three-machine workload, measure overlap and useful output, and publish rates together with quality and memory-release results.

[Linux startup check](data/three-machine-checkpoint-20261016/linux-startup.json) · [Project roadmap](../roadmap.md) · [Released macOS Monitor](../README.md)
