"""Report what this machine can run (``make doctor``).

Detects Docker, GPU, RAM, free disk, the ``claude`` CLI and its login, Ollama,
llama.cpp, sandbox options, and confirmed free-tier keys, then names the
usable model backends and the fallbacks the plan must take. The Claude backend
is marked unusable while the billing guard (brief section 8) finds a problem.

Stdlib only, so it runs before ``make setup``. It never calls a model and
never prints a secret value. ``--json PATH`` also writes the report as JSON.
``--apply`` lowers ``local_model_max_params_b`` in budget.toml if RAM is too
small for it (caps may only go down).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from baserate.guard import current_problems  # noqa: E402

FREE_TIER_KEYS = ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "GITHUB_MODELS_TOKEN")
# Rough resident memory for a 4-bit quantized model: about 0.6 GB per billion
# parameters plus 1.5 GB of runtime overhead. Keep 4 GB free for everything else.
GB_PER_B_PARAMS_Q4 = 0.6
RUNTIME_OVERHEAD_GB = 1.5
RESERVED_RAM_GB = 4.0


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a command and return (exit code, combined output); 127 if missing."""
    if shutil.which(cmd[0]) is None:
        return 127, ""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def ram_gb() -> dict[str, float | None]:
    """Total and available RAM in GB."""
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        fields = {}
        for line in meminfo.read_text().splitlines():
            key, _, rest = line.partition(":")
            fields[key] = float(rest.split()[0]) / 1024 / 1024
        return {"total": fields.get("MemTotal"), "available": fields.get("MemAvailable")}
    code, out = _run(["sysctl", "-n", "hw.memsize"])
    if code == 0 and out.isdigit():
        return {"total": int(out) / 1024**3, "available": None}
    return {"total": None, "available": None}


def max_params_for_ram(total_gb: float | None) -> float | None:
    """Largest 4-bit model (billions of parameters) that fits in RAM."""
    if total_gb is None:
        return None
    usable = total_gb - RESERVED_RAM_GB - RUNTIME_OVERHEAD_GB
    return max(round(usable / GB_PER_B_PARAMS_Q4, 1), 0.0)


def docker_status() -> dict[str, Any]:
    """Docker client presence and whether the daemon answers."""
    client = shutil.which("docker") is not None
    daemon = False
    if client:
        code, _ = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
        daemon = code == 0
    return {"client": client, "daemon": daemon}


def gpu_status() -> dict[str, Any]:
    """NVIDIA GPU via nvidia-smi, or Apple silicon via platform."""
    code, out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    if code == 0 and out:
        return {"kind": "nvidia", "detail": out.splitlines()}
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return {"kind": "apple-silicon", "detail": []}
    return {"kind": None, "detail": []}


def claude_status() -> dict[str, Any]:
    """The claude CLI, a best-effort login check, and the billing guard."""
    path = shutil.which("claude")
    version = None
    if path:
        code, out = _run(["claude", "--version"])
        version = out.splitlines()[0] if code == 0 and out else None
    creds = Path.home() / ".claude" / ".credentials.json"
    if creds.exists():
        login = "credentials file present"
    elif platform.system() == "Darwin":
        login = "unknown (macOS keychain; confirm with /status in Claude Code)"
    else:
        login = "not detected (no ~/.claude/.credentials.json)"
    problems = current_problems(ROOT)
    usable = path is not None and not problems
    return {
        "cli": path is not None,
        "version": version,
        "login": login,
        "billing_guard_problems": problems,
        "usable": usable,
    }


def ollama_status() -> dict[str, Any]:
    """Ollama binary and server."""
    binary = shutil.which("ollama") is not None
    server = False
    models: list[str] = []
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as resp:
            data = json.loads(resp.read())
            server = True
            models = [m.get("name", "") for m in data.get("models", [])]
    except (OSError, ValueError):
        pass
    return {"binary": binary, "server": server, "models": models}


def python_modules() -> dict[str, bool]:
    """Whether optional local-inference Python packages import in this interpreter."""
    found = {}
    for mod in ("torch", "transformers", "llama_cpp"):
        code = subprocess.run(
            [sys.executable, "-c", f"import {mod}"], capture_output=True, check=False
        ).returncode
        found[mod] = code == 0
    return found


def sandbox_status() -> dict[str, Any]:
    """Which isolation mechanisms work here, in order of preference."""
    docker = docker_status()
    options = {
        "docker_network_none": docker["daemon"],
        "bubblewrap": shutil.which("bwrap") is not None,
        "sandbox_exec": shutil.which("sandbox-exec") is not None,
        "unshare_netns": False,
    }
    if platform.system() == "Linux" and shutil.which("unshare"):
        code, _ = _run(["unshare", "-rn", "true"])
        options["unshare_netns"] = code == 0
    preferred = next((name for name, ok in options.items() if ok), None)
    return {"options": options, "preferred": preferred}


def network_status() -> dict[str, bool]:
    """Reachability of package and model hosts (HEAD requests only)."""
    hosts = {
        "pypi": "https://pypi.org/simple/",
        "huggingface": "https://huggingface.co/api/models?limit=1",
        "ollama": "https://ollama.com/",
        "arxiv": "https://arxiv.org/",
    }
    result = {}
    for name, url in hosts.items():
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=8):
                result[name] = True
        except OSError:
            result[name] = False
    return result


def free_tier_status() -> dict[str, Any]:
    """Which free-tier keys are present (names only) and whether they are confirmed."""
    present = [k for k in FREE_TIER_KEYS if os.environ.get(k)]
    confirmed = os.environ.get("FREE_TIER_KEYS_CONFIRMED") == "1"
    return {"present": present, "confirmed": confirmed, "usable": present if confirmed else []}


