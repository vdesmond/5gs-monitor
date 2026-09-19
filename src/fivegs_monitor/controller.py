"""Closed-loop SLA controller.

Every `period` seconds it reads the per-slice KPIs from Prometheus, compares them with the SLA
policy and, when the protected (URLLC) slice is breaching - or is predicted to breach within the
look-ahead horizon - throttles the best-effort (eMBB) slice at its UPF through the shaper agent.
When the protected slice is healthy again the cap is released additively (AIMD), so the eMBB slice
gets its capacity back as soon as the URLLC slice can spare it.

The policy is a YAML file (deploy/controller/policy.yaml):

  period_s: 2
  protected:
    slice: urllc
    rtt_p95_ms: 15         # SLA: p95 RTT
    loss_ratio: 0.01       # SLA: reliability
    predict_horizon_s: 6   # act early if the RTT trend crosses the SLA within this horizon
  best_effort:
    slice: embb
    shaper_url: http://upf-embb-shaper.5gs.svc:8081
    start_mbit: 40         # first cap applied on a breach (a bit under the cell rate)
    decrease_factor: 0.7   # multiplicative decrease while breaching
    increase_mbit: 1       # additive increase per healthy period
    floor_mbit: 2
    release_after_s: 30    # cap clearly not binding (throughput < half of it) this long -> drop it

The controller exposes its own state on :9102 (PodMonitor) so decisions end up in Grafana next to
the KPIs, and `PUT /enabled {"enabled": bool}` on :8082 so experiments can A/B it.
"""

import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import yaml
from prometheus_client import Gauge, start_http_server

g_enabled = Gauge("controller_enabled", "1 when the controller may actuate")
g_breach = Gauge("controller_breach", "1 while the protected slice violates its SLA", ["slice"])
g_predicted = Gauge("controller_predicted_breach", "1 when a breach is forecast within the horizon", ["slice"])
g_cap = Gauge("controller_cap_mbit", "Cap applied to the best-effort slice (0 = none)", ["slice"])
g_kpi = Gauge("controller_kpi", "KPI value as seen by the controller", ["slice", "kpi"])
g_slope = Gauge("controller_rtt_slope_ms_per_s", "Fitted RTT trend", ["slice"])


def add_args(p):
    p.add_argument("--policy", default=os.environ.get("POLICY", "/etc/fivegs-monitor/policy.yaml"))
    p.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL", "http://kps-prometheus.monitoring.svc:9090"))
    p.add_argument("--metrics-port", type=int, default=9102)
    p.add_argument("--api-port", type=int, default=8082)
    p.add_argument("--enabled", default=os.environ.get("CONTROLLER_ENABLED", "true"))
    p.set_defaults(func=run)


class Prom:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def scalar(self, query, default=float("nan")):
        try:
            r = requests.get(f"{self.url}/api/v1/query", params={"query": query}, timeout=5).json()
            result = r["data"]["result"]
            return float(result[0]["value"][1]) if result else default
        except (requests.RequestException, KeyError, ValueError, IndexError):
            return default

    def series(self, query, seconds, step="2s"):
        """(t, v) samples of a range query ending now."""
        now = time.time()
        try:
            r = requests.get(f"{self.url}/api/v1/query_range",
                             params={"query": query, "start": now - seconds, "end": now, "step": step},
                             timeout=5).json()
            result = r["data"]["result"]
            return [(float(t), float(v)) for t, v in result[0]["values"]] if result else []
        except (requests.RequestException, KeyError, ValueError, IndexError):
            return []


