import argparse
import json
import statistics
import time
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description="Exercise 20 shared live observation subscriptions")
    parser.add_argument("--url", default="http://127.0.0.1:5173")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    with httpx.Client(base_url=args.url + "/api/v1/", timeout=10) as client:
        def call(method, path, payload=None):
            response = client.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()

        selected = [row["instrument_id"] for row in call("GET", "markets?market_type=PERP&limit=50")["items"]
                    if row["status"] == "ACTIVE"][:20]
        if len(selected) != 20:
            raise RuntimeError("need 20 active native markets")
        pool = call("POST", "watchlists", {"name": f"Load test {uuid4().hex[:8]}", "type": "MANUAL"})["id"]
        try:
            for market in selected:
                call("POST", f"watchlists/{pool}/members/{market}", {})
            deadline = time.monotonic() + 45
            while True:
                status = call("GET", "system/status")
                state = status["worker"]["state"]
                if not status["worker_stale"] and state.get("ws_connected") and state.get("subscriptions", 0) >= 60:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("worker did not subscribe to the 20 asset streams")
                time.sleep(2)

            max_ws_delay = 0
            reconnects = state.get("ws_reconnects", 0)
            max_subscriptions = state["subscriptions"]
            end = time.monotonic() + args.seconds
            samples = []
            while time.monotonic() < end:
                start = time.monotonic()
                status = call("GET", "system/status")
                samples.append((time.monotonic() - start) * 1000)
                state = status["worker"]["state"]
                if status["worker_stale"] or not state.get("ws_connected") or state.get("subscriptions", 0) < 60:
                    raise RuntimeError("live worker degraded during load sample")
                max_ws_delay = max(max_ws_delay, state.get("ws_latency_ms", 0))
                max_subscriptions = max(max_subscriptions, state["subscriptions"])
                time.sleep(1)
            if state.get("ws_data_at") is None:
                raise RuntimeError("no observed WS monotonic latency sample")
            print(json.dumps(dict(assets=len(selected), samples=len(samples), duration_seconds=args.seconds,
                                  subscriptions=max_subscriptions, references=state["subscription_references"],
                                  ws_delay_max_ms=max_ws_delay,
                                  reconnects=state.get("ws_reconnects", 0) - reconnects,
                                  status_median_ms=round(statistics.median(samples), 2),
                                  status_max_ms=round(max(samples), 2))))
        finally:
            call("DELETE", f"watchlists/{pool}")


if __name__ == "__main__":
    main()

