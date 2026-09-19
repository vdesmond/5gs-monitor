"""Plot a run written by experiments/run.py:  uv run --extra analysis experiments/plot.py results/<stamp>"""

import json
import pathlib
import sys

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd

matplotlib.use("Agg")
plt.style.use(pathlib.Path(__file__).parent / "style.mplstyle")

SLA_MS = 15
# Dark theme backgrounds for phases
COLORS = {"baseline": ("#ffffff", 0.05), "uncontrolled": ("#e95378", 0.15), "controlled": ("#27D797", 0.15), "cooldown": ("#ffffff", 0.05)}


def main(run_dir):
    run = pathlib.Path(run_dir)
    df = pd.read_csv(run / "kpis.csv")
    with open(run / "phases.json") as fh:
        phases = json.load(fh)
    t0 = phases[0]["start"]
    df["s"] = df["t"] - t0
    df = df[df["s"] >= 0]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True, gridspec_kw={"hspace": 0.12})
    for ax in (ax1, ax2):
        for ph in phases:
            a = ph["start"] - t0
            c, a_val = COLORS[ph["phase"]]
            ax.axvspan(a, a + ph["seconds"], color=c, alpha=a_val, lw=0)
    for ph in phases:
        ax1.text(ph["start"] - t0 + ph["seconds"] / 2, ax1.get_ylim()[1], ph["phase"],
                 ha="center", va="bottom", fontsize=9, color="#ffffff")

    ax1.plot(df["s"], df["urllc_p95_ms"], color="#e95378", lw=1.6, label="URLLC RTT p95")
    ax1.plot(df["s"], df["urllc_p50_ms"], color="#FAB795", lw=1.0, alpha=0.8, label="URLLC RTT p50")
    ax1.axhline(SLA_MS, color="#e95378", ls="--", lw=1, label=f"SLA ({SLA_MS} ms p95)")
    ax1.set_ylabel("latency (ms)")
    ax1.set_ylim(bottom=0)
    ax1.legend(loc="upper left", fontsize=9, ncol=3)

    ax2.plot(df["s"], df["embb_mbit"], color="#25B0BC", lw=1.4, label="eMBB downlink throughput")
    ax2.step(df["s"], df["cap_mbit"].where(df["cap_mbit"] > 0), color="#27D797", lw=1.6, where="post",
             label="controller cap on eMBB (UPF)")
    if "cell_mbit" in df:
        ax2.plot(df["s"], df["cell_mbit"], color="#ffffff", ls=":", lw=1, alpha=0.5, label="shared cell capacity")
    ax2.set_ylabel("Mbit/s")
    ax2.set_xlabel("time (s)")
    ax2.set_ylim(bottom=0)
    ax2.legend(loc="upper left", fontsize=9, ncol=2)

    fig.suptitle("SLA-aware slice control on a virtual 5G core: URLLC latency vs eMBB load", fontsize=11)
    fig.savefig(run / "sla_loop.png", dpi=150, bbox_inches="tight")
    print(f"wrote {run / 'sla_loop.png'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else max(p for p in pathlib.Path("results").iterdir() if not p.is_symlink()))
