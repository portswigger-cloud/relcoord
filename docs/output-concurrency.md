# Generating a repository's outputs at once

How `max-output-concurrency` behaves is in the README ("Generating a
repository's outputs at once"). This page records what it was measured to cost
and gain, and how to turn it on for a deployment and check that it helped.

## Measured offline

Not a measurement of any cluster: one developer machine (18-core Apple
M-series, 48 GiB, macOS, CPython 3.15.2, helm 4.3.2, manifest-builder 0.9.2),
2026-11-25.

The input was the same for every run: system at `9f8ac69` diffed against
manifests at `77a47cc5`, as `DiffCommentProcessor.diff(system=True)` over the 17
system targets, with the chart cache warm and no validator, so that the
validator's own latency (PLAT-1195) is left out. Each process ran the diff twice,
cold then warm, twice per limit. Medians of those four:

| `max-output-concurrency` | generating 17 targets | whole diff | peak memory (median / max) |
|---|---|---|---|
| 1 (default) | 10.8 s | 13.9 s | 461 / 471 MiB |
| 2 | 9.0 s | 13.0 s | 796 / 809 MiB |
| 3 | 6.6 s | 10.7 s | 1059 / 1116 MiB |
| 4 | 5.7 s | 9.8 s | 1433 / 1637 MiB |

- The diff was byte-for-byte the same in every run, at every limit.
- Peak memory is the summed RSS of relcoord, its forkserver, the workers and
  their helm processes, sampled every 100 ms. Forked processes share pages that
  this counts once per process, so it overstates. Each further slot added about
  300–400 MiB here.
- Generating in a worker costs about 0.3 s per output more than generating in
  relcoord's own process: about 0.2 s to set up the scratch repository and
  0.1 s for object lookups through it. Per-output time also grew with the limit
  on this machine, more than that accounts for; the cause was not established,
  and is a reason to measure on the cluster rather than extrapolate from here.
- Gains flatten past 3–4: the rest of a diff (cloning, the diff itself) does not
  shrink, and the slowest targets bound the batch.

Live timings from before this setting existed, for reference only: generating
the 17 targets took 43 s on platform-prod and 97 s on fe-prod before its CPU
request was raised, and a whole system diff 105 s and 143 s.

## Enabling it on a deployment

For PortSwigger's relcoord on fe-prod, as a change to the system repository:

1. Bump relcoord's image in `fe-prod/section.toml` to a release that has this
   setting, and in the section of any other instance kept in step with it.
2. Set `max-output-concurrency = 3` in `fe-prod/relcoord/relcoord.toml`, and in
   `platform/relcoord/relcoord.toml` while that instance renders the same
   commits. 3 rather than 4: offline it took a diff's generation most of the way
   (−40%, against −47% at 4) at one fewer core and about 400 MiB less.
3. Raise the requests in `fe-prod/relcoord/extra/limitrange-relcoord.yaml`, today
   `cpu: "1"` and `memory: 1536Mi` (sized from a 1.41 GiB peak generating one
   output at a time), to at least `cpu: "3"` and `memory: 2560Mi`: a core per
   slot, and 512 MiB for each slot beyond the first, which is above the
   300–400 MiB measured offline. fe's Karpenter sizes nodes from requests and
   its default NodePool allows larger instances, so no node change is needed;
   with a 1 CPU request relcoord can land on a single-vCPU node, where the
   setting gains nothing. Both replicas reserve what is requested.
4. After the rollout, read the peak `container_memory_working_set_bytes` of
   relcoord's container over a few system diffs and rollouts, and adjust the
   memory request to that peak plus headroom.

## Checking that it helped

Compare like for like: the same kind of system commit, diffed or rolled out on
the same node type, before and after the change above.

- In Tempo, the `generate manifests <output>` spans under one `diff.process`
  (or `change.process`) trace should overlap, up to 3 at a time. Their spread,
  first start to last end, is the generation time to compare with the serial
  sum.
- `diff.process` is the end-to-end time of a diff. Report it alongside the
  generation spread, so that a change in the rest of the request is not taken
  for one in generation.
- The `validate manifests` spans are the validator's (PLAT-1195), and the fe-prod
  CPU request change happened before this; compare those separately rather than
  adding the improvements together.
