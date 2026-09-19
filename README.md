# 5gs-monitor

This project implements a closed-loop controller that monitors a URLLC network slice and throttles a competing eMBB slice at the user plane to guarantee latency SLAs in a shared cell. It's a simple demonstration of the capabilities of

In this setup, two network slices (eMBB and URLLC) run on an [Open5GS](https://open5gs.org) core with a UERANSIM RAN, each with their own SMF and UPF pair. The setup measures KPIs like latency and throughput end-to-end through the PDU sessions and scrapes them into Prometheus. 

When an eMBB download starts inflating the shared cell's latency, the controller throttles the eMBB slice at its UPF just enough to bring URLLC back inside its latency budget, handing the capacity back (AIMD - ?) as soon as it can.

![experiment](results/latest/sla_loop.png)

In the figure we can see that, uncontrolled, a 46 Mbit/s eMBB download pushes the URLLC slice's p95 RTT from 1.2 ms to 24 ms (against a 15 ms SLA). With the controller on, URLLC's median p95 drops back down to 1.8 ms and it stays inside the SLA 89 % of the time. The two dips we see in the plot are AIMD probing past the cell rate. Even with the throttling, eMBB still gets 31 Mbit/s on average.

## Architecture

```text
                 ┌──────────── Kubernetes (OrbStack, single node, arm64) ────────────┐
                 │  namespace 5gs                                                    │
 UE eMBB ──┐     │   gnb ─────► AMF ─► NRF/SCP/NSSF/AUSF/UDM/UDR/PCF/BSF ─► MongoDB  │
 (+probe)  ├─ RLS┤   (UERANSIM) │                                                    │
 UE URLLC ─┘     │              ├─► smf (embb)  ─► upf (embb)  ─[shaper]─┐           │
 (+probe)        │              └─► smf-urllc   ─► upf-urllc  ───────────┼─► app-server (iperf3)
                 │                                                        │           │
                 │   controller ◄─── Prometheus ◄─── probes, NF exporters, shapers    │
                 │                                                                    │
                 │  namespace monitoring: kube-prometheus-stack (Prometheus, Grafana) │
                 └────────────────────────────────────────────────────────────────────┘
```

Some implementation details:

- The core runs on Gradiant Open5GS Helm charts (2.7.2 images on arm64)
- The two slices are eMBB (`sst=1 sd=000001 dnn=embb`) and URLLC (`sst=2 sd=000002 dnn=urllc`). The AMF and NSSF advertise both, and each SMF carries an `info` block so the AMF picks the right one.
- Subscribers are provisioned straight into MongoDB with their per-slice QoS rules.
- A single UERANSIM gNB serves both slices, with one UE Deployment per slice.
- To measure KPIs, sidecars in each UE pod pin the app server behind `uesimtun0`, ping it for RTT and loss, and read the tunnel byte counters for throughput.
- Because UERANSIM doesn't actually model radio resources, the shared cell is a 50 Mbit/s HTB and a 300-packet FIFO on the gNB's downlink. This is what both slices fight for!
- The controller reads KPIs from Prometheus every 2 s and compares them against `policy.yaml`. If there's a breach, it multiplicatively decreases the eMBB cap on the UPF's `ogstun`. If healthy, it additively increases it. It also uses a linear RTT trend over the last 10 s to hold the cap when a breach is forecast within 6 s.


The loop policy is configured in `policy.yaml`:

```yaml
protected:   {slice: urllc, rtt_p95_ms: 15, loss_ratio: 0.01, trend_window_s: 10, predict_horizon_s: 6}
best_effort: {slice: embb, start_mbit: 40, decrease_factor: 0.7, increase_mbit: 1, floor_mbit: 2, release_after_s: 30}
```

We get a classic AIMD sawtooth that we saw in the plot: eMBB ramps up until it hurts URLLC, gets cut back, and ramps again. The URLLC excursions generally last one or two control periods each. We can trade eMBB throughput for fewer of these excursions by setting a smaller `increase_mbit` or a longer `predict_horizon_s`.

Note that this is orchestration at the core/telco-cloud layer! A real gNB would give URLLC a scheduling priority that this FIFO does not, which is why a closed loop is needed here in the first place. The controller only acts on the eMBB slice.

## Usage & Reproduce

Everything runs in Kubernetes, so you just need Docker, a cluster (I use OrbStack on macOS: `orb config set k8s.enable true`), `helm`, `kubectl`, and `uv`.

```bash
git clone --recurse-submodules https://github.com/vdesmond/5gs-monitor && cd 5gs-monitor
make up
make status
make grafana
make experiment 
make plot
```

`make down` removes the `5gs` namespace