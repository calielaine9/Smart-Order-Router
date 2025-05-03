#!/usr/bin/env python3
import sys
import os
import json
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from collections import defaultdict

ORDER_SIZE = 5000
STEP = 100  # chunk size for allocator search

def load_snapshots(path):
    """
    Read CSV, keep first message per (ts_event, publisher_id),
    then group into time‐ordered Level-1 snapshots.
    """
    df = pd.read_csv(path)
    df = df.sort_values(["ts_event", "sequence"])
    df = df.drop_duplicates(subset=["ts_event", "publisher_id"], keep="first")
    snapshots = []
    for ts, group in df.groupby("ts_event", sort=True):
        venues = []
        for _, r in group.iterrows():
            venues.append({
                "ask": float(r.ask_px_00),
                "ask_size": float(r.ask_sz_00),
                "fee": 0.0003,    # flat fee per share
                "rebate": 0.002   # flat rebate per share
            })
        snapshots.append({"ts": ts, "venues": venues})
    return snapshots

def compute_cost(split, venues, order_size, lam_over, lam_under, theta):
    """
    Compute total expected cost for a single static split:
      - market cost = executed * (ask + fee)
      - rebate for unfilled limit shares
      - penalties for under- or over-fill
      - linear queue‐risk penalty
    """
    executed = 0
    cash = 0.0
    for share_qty, v in zip(split, venues):
        exe = min(share_qty, v["ask_size"])
        executed += exe
        cash += exe * (v["ask"] + v["fee"])
        cash -= max(share_qty - exe, 0) * v["rebate"]
    under = max(order_size - executed, 0)
    over  = max(executed - order_size, 0)
    cash += lam_under * under + lam_over * over
    cash += theta * (under + over)
    return cash

def allocate(order_size, venues, lam_over, lam_under, theta):
    """
    Brute-force search in STEPs of size STEP per venue exactly
    as per allocator_pseudocode.txt.
    """
    splits = [[]]
    N = len(venues)
    for v in range(N):
        new_splits = []
        for alloc in splits:
            used = sum(alloc)
            max_v = int(min(order_size - used, venues[v]["ask_size"]))
            for q in range(0, max_v + 1, STEP):
                new_splits.append(alloc + [q])
        splits = new_splits

    best_split = None
    best_cost = float("inf")
    for alloc in splits:
        if sum(alloc) != order_size:
            continue
        cost = compute_cost(alloc, venues, order_size, lam_over, lam_under, theta)
        if cost < best_cost:
            best_cost, best_split = cost, alloc

    # fallback in case no exact split was found
    if best_split is None:
        best_split = [0] * N
        idx = min(range(N), key=lambda i: venues[i]["ask"])
        best_split[idx] = order_size
    return best_split, best_cost

def backtest_static(snapshots, lam_over, lam_under, theta):
    """
    Run static SOR: at each snapshot, recompute full‐order split
    for the *remaining* shares, execute up to ask_size, roll forward
    any unfilled quantity.
    Returns total cash and average fill price.
    """
    remaining = ORDER_SIZE
    cash = 0.0

    for snap in snapshots:
        if remaining <= 0:
            break
        split, _ = allocate(remaining, snap["venues"], lam_over, lam_under, theta)
        for qty, v in zip(split, snap["venues"]):
            exe = min(qty, remaining, v["ask_size"])
            cash += exe * (v["ask"] + v["fee"])
            cash -= max(qty - exe, 0) * v["rebate"]
            remaining -= exe

    # apply under/over‐fill penalties at the end
    under = max(remaining, 0)
    over  = max(-remaining, 0)
    cash += lam_under * under + lam_over * over

    avg_price = cash / ORDER_SIZE
    return cash, avg_price

def baseline_bestask(snapshots):
    rem, cash = ORDER_SIZE, 0.0
    for snap in snapshots:
        if rem <= 0:
            break
        best = min(snap["venues"], key=lambda v: v["ask"])
        take = min(rem, best["ask_size"])
        cash += take * best["ask"]
        rem -= take
    filled = ORDER_SIZE - rem
    avg = cash / filled if filled > 0 else None
    return cash, avg

