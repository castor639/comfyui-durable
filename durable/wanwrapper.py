import functools
import hashlib
import logging
import sys

log = logging.getLogger("durable")

SAFE_EMBED_KEYS = {
    "target_shape", "num_frames", "control_embeds", "image_embeds", "clip_context", "negative_clip_context",
    "max_seq_len", "seq_len", "lat_h", "lat_w", "end_image", "fun_or_fl2v_model", "has_ref",
    "add_cond_latents", "mask", "force_offload", "tiled_vae", "vae",
}
UNSUPPORTED_ARGS = ("context_options", "freeinit_args", "multitalk_embeds", "loop_args", "flowedit_args",
                    "unianimate_poses", "fantasytalking_embeds", "uni3c_embeds")


def supported(kwargs):
    if any(kwargs.get(k) is not None for k in UNSUPPORTED_ARGS):
        return False
    if kwargs.get("denoise_strength", 1.0) < 1.0:
        return False
    samples = kwargs.get("samples")
    if samples is not None and (kwargs.get("add_noise_to_samples") or samples.get("noise_mask") is not None):
        return False
    embeds = kwargs.get("image_embeds") or {}
    return all(v is None or k in SAFE_EMBED_KEYS for k, v in embeds.items())


class Tap:
    def __init__(self, code, key, steps, journal, controller, ctx):
        self.code = code
        self.key = key
        self.steps = steps
        self.journal = journal
        self.controller = controller
        self.ctx = ctx

    def on_progress(self):
        f = sys._getframe(2)
        while f is not None and f.f_code is not self.code:
            f = f.f_back
        if f is None:
            return
        loc = f.f_locals
        latent, idx = loc.get("latent"), loc.get("idx")
        if latent is None or idx is None:
            return
        step = int(loc.get("start_step") or 0) + int(loc.get("ttm_start_step") or 0) + int(idx) + 1
        if step > self.steps - 2:
            return
        import comfy.model_management
        if self.controller.pause_requested:
            self.journal.save_now(self.key, step, latent, self.steps, 0)
            self.controller.event("paused", f"Paused at step {step}/{self.steps}", prompt_id=self.ctx.prompt_id, step=step)
            comfy.model_management.interrupt_current_processing(True)
        else:
            self.journal.maybe_save(self.key, step, latent, self.steps, 0)


def install(journal, controller):
    import nodes
    import comfy.utils
    from comfy_execution.utils import get_executing_context

    cls = nodes.NODE_CLASS_MAPPINGS.get("WanVideoSampler")
    if cls is None or getattr(cls, "_durable", False):
        return False
    original = getattr(cls, cls.FUNCTION)
    code = original.__code__
    state = {"tap": None}

    @functools.wraps(original)
    def process(self, *args, **kwargs):
        ctx = get_executing_context()
        node_hash = controller.node_hash(ctx.prompt_id, ctx.node_id) if ctx else None
        if node_hash is None or args or not supported(kwargs):
            return original(self, *args, **kwargs)

        call = controller.next_sampler_call(ctx.prompt_id, ctx.node_id, ctx.list_index)
        key = hashlib.sha256(f"{node_hash}|{ctx.list_index}|{call}|wanvideo".encode()).hexdigest()
        controller.track_snapshot(ctx.prompt_id, key)
        steps = int(kwargs["steps"])

        snap = journal.load(key)
        if snap is not None and snap["total"] == steps and 0 < snap["i"] < steps:
            end = kwargs.get("end_step", -1)
            if end == -1 or snap["i"] < end:
                kwargs = dict(kwargs, samples={"samples": snap["x"].unsqueeze(0)}, start_step=snap["i"], add_noise_to_samples=False)
                controller.event("resumed", f"Resumed WanVideo sampling at step {snap['i'] + 1}/{steps} instead of starting over",
                                 prompt_id=ctx.prompt_id, node=ctx.node_id, step=snap["i"])

        state["tap"] = Tap(code, key, steps, journal, controller, ctx)
        controller.sampling = True
        try:
            return original(self, *args, **kwargs)
        finally:
            state["tap"] = None
            controller.sampling = False

    update_absolute = comfy.utils.ProgressBar.update_absolute

    def update_hook(self, *args, **kwargs):
        tap = state["tap"]
        if tap is not None:
            try:
                tap.on_progress()
            except comfy.model_management.InterruptProcessingException:
                raise
            except Exception as e:
                log.warning(f"[durable] WanVideo snapshot skipped: {e}")
        return update_absolute(self, *args, **kwargs)

    import comfy.model_management
    comfy.utils.ProgressBar.update_absolute = update_hook
    setattr(cls, cls.FUNCTION, process)
    cls._durable = True
    log.info("[durable] WanVideoWrapper sampler support enabled")
    return True
