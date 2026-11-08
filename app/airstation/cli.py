"""Operator commands, run inside the api container by scripts/airctl."""
import json
import sys
import time

from . import db, director, liquidsoap


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: cli.py state|skip|live on|off|record off|live|all|queue|plan|telnet CMD")
        return 2
    cmd, rest = args[0], args[1:]
    if cmd == "state":
        print(json.dumps(liquidsoap.state(), indent=2))
    elif cmd == "skip":
        print(liquidsoap.command("air.skip"))
    elif cmd in ("live", "record"):
        if not rest:
            print(f"usage: {cmd} VALUE")
            return 2
        value = rest[0]
        conn = db.open_db()
        if cmd == "live":
            if value not in ("on", "off"):
                print("live on|off")
                return 2
            db.set_setting(conn, "live_enabled", value == "on")
        else:
            if value not in ("off", "live", "all"):
                print("record off|live|all")
                return 2
            db.set_setting(conn, "record_mode", value)
        conn.commit()
        print(liquidsoap.command(f"air.{cmd} {value}"))
    elif cmd == "queue":
        conn = db.open_db()
        for item in director.queue_items(conn):
            flag = "cached" if item["cached"] else "waiting"
            print(f"{item['queue_id']:>6} {item['reason']:<8} {flag:<8} "
                  f"{item['artist']} - {item['title']}")
    elif cmd == "plan":
        conn = db.open_db()
        print(f"Added {director.plan(conn, time.time())} tracks to the queue.")
    elif cmd == "telnet":
        print(liquidsoap.command(" ".join(rest)))
    else:
        print(f"Unknown command {cmd}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
