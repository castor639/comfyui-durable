# ComfyUI Durable

Your overnight queue survives crashes.

You queue ten video jobs and go to bed. Somewhere around job four, ComfyUI runs out of system RAM and the process dies. Normally you wake up to a dead terminal, an empty queue, and an hour of sampling thrown away.

With Durable installed, ComfyUI comes back on its own, your queue is put back exactly as it was, and the job that was running picks up from the last sampling step it reached instead of starting over.

There is nothing to wire up. No new nodes, no changes to your workflows, no new launch script.

## Install

Clone into `custom_nodes` and restart ComfyUI:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/castor639/comfyui-durable.git
```

You will see `[durable] protecting jobs` in the log, and a small pill in the bottom left of the UI.

## What it does

**Saves sampling progress.** While a sampler runs, Durable writes the current latent to disk every few seconds. That is a few MB even for video, so it costs almost nothing. If the job dies at step 34 of 50, the next attempt starts at step 34.

**Keeps finished work.** Outputs of normal nodes (latents, images, masks, text conditioning) are cached on disk, keyed by ComfyUI's own cache key plus the size and modified time of every model file involved. If ComfyUI crashes during VAE decode, the rerun skips sampling entirely and only decodes.

**Restarts ComfyUI.** A tiny watchdog process watches ComfyUI. If it dies with jobs still in the queue, the watchdog starts it again with the same command line. If you stop ComfyUI yourself (Ctrl+C, closing it normally), the watchdog stays quiet.

**Puts your queue back.** Every queued job is journaled to disk. After a crash the queue is restored in order. After a normal shutdown the jobs are kept as paused, and the Resume button runs them.

**Retries out of memory errors.** When a node fails with a GPU out of memory error, Durable frees memory and runs the job again, up to two times. Because of the two points above, the retry starts where the failure happened.

**Pause and resume.** The Pause button saves the running job at its current step, holds the rest of the queue, and frees VRAM. Resume continues from that step, even after a reboot.

## Settings

All optional, set as environment variables before starting ComfyUI.

| Variable | Default | Meaning |
|---|---|---|
| `DURABLE_SNAPSHOT_SECONDS` | `10` | Minimum seconds between sampler snapshots |
| `DURABLE_CACHE_GB` | `20` | Disk cache size limit, oldest entries are dropped first |
| `DURABLE_CACHE_ENTRY_GB` | `4` | Outputs bigger than this are not cached |
| `DURABLE_OOM_RETRIES` | `2` | Automatic retries after an out of memory error |
| `DURABLE_WATCHDOG` | `1` | Set to `0` to turn off automatic restarts |
| `DURABLE_DISABLE` | unset | Set to `1` to turn everything off |

State lives in `ComfyUI/user/durable/<port>/`. Deleting that folder resets everything.

## How well resuming works

Measured on an A10G by hard killing ComfyUI (`kill -9`) in the middle of sampling and comparing the finished output against an uninterrupted run with the same seed:

| Sampler | Model | Difference after resume |
|---|---|---|
| `euler` | SD 1.5 | none, bit identical |
| `euler_ancestral` | SD 1.5 | none, bit identical |
| `dpmpp_2m` | SD 1.5 | 0.08% |
| `dpmpp_2m_sde` | SD 1.5 | 0.14% |
| `uni_pc` | Wan 2.1 1.3B video | 1.7%, not visible in the frames |
| `unipc` in WanVideoWrapper | Wan 2.1 1.3B video | 5.3%, 31.8 dB PSNR over all frames, not visible |

Ancestral and SDE samplers are exact because Durable replays the noise generator to the same position. Multistep samplers (`dpmpp_2m`, `uni_pc`, `deis`, ...) keep a short history of previous steps that is not saved, so the first step after a resume runs at a lower order and the result drifts slightly.

Other checks from the same run: a crash during VAE decode restarted and finished in 7 seconds because the sampled latent came from the disk cache; Pause dropped VRAM to 373 MiB and Resume gave a bit identical image; a simulated out of memory error was retried and the job completed; a 20 step SD 1.5 job took 2.01 s with Durable and 2.01 s without, and a full Wan 2.1 1.3B job (33 frames, 30 steps) took 153 s with Durable against 152 s without.

## Things to know

- When the watchdog restarts ComfyUI it runs in the background, so on Linux and macOS its output goes to `ComfyUI/user/durable/<port>/relaunch.log`. Stop it with `kill <pid>`; a normal kill counts as an intentional stop and does not trigger another restart.
- Durable hooks ComfyUI's built in sampler path (KSampler, KSamplerAdvanced, SamplerCustom) and the `WanVideoSampler` node from ComfyUI-WanVideoWrapper. For the wrapper it resumes through the node's own `samples` and `start_step` inputs, the same path Wan 2.2 high and low noise workflows use. Context windows, FreeInit, multitalk, partial denoise and the more exotic embed types are left alone and simply restart. Other custom nodes with their own sampling loop are not snapshotted, though their outputs are still cached and their jobs still restored.
- A sampler is only snapshotted at steps where it reports the state Durable expects. Samplers that do not (for example `dpm_adaptive`) still run normally and simply restart from the beginning after a crash.
- If you run ComfyUI under systemd, Docker or another supervisor, Durable leaves restarting to that supervisor and only restores the queue.
- API keys for partner nodes are never written to disk, so restored jobs that use them need the key again.
- With `--cache-none`, ComfyUI does not compute node cache keys, so snapshots and the disk cache are off.

## License

GPL-3.0, same as ComfyUI.