def slope(samples):
    """Least-squares slope (units/s) of (t, v) samples; 0 with fewer than 3 points."""
    pts = [(t, v) for t, v in samples if not math.isnan(v)]
    if len(pts) < 3:
        return 0.0
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mv = sum(v for _, v in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    return sum((t - mt) * (v - mv) for t, v in pts) / den if den else 0.0


class Controller:
    def __init__(self, policy, prom, enabled):
        self.p = policy
        self.prom = prom
        self.enabled = enabled
        self.cap = 0.0            # current cap on the best-effort slice, 0 = none
        self.idle_since = None    # since when the cap has been slack (throughput well below it)
        self.lock = threading.Lock()
        g_enabled.set(int(enabled))

    # --- actuator -------------------------------------------------------------------------
    def apply_cap(self, mbit):
        be = self.p["best_effort"]
        mbit = 0.0 if mbit <= 0 else max(be["floor_mbit"], mbit)
        try:
            requests.put(f"{be['shaper_url']}/rate", json={"mbit": mbit}, timeout=5).raise_for_status()
        except requests.RequestException as e:
            print(f"actuator error: {e}", flush=True)
            return
        if mbit != self.cap:
            print(f"cap {be['slice']} -> {mbit or 'none'} mbit", flush=True)
        self.cap = mbit
        g_cap.labels(be["slice"]).set(mbit)

    # --- one control period ---------------------------------------------------------------
    def step(self):
        pr, be = self.p["protected"], self.p["best_effort"]
        s = pr["slice"]
        rtt = self.prom.scalar(f'ue_rtt_ms{{slice="{s}",quantile="0.95"}}')
        loss = self.prom.scalar(f'ue_loss_ratio{{slice="{s}"}}', 0.0)
        hist = self.prom.series(f'ue_rtt_ms{{slice="{s}",quantile="0.95"}}', pr.get("trend_window_s", 10))
        k = slope(hist)
        be_mbit = self.prom.scalar(
            f'rate(ue_tun_bytes_total{{slice="{be["slice"]}",direction="rx"}}[10s]) * 8 / 1e6', 0.0)
        g_kpi.labels(s, "rtt_p95_ms").set(rtt)
        g_kpi.labels(s, "loss_ratio").set(loss)
        g_kpi.labels(be["slice"], "throughput_mbit").set(be_mbit)
        g_slope.labels(s).set(k)

        have_rtt = not math.isnan(rtt)
        breach = (have_rtt and rtt > pr["rtt_p95_ms"]) or loss > pr["loss_ratio"]
        predicted = (not breach) and have_rtt and k > 0 and rtt + k * pr.get("predict_horizon_s", 0) > pr["rtt_p95_ms"]
        g_breach.labels(s).set(int(breach))
        g_predicted.labels(s).set(int(predicted))

        with self.lock:
            if not self.enabled:
                return
            now = time.time()
            if breach:
                self.idle_since = None
                self.apply_cap(be["start_mbit"] if self.cap == 0 else self.cap * be["decrease_factor"])
            elif predicted:
                self.idle_since = None      # hold the cap: don't grow into a forecast breach
            elif self.cap > 0:
                if be_mbit < 0.5 * self.cap:
                    # cap is not what limits the slice; drop it once that has been true for a while
                    self.idle_since = self.idle_since or now
                    if now - self.idle_since >= be["release_after_s"]:
                        self.apply_cap(0)
                        return
                else:
                    self.idle_since = None
                self.apply_cap(self.cap + be["increase_mbit"])

    def set_enabled(self, enabled):
        with self.lock:
            self.enabled = enabled
            g_enabled.set(int(enabled))
            if not enabled:
                self.apply_cap(0)
                self.idle_since = None
        print(f"controller enabled={enabled}", flush=True)


def make_handler(ctl):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, {"enabled": ctl.enabled, "cap_mbit": ctl.cap})

        def do_PUT(self):
            if self.path != "/enabled":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            ctl.set_enabled(bool(body.get("enabled", True)))
            self._send(200, {"enabled": ctl.enabled, "cap_mbit": ctl.cap})

        def log_message(self, fmt, *args):
            print(f"api {self.address_string()} {fmt % args}", flush=True)

    return Handler


def run(args):
    with open(args.policy) as f:
        policy = yaml.safe_load(f)
    start_http_server(args.metrics_port)
    ctl = Controller(policy, Prom(args.prometheus), str(args.enabled).lower() in ("1", "true", "yes"))
    threading.Thread(target=HTTPServer(("0.0.0.0", args.api_port), make_handler(ctl)).serve_forever,
                     daemon=True).start()
    ctl.apply_cap(0)
    period = policy.get("period_s", 2)
    print(f"controller up: policy={args.policy} prometheus={args.prometheus} period={period}s", flush=True)
    while True:
        t0 = time.time()
        try:
            ctl.step()
        except Exception as e:  # noqa: BLE001 - keep the loop alive on transient errors
            print(f"step error: {e}", flush=True)
        time.sleep(max(0.0, period - (time.time() - t0)))