def budget_caps() -> dict[str, Any]:
    """Current caps from budget.toml, or an empty dict if it is missing."""
    path = ROOT / "budget.toml"
    if not path.exists():
        return {}
    return dict(tomllib.loads(path.read_text()))


def lower_params_cap(limit_b: float) -> bool:
    """Lower local_model_max_params_b in budget.toml to ``limit_b`` if it is higher."""
    path = ROOT / "budget.toml"
    text = path.read_text()
    match = re.search(r"^local_model_max_params_b\s*=\s*([0-9.]+)", text, flags=re.M)
    if not match or float(match.group(1)) <= limit_b:
        return False
    new = f"local_model_max_params_b = {limit_b:g}"
    path.write_text(text[: match.start()] + new + text[match.end() :])
    return True


def collect(check_network: bool) -> dict[str, Any]:
    """Gather the full doctor report."""
    disk = shutil.disk_usage(ROOT)
    ram = ram_gb()
    caps = budget_caps()
    report: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpus": os.cpu_count(),
        "ram_gb": ram,
        "free_disk_gb": round(disk.free / 1024**3, 1),
        "disk_floor_gb": caps.get("disk_floor_gb"),
        "pythons": {v: shutil.which(f"python{v}") is not None for v in ("3.11", "3.12")},
        "uv": _run(["uv", "--version"])[1] or None,
        "docker": docker_status(),
        "gpu": gpu_status(),
        "claude": claude_status(),
        "ollama": ollama_status(),
        "llama_cpp_binaries": [b for b in ("llama-cli", "llama-server") if shutil.which(b)],
        "python_modules": python_modules(),
        "sandbox": sandbox_status(),
        "free_tier": free_tier_status(),
        "network": network_status() if check_network else {},
        "allow_push": os.environ.get("ALLOW_PUSH") == "1",
        "cap_share": os.environ.get("CAP_SHARE", "1"),
        "local_model_max_params_b": caps.get("local_model_max_params_b"),
        "ram_fits_params_b": max_params_for_ram(ram["total"]),
    }
    report["backends"] = backends(report)
    return report


def backends(r: dict[str, Any]) -> dict[str, str]:
    """Summarize usable backends and the fallbacks that follow."""
    local_ready = (
        r["ollama"]["server"] or bool(r["llama_cpp_binaries"]) or any(r["python_modules"].values())
    )
    can_install = r.get("network", {}).get("ollama") or r.get("network", {}).get("huggingface")
    if local_ready:
        local = "usable"
    elif can_install:
        local = "not installed; installable (network reaches model hosts)"
    else:
        local = "unavailable"
    claude = r["claude"]
    if claude["usable"]:
        claude_msg = "usable (headless claude -p through the plan)"
    elif not claude["cli"]:
        claude_msg = "unusable: claude CLI not found"
    else:
        claude_msg = "unusable: billing guard: " + "; ".join(claude["billing_guard_problems"])
    ft = r["free_tier"]
    if ft["usable"]:
        free = "usable: " + ", ".join(ft["usable"])
    elif ft["present"]:
        free = "keys present but FREE_TIER_KEYS_CONFIRMED is not 1; ignored"
    else:
        free = "none configured"
    sb = r["sandbox"]["preferred"]
    if sb == "unshare_netns":
        sb = "unshare_netns (blocks network only; writes still need confinement)"
    return {
        "local_open_weight": local,
        "claude_plan": claude_msg,
        "free_tiers": free,
        "sandbox": sb or "none: run only this project's own generated tasks",
        "review": "headless opus" if claude["usable"] else "reviewer subagent fallback",
    }


def render(r: dict[str, Any]) -> list[str]:
    """Human-readable lines."""
    ram = r["ram_gb"]
    total = f"{ram['total']:.1f}" if ram["total"] is not None else "unknown"
    lines = [
        f"doctor {r['timestamp']}",
        f"platform: {r['platform']}, cpus {r['cpus']}, ram_total_gb {total}",
        f"free_disk_gb: {r['free_disk_gb']} (floor {r['disk_floor_gb']})",
        f"python 3.11/3.12: {r['pythons']}; uv: {r['uv']}",
        f"docker: client {r['docker']['client']}, daemon {r['docker']['daemon']}",
        f"gpu: {r['gpu']['kind'] or 'none'}",
        f"claude cli: {r['claude']['cli']} {r['claude']['version'] or ''}".rstrip(),
        f"claude login: {r['claude']['login']}",
        f"ollama: binary {r['ollama']['binary']}, server {r['ollama']['server']}",
        f"llama.cpp binaries: {r['llama_cpp_binaries'] or 'none'}",
        f"python inference modules: {r['python_modules']}",
        f"sandbox options: {r['sandbox']['options']}",
        f"network: {r['network'] or 'not checked'}",
        f"ALLOW_PUSH=1: {r['allow_push']}; CAP_SHARE: {r['cap_share']}",
        f"local_model_max_params_b: {r['local_model_max_params_b']} "
        f"(RAM fits about {r['ram_fits_params_b']}B at 4-bit)",
    ]
    lines.extend(f"backend {k}: {v}" for k, v in r["backends"].items())
    return lines


def main(argv: list[str] | None = None) -> int:
    """Print the report; optionally write JSON and lower the params cap."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="also write the report as JSON here")
    parser.add_argument("--no-network", action="store_true", help="skip reachability checks")
    parser.add_argument("--apply", action="store_true", help="lower the params cap to fit RAM")
    args = parser.parse_args(argv)
    report = collect(check_network=not args.no_network)
    for line in render(report):
        print(line)
    fits = report["ram_fits_params_b"]
    if args.apply and fits is not None and lower_params_cap(fits):
        print(f"lowered local_model_max_params_b to {fits:g} to fit RAM")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
