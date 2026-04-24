#!/usr/bin/env python3
"""
ArcWatch Local GPU Monitor

Collects NVIDIA GPU metrics via nvidia-smi and reports them to ArcWatch.

Usage:
    python scripts/gpu_monitor.py --api-key YOUR_KEY
    python scripts/gpu_monitor.py --api-key YOUR_KEY --cluster my-workstation --interval 30
    python scripts/gpu_monitor.py --api-key YOUR_KEY --once
"""
import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error


def get_gpu_metrics():
    """Query nvidia-smi for GPU metrics. Returns list of metric dicts."""
    query_fields = [
        "uuid", "index", "name", "utilization.gpu", "memory.used",
        "memory.total", "temperature.gpu", "power.draw", "clocks.sm",
        "clocks.mem",
    ]
    cmd = [
        "nvidia-smi",
        f"--query-gpu={','.join(query_fields)}",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            print(f"nvidia-smi failed: {result.stderr.strip()}", file=sys.stderr)
            return []
    except FileNotFoundError:
        print("nvidia-smi not found. Is the NVIDIA driver installed?", file=sys.stderr)
        return []
    except subprocess.TimeoutExpired:
        print("nvidia-smi timed out", file=sys.stderr)
        return []

    metrics = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 10:
            continue

        def safe_float(val):
            try:
                return float(val) if val and val.strip() not in ("[N/A]", "N/A", "") else None
            except (ValueError, TypeError):
                return None

        def safe_int(val):
            try:
                return int(float(val)) if val and val.strip() not in ("[N/A]", "N/A", "") else None
            except (ValueError, TypeError):
                return None

        metrics.append({
            "gpu_uuid": parts[0],
            "gpu_index": safe_int(parts[1]) or 0,
            "gpu_model": parts[2],
            "utilization": safe_float(parts[3]),
            "memory_used_mb": safe_int(parts[4]),
            "memory_total_mb": safe_int(parts[5]),
            "temperature": safe_int(parts[6]),
            "power_watts": safe_float(parts[7]),
            "sm_clock_mhz": safe_int(parts[8]),
            "mem_clock_mhz": safe_int(parts[9]),
        })
    return metrics


def report_metrics(api_url, api_key, cluster_name, metrics, node_name):
    """POST metrics to ArcWatch API."""
    gpu_type = metrics[0]["gpu_model"] if metrics else "Unknown"
    payload = {
        "cluster_name": cluster_name,
        "node_name": node_name,
        "gpu_type": gpu_type,
        "metrics": metrics,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            return True, body
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else str(e)
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(description="ArcWatch Local GPU Monitor")
    parser.add_argument("--api-url", default="http://localhost:8000/api/v1/ingest/gpu/")
    parser.add_argument("--api-key", required=True, help="API key with 'ingest' scope")
    parser.add_argument("--cluster", default="local", help="Cluster name (default: local)")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between reports (default: 15)")
    parser.add_argument("--once", action="store_true", help="Report once and exit")
    args = parser.parse_args()

    node_name = socket.gethostname()
    print(f"ArcWatch GPU Monitor -- node={node_name}, cluster={args.cluster}, interval={args.interval}s")
    print(f"Reporting to: {args.api_url}")

    while True:
        metrics = get_gpu_metrics()
        if metrics:
            ok, result = report_metrics(args.api_url, args.api_key, args.cluster, metrics, node_name)
            if ok:
                gpu_info = ", ".join(f"GPU{m['gpu_index']}: {m.get('utilization', '?')}%" for m in metrics)
                print(f"[OK] Reported {len(metrics)} GPU(s): {gpu_info}")
            else:
                print(f"[ERR] {result}", file=sys.stderr)
        else:
            print("[WARN] No GPU metrics collected", file=sys.stderr)

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
