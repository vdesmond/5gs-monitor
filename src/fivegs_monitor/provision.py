"""Provision the slice subscribers straight into the Open5GS MongoDB.

Replaces the chart's `populate` job (whose dbctl image is amd64-only) and lets each slice carry its
own QoS profile. Document layout follows open5gs-dbctl `add_ue_with_slice` for Open5GS 2.7.
"""

import os
import sys

from pymongo import MongoClient

KEY = "465B5CE8B199B49FAA5F0A2EE238A6BC"
OPC = "E8ED289DEBA952E4283B54E88E6183CA"

# unit: 0 bps, 1 kbps, 2 Mbps, 3 Gbps
SUBSCRIBERS = [
    # imsi, slice, sst, sd, dnn, 5qi, arp priority, ambr (value, unit)
    ("999700000000001", "embb", 1, "000001", "embb", 9, 8, (1, 3)),
    ("999700000000002", "urllc", 2, "000002", "urllc", 5, 1, (100, 2)),
]


def subscriber_doc(imsi, sst, sd, dnn, qi, arp_priority, ambr):
    ambr_doc = {
        "downlink": {"value": ambr[0], "unit": ambr[1]},
        "uplink": {"value": ambr[0], "unit": ambr[1]},
    }
    return {
        "imsi": imsi,
        "msisdn": [],
        "imeisv": [],
        "mme_host": [],
        "mme_realm": [],
        "purge_flag": [],
        "security": {"k": KEY, "amf": "8000", "op": None, "opc": OPC},
        "ambr": ambr_doc,
        "slice": [
            {
                "sst": sst,
                "sd": sd,
                "default_indicator": True,
                "session": [
                    {
                        "name": dnn,
                        "type": 3,  # IPv4
                        "pcc_rule": [],
                        "ambr": ambr_doc,
                        "qos": {
                            "index": qi,
                            "arp": {
                                "priority_level": arp_priority,
                                "pre_emption_capability": 1,
                                "pre_emption_vulnerability": 1,
                            },
                        },
                    }
                ],
            }
        ],
        "access_restriction_data": 32,
        "subscriber_status": 0,
        "network_access_mode": 0,
        "subscribed_rau_tau_timer": 12,
        "__v": 0,
    }


def add_args(p):
    p.add_argument("--db-uri", default=os.environ.get("DB_URI", "mongodb://open5gs-mongodb/open5gs"))
    p.set_defaults(func=run)


def run(args):
    db = MongoClient(args.db_uri, serverSelectionTimeoutMS=10000).get_default_database()
    for imsi, slice_name, sst, sd, dnn, qi, arp, ambr in SUBSCRIBERS:
        doc = subscriber_doc(imsi, sst, sd, dnn, qi, arp, ambr)
        db.subscribers.replace_one({"imsi": imsi}, doc, upsert=True)
        print(f"provisioned imsi={imsi} slice={slice_name} sst={sst} sd={sd} dnn={dnn} 5qi={qi}", file=sys.stderr)
