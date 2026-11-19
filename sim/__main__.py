"""`python -m sim <command>`: drive the chaos channels by hand.

  run [--wall SECONDS]        run the whole scenario against a running tower and carrier API
  erp-file HOUR               drop one ERP export (scenario hour, 5 to 10) into the inbox
  clock [--wall S | --clear]  write or clear the shared scenario clock file
  faults CARRIER [options]    set or clear a fault on the carrier API (C1, C2, C3 or *)
  contracts                   draw the 12 MSA contract PDFs into data/contracts/ (then: python -m tower.contracts)
  bols [--count N]            drop N bills of lading (5 bad on purpose) into data/inbox/bol/ for shipments in the database
"""

from __future__ import annotations

import argparse

import httpx

from sim.erp_writer import ErpSim
from sim.scenario import Scenario, at, make_clock
from sim.world import World
from tower.clock import clear_clock, write_clock
from tower.config import load_settings


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m sim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--wall", type=float, default=75.0)
    e = sub.add_parser("erp-file")
    e.add_argument("hour", type=int)
    c = sub.add_parser("clock")
    c.add_argument("--wall", type=float, default=75.0)
    c.add_argument("--clear", action="store_true")
    sub.add_parser("contracts")
    b = sub.add_parser("bols")
    b.add_argument("--count", type=int, default=60)
    f = sub.add_parser("faults")
    f.add_argument("carrier")
    f.add_argument("--error-rate", type=float, default=0.0)
    f.add_argument("--latency-ms", type=int, default=0)
    f.add_argument("--outage", action="store_true")
    f.add_argument("--timeout", action="store_true")
    f.add_argument("--clear", action="store_true", help="remove every fault")
    args = ap.parse_args()
    settings = load_settings()

    if args.cmd == "run":
        sc = Scenario(settings, World(settings.network_dir, settings.scenario_seed), make_clock(args.wall))
        sc.run()
        print(f"done: {sc.exports_done} ERP files, GPS {dict(sc.gps.stats)}")
    elif args.cmd == "erp-file":
        path, stats = ErpSim(World(settings.network_dir, settings.scenario_seed), settings.scenario_seed).write_export(
            at(args.hour), settings.inbox)
        print(path, dict(stats))
    elif args.cmd == "contracts":
        from sim.pdf_sim import write_contracts
        print(f"{len(write_contracts(settings))} contract PDFs in {settings.contracts_dir}")
    elif args.cmd == "bols":
        from sim.pdf_sim import write_bols
        manifest = write_bols(settings, count=args.count)
        print(f"{len(manifest)} bills of lading in {settings.bol_inbox}; deliberately bad: "
              + ", ".join(f"{m['file']} ({m['note']})" for m in manifest if m["mode"] not in ("clean", "minor_drift")))
    elif args.cmd == "clock":
        if args.clear:
            clear_clock(settings.clock_file)
        else:
            write_clock(settings.clock_file, make_clock(args.wall))
        print("ok")
    else:
        client = httpx.Client(base_url=settings.carrier_api_url, headers={"X-Admin-Key": settings.carrier_admin_key})
        if args.clear:
            client.delete("/admin/faults").raise_for_status()
        else:
            body = {"carrier": args.carrier, "error_rate": args.error_rate, "latency_ms": args.latency_ms,
                    "outage": args.outage, "timeout": args.timeout}
            print(client.put("/admin/faults", json=body).raise_for_status().json())


if __name__ == "__main__":
    main()
