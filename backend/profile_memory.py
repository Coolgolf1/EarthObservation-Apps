"""
Memory profiling script for EarthAnalyser backend.

SAFE version — stops BEFORE crashing. Checks available memory before each
allocation and maintains a 500 MB safety margin at all times.

Tests:
  1. Raw numpy array allocation (baseline)
  2. H3 hexagon dict build-up

Usage:
    python3 profile_memory.py [--test {numpy,h3,all}] [--headroom-mb 500]
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

import psutil
import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROCESS = psutil.Process(os.getpid())
_MB = 1024 ** 2
_GB = 1024 ** 3

# Minimum available system memory we MUST keep free at all times
HEADROOM_MB = 500


def rss_mb() -> float:
    """Current resident set size in MB."""
    return _PROCESS.memory_info().rss / _MB


def available_mb() -> float:
    """Available system memory in MB."""
    return psutil.virtual_memory().available / _MB


def log(msg: str) -> None:
    print(f"[profile] RSS={rss_mb():,.0f} MB | avail={available_mb():,.0f} MB | {msg}",
          flush=True)


def is_safe(next_alloc_mb: float = 0) -> bool:
    """Return True if it's safe to allocate next_alloc_mb more."""
    avail = available_mb()
    # Must keep HEADROOM_MB free AFTER the allocation
    return avail - next_alloc_mb > HEADROOM_MB


def would_exceed(next_alloc_mb: float) -> bool:
    """Return True if the next allocation would breach the safety margin."""
    return not is_safe(next_alloc_mb)


# ---------------------------------------------------------------------------
# Test 1 — raw numpy allocations (SAFE)
# ---------------------------------------------------------------------------

def test_numpy_allocation(headroom: float) -> dict:
    """Allocate float32 arrays in 128 MB steps, stopping BEFORE memory runs out."""
    print("\n" + "=" * 60)
    print("TEST: Raw numpy allocation (float32)")
    print("=" * 60)

    arrays: list[np.ndarray] = []
    step_mb = 128  # smaller steps for precision
    step_pixels = int(step_mb * _MB / 4)  # float32 = 4 bytes

    baseline_rss = rss_mb()
    iteration = 0

    while True:
        iteration += 1
        # CHECK BEFORE allocating
        if would_exceed(step_mb):
            log(f"STOP: next {step_mb} MB chunk would breach {HEADROOM_MB} MB headroom "
                f"(avail={available_mb():.0f} MB)")
            break

        log(f"Allocating {step_mb} MB chunk #{iteration}...")
        arr = np.empty(step_pixels, dtype=np.float32)
        arr[:] = 1.0  # force physical pages
        arrays.append(arr)
        log(f"Chunk #{iteration} OK")

    total_allocated_mb = sum(a.nbytes for a in arrays) / _MB
    peak_rss = rss_mb()
    result = {
        "test": "numpy",
        "chunks": len(arrays),
        "step_mb": step_mb,
        "total_allocated_mb": total_allocated_mb,
        "peak_rss_mb": peak_rss,
        "baseline_rss_mb": baseline_rss,
        "available_at_stop_mb": available_mb(),
    }

    log(f"RESULT: {total_allocated_mb:,.0f} MB allocated in {len(arrays)} chunks")
    log(f"Peak RSS: {peak_rss:,.0f} MB | Available at stop: {available_mb():,.0f} MB")

    # Cleanup
    del arrays
    gc.collect()
    log("Cleaned up")
    return result


# ---------------------------------------------------------------------------
# Test 2 — H3 hexagon dict build-up (SAFE)
# ---------------------------------------------------------------------------