def baseline_twap(snapshots):
    """
    60-second buckets, equal share per bucket, always take best ask.
    """
    times = [pd.to_datetime(s["ts"]) for s in snapshots]
    t0 = times[0]
    buckets = defaultdict(list)
    for t, s in zip(times, snapshots):
        idx = int((t - t0).total_seconds() // 60)
        buckets[idx].append(s)
    rem, cash = ORDER_SIZE, 0.0
    B = len(buckets)
    for b in sorted(buckets):
        if rem <= 0:
            break
        snap = buckets[b][0]
        share = rem / (B - b)
        best = min(snap["venues"], key=lambda v: v["ask"])
        take = min(share, best["ask_size"], rem)
        cash += take * best["ask"]
        rem -= take
    filled = ORDER_SIZE - rem
    avg = cash / filled if filled > 0 else None
    return cash, avg

def baseline_vwap(snapshots):
    """
    At each snapshot, weight by displayed ask size,
    allocate remaining order pro rata, take up to ask_size.
    """
    rem, cash = ORDER_SIZE, 0.0
    for snap in snapshots:
        if rem <= 0:
            break
        total_sz = sum(v["ask_size"] for v in snap["venues"])
        if total_sz <= 0:
            continue
        for v in snap["venues"]:
            share = rem * (v["ask_size"] / total_sz)
            take = min(share, v["ask_size"], rem)
            cash += take * v["ask"]
            rem -= take
    filled = ORDER_SIZE - rem
    avg = cash / filled if filled > 0 else None
    return cash, avg

def cumulative_cash_curve(snapshots, lam_over, lam_under, theta):
    """
    Return list of cumulative cash spent *up to* each snapshot,
    including final under/over‐fill penalty at the last point.
    """
    remaining = ORDER_SIZE
    cash = 0.0
    cum = []
    for snap in snapshots:
        if remaining <= 0:
            break
        split, _ = allocate(remaining, snap["venues"], lam_over, lam_under, theta)
        for qty, v in zip(split, snap["venues"]):
            exe = min(qty, remaining, v["ask_size"])
            cash += exe * (v["ask"] + v["fee"])
            cash -= max(qty - exe, 0) * v["rebate"]
            remaining -= exe
        cum.append(cash)
    under = max(remaining, 0)
    over  = max(-remaining, 0)
    penalty = lam_under * under + lam_over * over
    if cum:
        cum[-1] += penalty
    return cum

def bps_savings(base, ours):
    return (base - ours) / base * 10000

def main():
    fn = sys.argv[1] if len(sys.argv) > 1 else "l1_day.csv"
    if not os.path.isfile(fn):
        sys.exit(f"ERROR: file not found: {fn}")

    snaps = load_snapshots(fn)

    # simple grid search
    lam_over_list  = np.linspace(0.001, 0.02, 20)
    lam_under_list = np.linspace(0.01, 0.08, 20)
    theta_list     = np.linspace(0, 0.005, 20)

    best = {"cost": float("inf")}
    for lo in lam_over_list:
        for lu in lam_under_list:
            for th in theta_list:
                cost, avg = backtest_static(snaps, lo, lu, th)
                if cost < best["cost"]:
                    best.update({"lo": lo, "lu": lu, "th": th, "cost": cost, "avg": avg})

    final_cash, final_avg = backtest_static(snaps, best["lo"], best["lu"], best["th"])
    ba_cash, ba_avg       = baseline_bestask(snaps)
    tw_cash, tw_avg       = baseline_twap(snaps)
    vw_cash, vw_avg       = baseline_vwap(snaps)

    output = {
      "best_params": {
        "lambda_over":  best["lo"],
        "lambda_under": best["lu"],
        "theta_queue":  best["th"]
      },
      "our_strategy": {
        "total_cash":     round(final_cash, 4),
        "average_price":  round(final_avg, 6)
      },
      "baseline_bestask": {
        "total_cash":    round(ba_cash, 4),
        "average_price": round(ba_avg, 6)
      },
      "baseline_twap": {
        "total_cash":    round(tw_cash, 4),
        "average_price": round(tw_avg, 6)
      },
      "baseline_vwap": {
        "total_cash":    round(vw_cash, 4),
        "average_price": round(vw_avg, 6)
      },
      "savings_vs_bestask_bps": round(bps_savings(ba_cash, final_cash), 4),
      "savings_vs_twap_bps":    round(bps_savings(tw_cash, final_cash), 4),
      "savings_vs_vwap_bps":    round(bps_savings(vw_cash, final_cash), 4)
    }

    print(json.dumps(output, indent=2))

    # generate cumulative‐cost plot
    curve = cumulative_cash_curve(snaps, best["lo"], best["lu"], best["th"])
    plt.figure(figsize=(8, 4))
    plt.plot(curve, label="SOR cumulative cash")
    plt.xlabel("Snapshot index")
    plt.ylabel("Cumulative cash spent")
    plt.title("Cumulative Cost of Static SOR")
    plt.legend()
    plt.savefig("results.png", dpi=150, bbox_inches="tight")

if __name__ == "__main__":
    main()
