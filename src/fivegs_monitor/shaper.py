"""tc rate-limiter agent.

Runs as a privileged sidecar next to a network function and owns one HTB shaper on one device,
driven over a tiny HTTP API so the controller can change the rate without exec'ing into pods:

  GET  /rate            -> {"mbit": <float or null>}
  PUT  /rate {"mbit": x}   cap egress at x Mbit/s (x <= 0 removes the cap)

Two deployments use it:
  * gNB, dev eth0, match udp sport 4997: models the shared radio cell as a fixed-capacity FIFO link
    (UERANSIM has no radio resource model of its own).
  * eMBB UPF, dev ogstun: the controller's actuator - throttles the eMBB slice's downlink at the
    user plane so the URLLC slice keeps its latency budget.
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from prometheus_client import Gauge, start_http_server

rate_gauge = Gauge("shaper_rate_mbit", "Configured egress cap (0 = uncapped)", ["dev"])


def add_args(p):
    p.add_argument("--dev", default=os.environ.get("SHAPER_DEV", "eth0"))
    p.add_argument("--match-sport", type=int, default=int(os.environ.get("SHAPER_MATCH_SPORT", "0")),
                   help="only shape UDP with this source port (0 = all egress)")
    p.add_argument("--initial-mbit", type=float, default=float(os.environ.get("SHAPER_INITIAL_MBIT", "0")))
    p.add_argument("--queue-pkts", type=int, default=int(os.environ.get("SHAPER_QUEUE_PKTS", "300")),
                   help="FIFO depth; bounds the queueing delay (300 x 1.4 kB at 50 Mbit = 67 ms)")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--metrics-port", type=int, default=9101)
    p.set_defaults(func=run)


class Shaper:
    def __init__(self, dev, match_sport, queue_pkts):
        self.dev, self.match_sport, self.queue_pkts = dev, match_sport, queue_pkts
        self.mbit = 0.0
        self.lock = threading.Lock()

    def tc(self, *args, check=True):
        return subprocess.run(["tc", *args], check=check, capture_output=True, text=True)

    def clear(self):
        self.tc("qdisc", "del", "dev", self.dev, "root", check=False)
        self.mbit = 0.0
        rate_gauge.labels(self.dev).set(0)

    @staticmethod
    def burst(mbit):
        """HTB token bucket size. The cluster VM has no high-resolution timers, so with the default
        1.6 kB burst HTB can only release ~one packet per scheduler tick and a 50 Mbit class delivers
        ~15 Mbit. 10 ms worth of tokens keeps the rate accurate (at the cost of 10 ms bursts)."""
        return max(32_000, int(mbit * 1e6 / 8 * 0.010))

    def set_rate(self, mbit):
        with self.lock:
            if mbit <= 0:
                self.clear()
                return
            b = str(self.burst(mbit))
            if self.mbit > 0:
                # live update, no queue reset
                self.tc("class", "change", "dev", self.dev, "parent", "1:", "classid", "1:10",
                        "htb", "rate", f"{mbit}mbit", "ceil", f"{mbit}mbit", "burst", b, "cburst", b)
            else:
                self.tc("qdisc", "del", "dev", self.dev, "root", check=False)
                if self.match_sport:
                    # unmatched traffic goes to 1:20, uncapped
                    self.tc("qdisc", "add", "dev", self.dev, "root", "handle", "1:", "htb", "default", "20")
                    self.tc("class", "add", "dev", self.dev, "parent", "1:", "classid", "1:20",
                            "htb", "rate", "10gbit")
                else:
                    self.tc("qdisc", "add", "dev", self.dev, "root", "handle", "1:", "htb", "default", "10")
                self.tc("class", "add", "dev", self.dev, "parent", "1:", "classid", "1:10",
                        "htb", "rate", f"{mbit}mbit", "ceil", f"{mbit}mbit", "burst", b, "cburst", b)
                self.tc("qdisc", "add", "dev", self.dev, "parent", "1:10", "handle", "10:",
                        "pfifo", "limit", str(self.queue_pkts))
                if self.match_sport:
                    self.tc("filter", "add", "dev", self.dev, "parent", "1:", "protocol", "ip", "prio", "1",
                            "u32", "match", "ip", "protocol", "17", "0xff",
                            "match", "ip", "sport", str(self.match_sport), "0xffff", "flowid", "1:10")
            self.mbit = float(mbit)
            rate_gauge.labels(self.dev).set(self.mbit)


def make_handler(shaper):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/rate":
                self._send(200, {"dev": shaper.dev, "mbit": shaper.mbit or None})
            else:
                self._send(404, {"error": "not found"})

        def do_PUT(self):
            if self.path != "/rate":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                shaper.set_rate(float(body.get("mbit") or 0))
            except subprocess.CalledProcessError as e:
                return self._send(500, {"error": e.stderr})
            self._send(200, {"dev": shaper.dev, "mbit": shaper.mbit or None})

        def log_message(self, fmt, *args):
            print(f"shaper {self.address_string()} {fmt % args}", flush=True)

    return Handler


def run(args):
    start_http_server(args.metrics_port)
    shaper = Shaper(args.dev, args.match_sport, args.queue_pkts)
    shaper.clear()
    if args.initial_mbit > 0:
        shaper.set_rate(args.initial_mbit)
    print(f"shaper on {args.dev} sport={args.match_sport or 'any'} initial={args.initial_mbit} mbit", flush=True)
    HTTPServer(("0.0.0.0", args.port), make_handler(shaper)).serve_forever()