def test_h3_buildup(headroom: float) -> dict:
    """Build H3 dicts with increasing grids, stopping before memory runs out."""
    print("\n" + "=" * 60)
    print("TEST: H3 hexagon dictionary build-up")
    print("=" * 60)

    try:
        import h3
    except ImportError:
        log("h3 not installed — skipping")
        return {"test": "h3", "status": "skipped"}

    # Incremental scenarios — each one is independent (cleaned up after)
    scenarios = [
        ("100x100",       100,   100, 5),
        ("300x300",       300,   300, 5),
        ("500x500",       500,   500, 5),
        ("1000x1000",    1000,  1000, 5),
        ("1500x1500",    1500,  1500, 5),
        ("2000x2000",    2000,  2000, 5),
        ("3000x3000",    3000,  3000, 5),
        ("4000x4000",    4000,  4000, 5),
        ("5000x5000",    5000,  5000, 5),
        ("6000x6000",    6000,  6000, 5),
        ("8000x8000",    8000,  8000, 5),
    ]

    results: list[dict] = []
    last_safe_pixels = 0

    for label, rows, cols, h3_res in scenarios:
        total_px = rows * cols
        # Estimate memory needed: grid (float32) + hex dict overhead
        # Grid = rows*cols*4 bytes; dict ≈ ~200 bytes per hex cell
        est_grid_mb = (rows * cols * 4) / _MB
        # Rough estimate for hex count at res 5: about 1 hex per 100 pixels
        est_hex_count = total_px // 50
        est_dict_mb = (est_hex_count * 200) / _MB
        est_total_mb = est_grid_mb + est_dict_mb

        log(f"H3 scenario '{label}': {total_px:,} px | est ~{est_total_mb:,.0f} MB needed")

        # Safety check BEFORE starting
        if would_exceed(est_total_mb):
            log(f"STOP: estimated {est_total_mb:.0f} MB would breach headroom "
                f"(avail={available_mb():.0f} MB)")
            results.append({"scenario": label, "pixels": total_px,
                            "est_mb": est_total_mb, "status": "stopped_safe"})
            break

        try:
            lats = np.linspace(40.0, 41.0, rows)
            lons = np.linspace(-4.0, -3.0, cols)
            values = np.random.rand(rows, cols).astype(np.float32) * 10000

            hexagons: dict[str, dict] = {}

            t0 = time.time()
            for lat_idx in range(rows):
                # Check every 500 rows during processing
                if lat_idx > 0 and lat_idx % 500 == 0:
                    if available_mb() < HEADROOM_MB:
                        log(f"  MID-RUN STOP at row {lat_idx}/{rows} — "
                            f"avail dropped to {available_mb():.0f} MB")
                        break
                for lon_idx in range(cols):
                    val = float(values[lat_idx, lon_idx])
                    cell = h3.latlng_to_cell(float(lats[lat_idx]),
                                             float(lons[lon_idx]), h3_res)
                    if cell not in hexagons:
                        hexagons[cell] = {
                            "h3": cell,
                            "pixel_count": 0,
                            "value_sum": 0.0,
                            "value_max": -1e9,
                            "value_min": 1e9,
                            "classes": defaultdict(int),
                        }
                    hexagons[cell]["pixel_count"] += 1
                    hexagons[cell]["value_sum"] += val
                    hexagons[cell]["value_max"] = max(hexagons[cell]["value_max"], val)
                    hexagons[cell]["value_min"] = min(hexagons[cell]["value_min"], val)

            elapsed = time.time() - t0
            n_hex = len(hexagons)
            log(f"  {n_hex:,} hexagons in {elapsed:.1f}s")
            results.append({"scenario": label, "pixels": total_px,
                            "hexagons": n_hex, "elapsed_s": elapsed,
                            "rss_mb": rss_mb(), "status": "ok"})
            last_safe_pixels = total_px

            del hexagons, values, lats, lons
            gc.collect()

        except Exception as exc:
            log(f"  Error: {exc}")
            results.append({"scenario": label, "pixels": total_px,
                            "status": f"error: {exc}"})
            gc.collect()
            break

    return {
        "test": "h3",
        "scenarios": results,
        "last_safe_pixels": last_safe_pixels,
    }


# ---------------------------------------------------------------------------
# Final Report
# ---------------------------------------------------------------------------

