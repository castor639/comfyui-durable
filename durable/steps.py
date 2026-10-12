import hashlib
import inspect
import logging
import math
import os
import queue
import threading
import time

import torch

from .store import replace

log = logging.getLogger("durable")


def digest_tensor(h, t):
    t = t.detach().to("cpu").contiguous()
    h.update(f"{tuple(t.shape)}{t.dtype}".encode())
    h.update(t.view(torch.uint8).numpy().tobytes())


class StepJournal:
    def __init__(self, root, interval):
        self.dir = os.path.join(root, "steps")
        os.makedirs(self.dir, exist_ok=True)
        self.interval = interval
        self.last_save = {}
        self.pending = queue.Queue()
        threading.Thread(target=self._writer, daemon=True, name="durable-steps").start()

    def path(self, key):
        return os.path.join(self.dir, f"{key}.pt")

    def load(self, key):
        try:
            return torch.load(self.path(key), map_location="cpu", weights_only=True)
        except FileNotFoundError:
            return None
        except Exception as e:
            log.warning(f"[durable] ignoring unreadable step snapshot {key}: {e}")
            return None

    def maybe_save(self, key, step, x, total, draws):
        now = time.monotonic()
        if now - self.last_save.get(key, 0.0) < self.interval:
            return
        self.last_save[key] = now
        self.pending.put((key, {"i": step, "total": total, "draws": draws, "x": x.detach().to("cpu", copy=True)}))

    def save_now(self, key, step, x, total, draws):
        self.pending.join()
        self._write(key, {"i": step, "total": total, "draws": draws, "x": x.detach().to("cpu", copy=True)})

    def discard(self, key):
        self.last_save.pop(key, None)
        try:
            os.remove(self.path(key))
        except FileNotFoundError:
            pass

    def prune(self, max_age_days=3):
        cutoff = time.time() - max_age_days * 86400
        for name in os.listdir(self.dir):
            p = os.path.join(self.dir, name)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)

    def _write(self, key, data):
        tmp = self.path(key) + ".tmp"
        torch.save(data, tmp)
        replace(tmp, self.path(key))

    def _writer(self):
        while True:
            key, data = self.pending.get()
            try:
                self._write(key, data)
            except Exception as e:
                log.warning(f"[durable] step snapshot write failed: {e}")
            finally:
                self.pending.task_done()


_noise_kinds = {}


def noise_kind(fn):
    if fn not in _noise_kinds:
        kind = None
        try:
            src = inspect.getsource(fn)
            if "noise_sampler" in inspect.signature(fn).parameters:
                if "BrownianTreeNoiseSampler(" in src:
                    kind = "brownian_cpu" if "cpu=True" in src else "brownian"
                elif "default_noise_sampler(" in src:
                    kind = "default"
        except (TypeError, OSError, ValueError):
            pass
        _noise_kinds[fn] = kind
    return _noise_kinds[fn]


def replay_noise(kind, x, sigmas, seed, skip):
    from comfy.k_diffusion.sampling import BrownianTreeNoiseSampler, default_noise_sampler
    counter = [skip]
    if kind == "default":
        base = default_noise_sampler(x, seed=seed)
        for _ in range(skip):
            base(None, None)
    else:
        positive = sigmas[sigmas > 0]
        base = BrownianTreeNoiseSampler(x, positive.min(), sigmas.max(), seed=seed, cpu=kind == "brownian_cpu")

    def sampler(sigma, sigma_next):
        counter[0] += 1
        return base(sigma, sigma_next)

    return sampler, counter


UNIPC = ("sample_unipc", "sample_unipc_bh2")


def close(a, b):
    a, b = float(a), float(b)
    return abs(a - b) <= 1e-5 * max(1.0, abs(b))


def resumable_state(d, i, sigmas, total, unipc):
    x = d.get("x")
    if x is None or i >= total - 2:
        return None
    if unipc:
        return x * math.sqrt(1.0 + float(sigmas[i]) ** 2)
    if "sigma" not in d or not close(d["sigma"], sigmas[i]):
        return None
    if "sigma_hat" in d and not close(d["sigma_hat"], d["sigma"]):
        return None
    return x


def install(journal, controller):
    import comfy.model_management
    import comfy.samplers
    from comfy_execution.utils import get_executing_context

    original = comfy.samplers.KSAMPLER.sample

    def sample(self, model_wrap, sigmas, extra_args, callback, noise, latent_image=None, denoise_mask=None, disable_pbar=False):
        ctx = get_executing_context()
        node_hash = controller.node_hash(ctx.prompt_id, ctx.node_id) if ctx else None
        if node_hash is None:
            return original(self, model_wrap, sigmas, extra_args, callback, noise, latent_image, denoise_mask, disable_pbar)

        call = controller.next_sampler_call(ctx.prompt_id, ctx.node_id, ctx.list_index)
        h = hashlib.sha256(f"{node_hash}|{ctx.list_index}|{call}|{getattr(self.sampler_function, '__name__', '')}".encode())
        digest_tensor(h, sigmas)
        digest_tensor(h, noise)
        if latent_image is not None:
            digest_tensor(h, latent_image)
        key = h.hexdigest()
        controller.track_snapshot(ctx.prompt_id, key)

        snap = journal.load(key)
        inner = self.sampler_function
        total = len(sigmas) - 1

        def resumable(model_k, x, sigmas, extra_args=None, callback=None, disable=None, **kwargs):
            full_sigmas = sigmas
            start = 0
            if snap is not None and snap["total"] == total and tuple(snap["x"].shape) == tuple(x.shape) and 0 < snap["i"] < total:
                start = snap["i"]
                x = snap["x"].to(device=x.device, dtype=x.dtype)
                controller.event("resumed", f"Resumed sampling at step {start + 1}/{total} instead of starting over", prompt_id=ctx.prompt_id, node=ctx.node_id, step=start)

            counter = [0]
            kind = noise_kind(inner)
            if kind and kwargs.get("noise_sampler") is None:
                skip = int(snap.get("draws", start)) if start else 0
                kwargs["noise_sampler"], counter = replay_noise(kind, x, sigmas, (extra_args or {}).get("seed"), skip)

            unipc = getattr(inner, "__name__", "") in UNIPC

            def step_callback(d):
                if start:
                    d = {**d, "i": d["i"] + start}
                state = resumable_state(d, d["i"], full_sigmas, total, unipc)
                if controller.pause_requested:
                    if state is not None:
                        journal.save_now(key, d["i"], state, total, counter[0])
                        controller.event("paused", f"Paused at step {d['i'] + 1}/{total}", prompt_id=ctx.prompt_id, step=d["i"])
                    else:
                        controller.event("paused", "Paused, this sampler will restart from the beginning", prompt_id=ctx.prompt_id)
                    comfy.model_management.interrupt_current_processing(True)
                elif state is not None:
                    journal.maybe_save(key, d["i"], state, total, counter[0])
                if callback is not None:
                    callback(d)

            return inner(model_k, x, sigmas[start:], extra_args=extra_args, callback=step_callback, disable=disable, **kwargs)

        self.sampler_function = resumable
        controller.sampling = True
        try:
            return original(self, model_wrap, sigmas, extra_args, callback, noise, latent_image, denoise_mask, disable_pbar)
        finally:
            self.sampler_function = inner
            controller.sampling = False

    comfy.samplers.KSAMPLER.sample = sample
