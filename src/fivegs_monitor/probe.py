"""UE-side KPI probe.

Runs as a sidecar in the UE pod, sharing its network namespace with nr-ue. Once the PDU session's
uesimtun0 is up it pins the application server behind the tunnel with a host route and then measures,
end to end through gNB -> UPF -> server:

  * latency:    ICMP RTT (p50/p95/max over a sliding window, plus a histogram)
  * reliability: ping loss ratio
  * throughput: uesimtun0 rx/tx byte counters (rate() in PromQL)

Everything is exported on :9100 for Prometheus (deploy/ran/podmonitor.yaml).
"""

import collections
import os
import re
import socket
import subprocess
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

RTT_RE = re.compile(r"time=([\d.]+) ms")
LOSS_RE = re.compile(r"(\d+)% packet loss")

rtt_hist = Histogram(
    "ue_rtt_seconds", "End-to-end ICMP RTT through the PDU session", ["slice"],
    buckets=(0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0),
)
rtt_quantile = Gauge("ue_rtt_ms", "RTT quantile over the sliding window", ["slice", "quantile"])
loss_ratio = Gauge("ue_loss_ratio", "Ping loss ratio over the sliding window", ["slice"])
tun_bytes = Counter("ue_tun_bytes", "Bytes on uesimtun0", ["slice", "direction"])
tun_up = Gauge("ue_tun_up", "1 when uesimtun0 exists", ["slice"])
probe_rounds = Counter("ue_probe_rounds", "Ping rounds completed", ["slice"])


def add_args(p):
    p.add_argument("--slice", default=os.environ.get("SLICE", "default"))
    p.add_argument("--target", default=os.environ.get("TARGET_HOST", "app-server.5gs.svc.cluster.local"))
    p.add_argument("--dev", default="uesimtun0")
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--count", type=int, default=10, help="pings per round")
    p.add_argument("--interval", type=float, default=0.1, help="seconds between pings")
    p.add_argument("--window", type=int, default=50, help="samples in the sliding window")
    p.set_defaults(func=run)


def wait_for_dev(dev):
    while not os.path.exists(f"/sys/class/net/{dev}"):
        time.sleep(1)


def resolve(host):
    while True:
        try:
            return socket.gethostbyname(host)
        except socket.gaierror:
            time.sleep(2)


def pin_route(ip, dev, old_ip=None):
    # everything to the app server goes through the PDU session, not the pod's eth0
    if old_ip and old_ip != ip:
        subprocess.run(["ip", "route", "del", f"{old_ip}/32", "dev", dev], check=False, capture_output=True)
    subprocess.run(["ip", "route", "replace", f"{ip}/32", "dev", dev], check=False)


def read_counter(dev, name):
    with open(f"/sys/class/net/{dev}/statistics/{name}") as f:
        return int(f.read())


def quantile(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    i = min(len(sorted_vals) - 1, round(q * (len(sorted_vals) - 1)))
    return sorted_vals[i]


def run(args):
    start_http_server(args.port)
    s = args.slice
    tun_up.labels(s).set(0)
    wait_for_dev(args.dev)
    tun_up.labels(s).set(1)
    target = resolve(args.target)
    pin_route(target, args.dev)

    window = collections.deque(maxlen=args.window)   # rtt_ms, or None for a lost ping
    last = {"rx": read_counter(args.dev, "rx_bytes"), "tx": read_counter(args.dev, "tx_bytes")}

    while True:
        try:
            # nr-ue recreates the device (and drops the route) on re-registration, and the server
            # pod may have been rescheduled to a new IP: re-resolve and re-pin every round
            new_target = resolve(args.target)
            pin_route(new_target, args.dev, old_ip=target)
            target = new_target
            round_once(args, s, target, window, last)
        except OSError:
            # uesimtun0 vanished mid-round (UE re-registering); wait for it and start over
            tun_up.labels(s).set(0)
            wait_for_dev(args.dev)
            tun_up.labels(s).set(1)
            last = {"rx": read_counter(args.dev, "rx_bytes"), "tx": read_counter(args.dev, "tx_bytes")}


def round_once(args, s, target, window, last):
    out = subprocess.run(
        ["ping", "-I", args.dev, "-c", str(args.count), "-i", str(args.interval), "-W", "1", "-n", target],
        capture_output=True, text=True, check=False,
    ).stdout
    rtts = [float(x) for x in RTT_RE.findall(out)]
    for r in rtts:
        rtt_hist.labels(s).observe(r / 1000)
        window.append(r)
    for _ in range(args.count - len(rtts)):
        window.append(None)

    good = sorted(r for r in window if r is not None)
    for q in (0.5, 0.95, 0.99):
        rtt_quantile.labels(s, str(q)).set(quantile(good, q))
    rtt_quantile.labels(s, "max").set(good[-1] if good else float("nan"))
    loss_ratio.labels(s).set(1 - len(good) / len(window) if window else 0)

    for direction, name in (("rx", "rx_bytes"), ("tx", "tx_bytes")):
        now = read_counter(args.dev, name)
        if now >= last[direction]:
            tun_bytes.labels(s, direction).inc(now - last[direction])
        last[direction] = now
    probe_rounds.labels(s).inc()
