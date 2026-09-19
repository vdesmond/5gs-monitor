"""Single entry point; each subcommand is one container role."""

import argparse

from . import controller, probe, provision, shaper


def main() -> None:
    parser = argparse.ArgumentParser(prog="fivegs-monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    provision.add_args(sub.add_parser("provision", help="write slice subscribers to the Open5GS MongoDB"))
    probe.add_args(sub.add_parser("probe", help="UE sidecar: measure RTT/loss/throughput over uesimtun0"))
    shaper.add_args(sub.add_parser("shaper", help="tc rate-limiter agent with an HTTP API"))
    controller.add_args(sub.add_parser("controller", help="closed-loop SLA controller"))
    args = parser.parse_args()
    args.func(args)
