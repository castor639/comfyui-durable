import logging

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

try:
    from .durable.setup import install
    install()
except Exception:
    logging.getLogger("durable").exception("[durable] failed to start, ComfyUI will run without it")
