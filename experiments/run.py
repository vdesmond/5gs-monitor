import argparse
import csv
import json
import pathlib
import subprocess
import sys
import time

import requests

NS = "5gs"
PROM = "http://127.0.0.1:19090"
CONTROLLER_API = "http://127.0.0.1:18082"

SERIES = {
    "urllc_p95_ms": 'ue_rtt_ms{slice="urllc",quantile="0.95"}',
    "urllc_p50_ms": 'ue_rtt_ms{slice="urllc",quantile="0.5"}',
    "urllc_loss": 'ue_loss_ratio{slice="urllc"}',
    "embb_p95_ms": 'ue_rtt_ms{slice="embb",quantile="0.95"}',
    "embb_mbit": 'rate(ue_tun_bytes_total{slice="embb",direction="rx"}[5s]) * 8 / 1e6',
    "cap_mbit": "controller_cap_mbit",
    "breach": 'controller_breach{slice="urllc"}',
    "predicted": 'controller_predicted_breach{slice="urllc"}',
    "cell_mbit": 'shaper_rate_mbit{dev="eth0"}',
}



def port_forwards():
    procs = [
        subprocess.Popen(["kubectl", "port-forward", "-n", "monitoring", "svc/kps-prometheus", "19090:9090"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen(["kubectl", "port-forward", "-n", NS, "svc/controller", "18082:8082"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    for _ in range(30):
        try:
            requests.get(f"{PROM}/-/ready", timeout=1).raise_for_status()
            requests.get(CONTROLLER_API, timeout=1).raise_for_status()
            return procs
        except requests.RequestException:
            time.sleep(1)
    raise SystemExit("port-forwards did not come up")


def wait_ready():
    """Both PDU sessions up and the eMBB UE's server route pinned to its tunnel."""
    for _ in range(60):
        r = requests.get(f"{PROM}/api/v1/query", params={"query": "min(ue_tun_up)"}, timeout=5).json()
        tun = r["data"]["result"] and r["data"]["result"][0]["value"][1] == "1"
        route = subprocess.run(["kubectl", "exec", "-n", NS, "deploy/ue-embb", "-c", "loadgen", "--",
                                "ip", "route", "show", "dev", "uesimtun0"], capture_output=True, text=True, check=False).stdout
        if tun and route.strip():
            return
        time.sleep(5)
    raise SystemExit("UEs not ready (tunnel or route missing) - check `make status`")


def controller(enabled: bool):
    requests.put(f"{CONTROLLER_API}/enabled", json={"enabled": enabled}, timeout=5).raise_for_status()
    print(f"  controller {'on' if enabled else 'off'}", flush=True)


def load(seconds: int):
    """Start a TCP downlink on the eMBB UE (server -> UE) and return the process."""
    print(f"  eMBB load on for {seconds}s", flush=True)
    return subprocess.Popen(
        ["kubectl", "exec", "-n", NS, "deploy/ue-embb", "-c", "loadgen", "--",
         "timeout", str(seconds + 10), "iperf3", "-c", "app-server.5gs.svc.cluster.local", "-R",
         "-t", str(seconds), "-f", "m"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def kill_load():
    subprocess.run(["kubectl", "exec", "-n", NS, "deploy/ue-embb", "-c", "loadgen", "--", "pkill", "iperf3"],
                   capture_output=True, check=False)


def fetch(start, end, step="2s"):
    rows = {}
    for name, expr in SERIES.items():
        r = requests.get(f"{PROM}/api/v1/query_range",
                         params={"query": expr, "start": start, "end": end, "step": step}, timeout=30).json()
        for res in r["data"]["result"]:
            for t, v in res["values"]:
                rows.setdefault(float(t), {})[name] = float(v)
    return [{"t": t, **vals} for t, vals in sorted(rows.items())]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--short", action="store_true", help="quarter-length phases for a smoke test")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    f = 0.25 if args.short else 1.0
    phases = [("baseline", 30, False, False), ("uncontrolled", 90, True, False),
              ("controlled", 150, True, True), ("cooldown", 60, False, True)]

    procs = port_forwards()
    try:
        wait_ready()
        kill_load()
        controller(False)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = pathlib.Path(args.out) / stamp
        out.mkdir(parents=True)
        t0 = time.time()
        marks = []
        for name, secs, with_load, with_ctl in phases:
            secs = max(10, int(secs * f))
            print(f"[{time.time() - t0:6.1f}s] phase {name} ({secs}s)", flush=True)
            marks.append({"phase": name, "start": time.time(), "seconds": secs})
            controller(with_ctl)
            proc = load(secs) if with_load else None
            time.sleep(secs)
            if proc:
                proc.wait(timeout=30)
                summary = [l for l in proc.stdout.read().splitlines() if "receiver" in l]
                print("  " + (summary[-1].strip() if summary else "iperf: no summary"), flush=True)
        end = time.time()
        time.sleep(5)  # let the last scrapes land

        rows = fetch(t0, end + 5)
        with open(out / "kpis.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["t", *SERIES])
            w.writeheader()
            w.writerows(rows)
        with open(out / "phases.json", "w") as fh:
            json.dump(marks, fh, indent=1)
        print(f"wrote {out}/kpis.csv ({len(rows)} rows) and phases.json")

        # per-phase summary
        for m in marks:
            sel = [r for r in rows if m["start"] <= r["t"] < m["start"] + m["seconds"]]
            p95 = sorted(r["urllc_p95_ms"] for r in sel if "urllc_p95_ms" in r)
            thr = [r["embb_mbit"] for r in sel if "embb_mbit" in r]
            if p95:
                over = sum(1 for v in p95 if v > 15) / len(p95)
                print(f"  {m['phase']:<13} URLLC p95: median {p95[len(p95)//2]:5.1f} ms, worst {p95[-1]:5.1f} ms, "
                      f"over SLA {over:4.0%} of samples | eMBB {sum(thr)/max(1,len(thr)):5.1f} Mbit/s")
    finally:
        kill_load()
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    sys.exit(main())