def print_report(results: list[dict]) -> None:
    """Print a final summary report."""
    print("\n")
    print("=" * 60)
    print("  MEMORY PROFILING REPORT")
    print("=" * 60)
    total_ram = psutil.virtual_memory().total / _MB
    print(f"\n  System total RAM:  {total_ram:,.0f} MB ({total_ram / 1024:.1f} GB)")
    print(f"  Safety headroom:   {HEADROOM_MB} MB")
    print(f"  Usable for data:   {total_ram - HEADROOM_MB:,.0f} MB")
    print()

    for r in results:
        test_name = r.get("test", "?")
        if test_name == "numpy":
            safe_mb = r["total_allocated_mb"]
            print(f"  [{test_name.upper()}]")
            print(f"    Max safe allocation:  {safe_mb:,.0f} MB ({safe_mb / 1024:.1f} GB)")
            print(f"    Peak RSS:             {r['peak_rss_mb']:,.0f} MB")
            print(f"    Available at stop:    {r['available_at_stop_mb']:,.0f} MB")
            print(f"    Recommended limit:    {safe_mb * 0.8:,.0f} MB (80% safety factor)")
            print()
        elif test_name == "h3":
            last_px = r.get("last_safe_pixels", 0)
            print(f"  [{test_name.upper()}]")
            print(f"    Last safe grid:       {last_px:,} pixels")
            scenarios = r.get("scenarios", [])
            if scenarios:
                print(f"    Scenarios completed:  {sum(1 for s in scenarios if s['status'] == 'ok')}/{len(scenarios)}")
                for s in scenarios:
                    status_mark = "OK" if s["status"] == "ok" else "STOP"
                    extra = ""
                    if s["status"] == "ok":
                        extra = f"  ({s.get('hexagons', '?')} hex, {s.get('elapsed_s', 0):.1f}s)"
                    print(f"      {s['scenario']:>12s}  {s['pixels']:>12,} px  [{status_mark}]{extra}")
            print(f"    Recommended MAX_H3_INPUT_PIXELS:  {int(last_px * 0.7):,} (70% safety)")
            print()

    print("  " + "-" * 56)
    print("  RECOMMENDED CONSTANTS:")
    for r in results:
        if r.get("test") == "numpy":
            rec = int(r["total_allocated_mb"] * 0.8)
            print(f"    MAX_ERA5_BYTES        = {int(rec * _MB):_}  # ~{rec} MB")
            print(f"    MAX_IMAGECUBES_BYTES  = {int(rec * _MB):_}  # ~{rec} MB")
            print(f"    MAX_RASTER_BYTES_TOTAL= {int(rec * _MB):_}  # ~{rec} MB")
        if r.get("test") == "h3":
            last_px = r.get("last_safe_pixels", 10_000_000)
            rec_px = int(last_px * 0.7)
            print(f"    MAX_H3_INPUT_PIXELS   = {rec_px:_}  # {rec_px:,} pixels")
    print("  " + "-" * 56)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global HEADROOM_MB

    parser = argparse.ArgumentParser(description="SAFE memory profiling for EarthAnalyser")
    parser.add_argument("--test", choices=["numpy", "h3", "all"],
                        default="all", help="Which test to run (default: all)")
    parser.add_argument("--headroom-mb", type=float, default=500,
                        help="Minimum available MB to keep free (default: 500)")
    args = parser.parse_args()
    HEADROOM_MB = args.headroom_mb

    print("=" * 60)
    print("EarthAnalyser SAFE Memory Profiler")
    print("=" * 60)
    log(f"PID: {os.getpid()}")
    log(f"System RAM: {psutil.virtual_memory().total / _GB:.1f} GB")
    log(f"Available:  {psutil.virtual_memory().available / _GB:.1f} GB")
    log(f"Headroom:   {HEADROOM_MB} MB (will NOT go below this)")
    print()

    all_results: list[dict] = []

    if args.test in ("numpy", "all"):
        r = test_numpy_allocation(HEADROOM_MB)
        all_results.append(r)
        gc.collect()

    if args.test in ("h3", "all"):
        r = test_h3_buildup(HEADROOM_MB)
        all_results.append(r)
        gc.collect()

    print_report(all_results)
    log("Done — process still alive!")


if __name__ == "__main__":
    main()
