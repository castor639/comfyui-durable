import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

import psutil
import torch
from safetensors.torch import load_file

HERE = os.path.dirname(os.path.abspath(__file__))
COMFY = os.path.abspath(os.environ.get("COMFY_DIR", "ComfyUI"))
PY = sys.executable
PORT = 8188
URL = f"http://127.0.0.1:{PORT}"
STATE = os.path.join(COMFY, "user", "durable", str(PORT))
OUT = os.path.join(COMFY, "output")
RESULTS = []


def http(path, data=None):
    req = urllib.request.Request(URL + path, data=None if data is None else json.dumps(data).encode(), headers={"Content-Type": "application/json"}, method="GET" if data is None else "POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def comfy_pids():
    pids = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cmd = p.info["cmdline"] or []
        if any(c.endswith("main.py") for c in cmd) and str(PORT) in cmd:
            pids.append(p.info["pid"])
    return pids


def start(env=None, log="comfy.log"):
    e = dict(os.environ, DURABLE_SNAPSHOT_SECONDS="2", **(env or {}))
    f = open(os.path.join(HERE, log), "ab")
    subprocess.Popen([PY, "main.py", "--port", str(PORT), "--listen", "127.0.0.1"], cwd=COMFY, env=e, stdout=f, stderr=f, start_new_session=True,
                     preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    wait_ready()


def wait_ready(timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            http("/system_stats")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("ComfyUI did not come up")


def stop(sig=signal.SIGINT):
    for pid in comfy_pids():
        os.kill(pid, sig)
    t0 = time.time()
    while comfy_pids() and time.time() - t0 < 60:
        time.sleep(0.5)
    for pid in comfy_pids():
        os.kill(pid, signal.SIGKILL)
    time.sleep(1)


def reset_state():
    stop()
    shutil.rmtree(os.path.join(COMFY, "user", "durable"), ignore_errors=True)


def queue(prompt, prompt_id=None):
    prompt_id = prompt_id or str(uuid.uuid4())
    http("/prompt", {"prompt": prompt, "prompt_id": prompt_id, "client_id": "e2e"})
    return prompt_id


def wait_done(prompt_id, timeout=1800, until_success=False):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = http(f"/history/{prompt_id}")
            if prompt_id in h:
                st = h[prompt_id]["status"]
                if st.get("completed") or (st.get("status_str") == "error" and not until_success):
                    return st
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"job {prompt_id} did not finish")


def snapshot_step():
    d = os.path.join(STATE, "steps")
    best = -1
    if os.path.isdir(d):
        for name in os.listdir(d):
            if name.endswith(".pt"):
                try:
                    best = max(best, torch.load(os.path.join(d, name), weights_only=True)["i"])
                except Exception:
                    pass
    return best


def wait_snapshot(step, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = snapshot_step()
        if s >= step:
            return s
        time.sleep(0.3)
    raise RuntimeError("no snapshot")


def latent(prefix):
    d = os.path.join(OUT, "latents")
    files = sorted(f for f in os.listdir(d) if f.startswith(prefix))
    return load_file(os.path.join(d, files[-1]))["latent_tensor"].float()


def log_tail(name="comfy.log"):
    with open(os.path.join(HERE, name), errors="ignore") as f:
        return f.read()


def record(name, ok, **info):
    RESULTS.append({"test": name, "ok": ok, **info})
    print(json.dumps(RESULTS[-1]), flush=True)


def sd(prefix, steps=200, sampler="euler", seed=7, size=768, oom_node=False):
    p = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5-pruned-emaonly-fp16.safetensors"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a lighthouse on a cliff at dusk, oil painting", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": size, "height": size, "batch_size": 1}},
        "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": 7.0, "sampler_name": sampler, "scheduler": "normal", "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "10": {"class_type": "SaveLatent", "inputs": {"samples": ["3", 0], "filename_prefix": f"latents/{prefix}"}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }
    if oom_node:
        p["11"] = {"class_type": "DurableTestOOM", "inputs": {"samples": ["3", 0]}}
        p["8"]["inputs"]["samples"] = ["11", 0]
    return p


def wan(prefix, steps=30, seed=11, frames=33):
    return {
        "37": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.1_t2v_1.3B_fp16.safetensors", "weight_dtype": "default"}},
        "38": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
        "39": {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red fox running through fresh snow in a pine forest, cinematic", "clip": ["38", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "static, blurry, low quality", "clip": ["38", 0]}},
        "40": {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": 832, "height": 480, "length": frames, "batch_size": 1}},
        "48": {"class_type": "ModelSamplingSD3", "inputs": {"shift": 8.0, "model": ["37", 0]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": 6.0, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0, "model": ["48", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["40", 0]}},
        "10": {"class_type": "SaveLatent", "inputs": {"samples": ["3", 0], "filename_prefix": f"latents/{prefix}"}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["39", 0]}},
        "28": {"class_type": "SaveAnimatedWEBP", "inputs": {"images": ["8", 0], "filename_prefix": prefix, "fps": 16.0, "lossless": False, "quality": 90, "method": "default"}},
    }


def wanw(prefix, steps=30, seed=11, frames=33):
    return {
        "37": {"class_type": "WanVideoModelLoader", "inputs": {"model": "wan2.1_t2v_1.3B_fp16.safetensors", "base_precision": "bf16", "quantization": "disabled", "load_device": "offload_device", "attention_mode": "sdpa"}},
        "38": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red fox running through fresh snow in a pine forest, cinematic", "clip": ["38", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "static, blurry, low quality", "clip": ["38", 0]}},
        "50": {"class_type": "WanVideoTextEmbedBridge", "inputs": {"positive": ["6", 0], "negative": ["7", 0]}},
        "39": {"class_type": "WanVideoVAELoader", "inputs": {"model_name": "wan_2.1_vae.safetensors", "precision": "bf16"}},
        "40": {"class_type": "WanVideoEmptyEmbeds", "inputs": {"width": 832, "height": 480, "num_frames": frames}},
        "3": {"class_type": "WanVideoSampler", "inputs": {"model": ["37", 0], "image_embeds": ["40", 0], "text_embeds": ["50", 0], "steps": steps, "cfg": 6.0, "shift": 8.0, "seed": seed, "force_offload": True, "scheduler": "unipc", "riflex_freq_index": 0}},
        "10": {"class_type": "SaveLatent", "inputs": {"samples": ["3", 0], "filename_prefix": f"latents/{prefix}"}},
        "8": {"class_type": "WanVideoDecode", "inputs": {"vae": ["39", 0], "samples": ["3", 0], "enable_vae_tiling": False, "tile_x": 272, "tile_y": 272, "tile_stride_x": 144, "tile_stride_y": 128}},
        "28": {"class_type": "SaveAnimatedWEBP", "inputs": {"images": ["8", 0], "filename_prefix": prefix, "fps": 16.0, "lossless": False, "quality": 90, "method": "default"}},
    }


def baseline(prompt, prefix):
    reset_state()
    start({"DURABLE_DISABLE": "1"})
    t0 = time.time()
    st = wait_done(queue(prompt))
    took = time.time() - t0
    stop()
    return latent(prefix), took, st


def kill_mid_sampling(name, make, kill_at, sampler_label):
    ref, base_time, _ = baseline(make(f"{name}_ref"), f"{name}_ref")
    reset_state()
    start()
    t0 = time.time()
    pid = queue(make(f"{name}_run"))
    at = wait_snapshot(kill_at)
    for p in comfy_pids():
        os.kill(p, signal.SIGKILL)
    killed = time.time() - t0
    time.sleep(3)
    relaunched = bool(comfy_pids())
    wait_ready()
    st = wait_done(pid)
    total = time.time() - t0
    out = latent(f"{name}_run")
    diff = (out - ref).abs().max().item()
    rel = ((out - ref).norm() / ref.norm()).item()
    pattern = re.compile(r"Resumed (WanVideo )?sampling at step")
    resumed = bool(pattern.search(log_tail()) or pattern.search(open(os.path.join(STATE, "relaunch.log"), errors="ignore").read()))
    record(name, relaunched and st.get("completed", False) and resumed, sampler=sampler_label, killed_after_s=round(killed, 1), snapshot_step=at,
           baseline_s=round(base_time, 1), total_with_crash_s=round(total, 1), max_abs_diff=diff, rel_l2_diff=rel, watchdog_relaunched=relaunched, resumed_from_snapshot=resumed)
    stop()


def test_kill_in_decode():
    reset_state()
    start()
    offset = len(log_tail())
    pid = queue(wan("wan_decode"))
    t0 = time.time()
    while time.time() - t0 < 900 and "Requested to load WanVAE" not in log_tail()[offset:]:
        time.sleep(0.2)
    time.sleep(1)
    for p in comfy_pids():
        os.kill(p, signal.SIGKILL)
    killed = time.time() - t0
    time.sleep(3)
    wait_ready()
    t1 = time.time()
    st = wait_done(pid)
    after = time.time() - t1
    relog = open(os.path.join(STATE, "relaunch.log"), errors="ignore").read()
    hit = "restored KSampler (node 3) from disk cache" in relog
    record("kill_during_decode", st.get("completed", False) and hit, killed_after_s=round(killed, 1), finish_after_restart_s=round(after, 1), ksampler_from_disk_cache=hit)
    stop()


def test_pause_resume(make=sd, name="pause", at=60):
    ref, _, _ = baseline(make(f"{name}_ref"), f"{name}_ref")
    reset_state()
    start()
    pid = queue(make(f"{name}_run"))
    wait_snapshot(at)
    http("/durable/pause", {})
    t0 = time.time()
    while time.time() - t0 < 120:
        s = http("/durable/status")
        if s["paused"] >= 1:
            break
        time.sleep(0.5)
    paused = s["paused"] >= 1
    time.sleep(3)
    used = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip()
    http("/durable/resume", {})
    st = wait_done(pid, until_success=True)
    out = latent(f"{name}_run")
    diff = (out - ref).abs().max().item()
    record(f"{name}_resume", paused and st.get("completed", False), paused=paused, vram_used_mib_while_paused=used, max_abs_diff=diff)
    stop()


def test_clean_shutdown_keeps_queue():
    reset_state()
    start()
    a = queue(sd("clean_a", steps=400))
    b = queue(sd("clean_b", steps=30, seed=9))
    wait_snapshot(20)
    stop(signal.SIGINT)
    time.sleep(4)
    no_relaunch = not comfy_pids()
    start()
    s = http("/durable/status")
    kept = s["paused"]
    http("/durable/resume", {})
    sa, sb = wait_done(a, until_success=True), wait_done(b, until_success=True)
    record("clean_shutdown_keeps_queue", no_relaunch and kept == 2 and sa.get("completed") and sb.get("completed"), watchdog_stayed_quiet=no_relaunch, kept_as_paused=kept)
    stop()


def test_oom_retry():
    reset_state()
    node_dir = os.path.join(COMFY, "custom_nodes", "durable_test_oom")
    os.makedirs(node_dir, exist_ok=True)
    shutil.copy(os.path.join(HERE, "oom_node.py"), os.path.join(node_dir, "__init__.py"))
    flag = os.path.join(COMFY, "durable_oom_once")
    open(flag, "w").close()
    start()
    pid = queue(sd("oom", steps=30, oom_node=True))
    h = wait_done(pid, timeout=300, until_success=True)
    retried = "freed memory and retrying" in log_tail()
    record("oom_retry", h.get("completed", False) and retried, retried=retried)
    stop()
    shutil.rmtree(node_dir)


def test_overhead():
    times = {}
    for label, env in (("off", {"DURABLE_DISABLE": "1"}), ("on", {})):
        reset_state()
        start(env)
        wait_done(queue(sd(f"warm_{label}", steps=20, size=512)))
        runs = []
        for seed in range(3):
            t0 = time.time()
            wait_done(queue(sd(f"oh_{label}", steps=20, size=512, seed=100 + seed)))
            runs.append(time.time() - t0)
        times[label] = round(sum(runs) / len(runs), 2)
        stop()
    record("overhead_sd15_20_steps", True, avg_s_off=times["off"], avg_s_on=times["on"])


def test_wan_ab():
    times = {"off": [], "on": []}
    for label in ("off", "on", "off", "on"):
        reset_state()
        start({"DURABLE_DISABLE": "1"} if label == "off" else {})
        t0 = time.time()
        wait_done(queue(wan(f"ab_{label}", seed=21)), until_success=True)
        times[label].append(round(time.time() - t0, 1))
        stop()
    record("wan_job_time_on_vs_off", True, off_s=times["off"], on_s=times["on"])


if __name__ == "__main__":
    tests = sys.argv[1:] or ["sd_euler", "sd_dpmpp2m", "wan", "decode", "pause", "clean", "oom", "overhead"]
    for t in tests:
        try:
            if t == "sd_euler":
                kill_mid_sampling("sd_euler", lambda pre: sd(pre, sampler="euler"), 80, "euler")
            elif t == "sd_dpmpp2m":
                kill_mid_sampling("sd_dpmpp2m", lambda pre: sd(pre, sampler="dpmpp_2m"), 80, "dpmpp_2m")
            elif t == "sd_ancestral":
                kill_mid_sampling("sd_ancestral", lambda pre: sd(pre, sampler="euler_ancestral"), 80, "euler_ancestral")
            elif t == "sd_sde":
                kill_mid_sampling("sd_sde", lambda pre: sd(pre, sampler="dpmpp_2m_sde"), 80, "dpmpp_2m_sde")
            elif t == "wan":
                kill_mid_sampling("wan_t2v_1_3b", wan, 15, "uni_pc")
            elif t == "wanwrapper":
                kill_mid_sampling("wanwrapper_1_3b", wanw, 15, "unipc (WanVideoWrapper)")
            elif t == "pause_wanwrapper":
                test_pause_resume(wanw, "pause_wanwrapper", 10)
            elif t == "decode":
                test_kill_in_decode()
            elif t == "pause":
                test_pause_resume()
            elif t == "clean":
                test_clean_shutdown_keeps_queue()
            elif t == "oom":
                test_oom_retry()
            elif t == "wan_ab":
                test_wan_ab()
            elif t == "overhead":
                test_overhead()
        except Exception as e:
            record(t, False, error=repr(e))
            stop()
    with open(os.path.join(HERE, "results.json"), "w") as f:
        json.dump(RESULTS, f, indent=2)
