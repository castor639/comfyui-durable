import atexit
import json
import logging
import os
import signal
import subprocess
import sys

from aiohttp import web

from .store import read_json, write_json

log = logging.getLogger("durable")


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def state_root():
    import folder_paths
    from comfy.cli_args import args
    root = os.path.join(folder_paths.get_user_directory(), "durable", str(args.port))
    os.makedirs(root, exist_ok=True)
    return root


def mark_clean_exit(root):
    def write(*_):
        try:
            write_json(os.path.join(root, "clean_exit.json"), {"pid": os.getpid()})
        except Exception:
            pass

    atexit.register(write)
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None or signal.getsignal(sig) is not signal.SIG_DFL:
            continue

        def handler(signum, frame, _write=write):
            _write()
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        try:
            signal.signal(sig, handler)
        except ValueError:
            pass


def supervised_elsewhere():
    return os.getpid() == 1 or "INVOCATION_ID" in os.environ


def start_watchdog(root):
    if os.environ.get("DURABLE_WATCHDOG", "1") == "0" or supervised_elsewhere():
        return False
    info_path = os.path.join(root, "watchdog.json")
    info = read_json(info_path, {})
    try:
        import psutil
        if info.get("parent") == os.getpid() and psutil.pid_exists(info.get("pid", -1)):
            return True
    except Exception:
        pass
    script = os.path.join(os.path.dirname(__file__), "watchdog.py")
    argv = getattr(sys, "orig_argv", [sys.executable] + sys.argv)[1:]
    cmd = [sys.executable, "-I", script, "--parent", str(os.getpid()), "--root", root, "--cwd", os.getcwd(), "--argv", json.dumps(argv)]
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True)
    write_json(info_path, {"pid": proc.pid, "parent": os.getpid()})
    return True


def add_routes(server, controller, cache, info):
    routes = server.routes

    @routes.get("/durable/status")
    async def status(request):
        return web.json_response({**controller.status(cache), **info})

    @routes.post("/durable/pause")
    async def pause(request):
        controller.pause()
        return web.json_response(controller.status(cache))

    @routes.post("/durable/resume")
    async def resume(request):
        controller.resume()
        return web.json_response(controller.status(cache))


def install():
    if os.environ.get("DURABLE_DISABLE") == "1":
        log.info("[durable] disabled by DURABLE_DISABLE=1")
        return

    import execution
    import server as comfy_server
    from comfy_execution.cache_provider import register_cache_provider

    from . import steps
    from .cache import DiskCache
    from .controller import Controller, install_queue_hooks
    from .store import JobStore

    root = state_root()
    clean_path = os.path.join(root, "clean_exit.json")
    clean_exit = os.path.exists(clean_path)
    if clean_exit:
        os.remove(clean_path)

    srv = comfy_server.PromptServer.instance
    store = JobStore(root)
    controller = Controller(srv, store, oom_retries=int(env_float("DURABLE_OOM_RETRIES", 2)))
    journal = steps.StepJournal(root, interval=env_float("DURABLE_SNAPSHOT_SECONDS", 10))
    journal.prune()
    controller.journal = journal
    cache = DiskCache(root, controller,
                      max_bytes=int(env_float("DURABLE_CACHE_GB", 20) * 1024 ** 3),
                      max_entry_bytes=int(env_float("DURABLE_CACHE_ENTRY_GB", 4) * 1024 ** 3))

    install_queue_hooks(execution.PromptQueue, controller)
    steps.install(journal, controller)
    register_cache_provider(cache)
    mark_clean_exit(root)
    watchdog = start_watchdog(root)
    add_routes(srv, controller, cache, {"watchdog": watchdog, "root": root})
    controller.recover(clean_exit, max_attempts=3)
    log.info(f"[durable] protecting jobs, state in {root}, watchdog {'on' if watchdog else 'off'}")
