import os

import torch

FLAG = os.path.join(os.getcwd(), "durable_oom_once")


class DurableTestOOM:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"samples": ("LATENT",)}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "run"
    CATEGORY = "testing"

    def run(self, samples):
        if os.path.exists(FLAG):
            os.remove(FLAG)
            raise torch.OutOfMemoryError("CUDA out of memory. Tried to allocate 20.00 GiB (simulated by durable e2e test)")
        return (samples,)


NODE_CLASS_MAPPINGS = {"DurableTestOOM": DurableTestOOM}
