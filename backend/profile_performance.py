"""
Performance profiling script for EarthAnalyser backend.

SAFE version — monitors memory headroom throughout. Measures processing
TIME for different data sizes to find practical throughput limits.

Tests:
  1. H3 latlng_to_cell loop at different resolutions and grid sizes
  2. Numpy array operations (NDVI-like computation)
  3. Full pipeline simulation (grid → filter → H3 aggregate → serialize)

Usage:
    uv run python3 profile_performance.py [--test {h3,numpy,pipeline,all}] [--max-seconds 30]
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from collections import defaultdict

import psutil
import numpy as np

# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

_PROCESS = psutil.Process(os.getpid())
_MB = 1024 ** 2
HEADROOM_MB = 500


def available_mb() -> float:
    return psutil.virtual_memory().available / _MB


def rss_mb() -> float:
    return _PROCESS.memory_info().rss / _MB


def is_safe(est_mb: float = 0) -> bool:
    return available_mb() - est_mb > HEADROOM_MB


def log(msg: str) -> None:
    print(f"[perf] RSS={rss_mb():,.0f} MB | avail={available_mb():,.0f} MB | {msg}",
          flush=True)


# ---------------------------------------------------------------------------
# Test 1 — H3 conversion speed at different resolutions
# ---------------------------------------------------------------------------

def test_h3_speed(max_seconds: float) -> dict:
    """Measure H3 latlng_to_cell throughput at different resolutions and grid sizes."""
    print("\n" + "=" * 60)
    print("TEST: H3 latlng_to_cell throughput")
    print("=" * 60)

    import h3

    # Test at different H3 resolutions (the app uses 1-10)
    resolutions = [3, 4, 5, 6, 7, 8, 9]

    # Grid sizes to test (pixels)
    grid_sizes = [
        (100, 100),
        (500, 500),
        (1000, 1000),
        (2000, 2000),
        (3000, 3000),
        (4000, 4000),
        (5000, 5000),
    ]

    results: list[dict] = []

    for h3_res in resolutions:
        log(f"H3 resolution {h3_res}:")
        for rows, cols in grid_sizes:
            total_px = rows * cols
            est_mb = (rows * cols * 4) / _MB + 10  # grid + overhead

            if not is_safe(est_mb):
                log(f"  {rows}x{cols}: SKIP (memory)")
                break

            lats = np.linspace(40.0, 41.0, rows)
            lons = np.linspace(-4.0, -3.0, cols)
            values = np.random.rand(rows, cols).astype(np.float32) * 10000

            hexagons: dict[str, dict] = {}
            t0 = time.time()

            for lat_idx in range(rows):
                for lon_idx in range(cols):
                    val = float(values[lat_idx, lon_idx])
                    cell = h3.latlng_to_cell(float(lats[lat_idx]),
                                             float(lons[lon_idx]), h3_res)
                    if cell not in hexagons:
                        hexagons[cell] = {"count": 0, "sum": 0.0}
                    hexagons[cell]["count"] += 1
                    hexagons[cell]["sum"] += val

                # Time check every 100 rows
                if lat_idx > 0 and lat_idx % 100 == 0:
                    elapsed = time.time() - t0
                    if elapsed > max_seconds:
                        # Extrapolate
                        fraction = lat_idx / rows
                        est_total = elapsed / fraction
                        log(f"  {rows}x{cols} res={h3_res}: TIMEOUT at row {lat_idx}/{rows} "
                            f"({elapsed:.1f}s elapsed, est total ~{est_total:.0f}s)")
                        results.append({
                            "res": h3_res, "grid": f"{rows}x{cols}",
                            "pixels": total_px, "hexagons": len(hexagons),
                            "elapsed_s": elapsed, "est_total_s": est_total,
                            "px_per_sec": int(lat_idx * cols / elapsed),
                            "status": "timeout",
                        })
                        del hexagons, values, lats, lons
                        gc.collect()
                        break
            else:
                # Completed
                elapsed = time.time() - t0
                n_hex = len(hexagons)
                px_per_sec = int(total_px / elapsed) if elapsed > 0 else 0
                log(f"  {rows}x{cols} res={h3_res}: {elapsed:.2f}s | "
                    f"{px_per_sec:,} px/s | {n_hex:,} hexagons")
                results.append({
                    "res": h3_res, "grid": f"{rows}x{cols}",
                    "pixels": total_px, "hexagons": n_hex,
                    "elapsed_s": elapsed, "est_total_s": elapsed,
                    "px_per_sec": px_per_sec,
                    "status": "ok",
                })
                del hexagons, values, lats, lons
                gc.collect()
                continue

            # If we broke out (timeout), skip larger grids for this resolution
            break

    return {"test": "h3_speed", "results": results}


# ---------------------------------------------------------------------------
# Test 2 — Numpy array operations speed
# ---------------------------------------------------------------------------

def test_numpy_speed(max_seconds: float) -> dict:
    """Measure speed of numpy operations similar to NDVI computation."""
    print("\n" + "=" * 60)
    print("TEST: Numpy array operations (NDVI-like)")
    print("=" * 60)

    # Operations: create two bands, compute (B-A)/(B+A), filter, stats
    sizes_mb = [10, 50, 100, 250, 500, 1000, 2000]
    results: list[dict] = []

    for size_mb in sizes_mb:
        n_pixels = int(size_mb * _MB / 4)  # float32
        est_total_mb = size_mb * 3  # two bands + result

        if not is_safe(est_total_mb):
            log(f"  {size_mb} MB: SKIP (memory, need ~{est_total_mb} MB)")
            results.append({"size_mb": size_mb, "status": "skip_memory"})
            break

        log(f"  {size_mb} MB per band ({n_pixels:,} pixels)...")

        t0 = time.time()

        # Simulate two raster bands
        band_a = np.random.rand(n_pixels).astype(np.float32) * 10000
        band_b = np.random.rand(n_pixels).astype(np.float32) * 10000
        t_alloc = time.time() - t0

        # NDVI-like: (B - A) / (B + A)
        t1 = time.time()
        with np.errstate(divide="ignore", invalid="ignore"):
            result = (band_b - band_a) / (band_b + band_a)
        t_compute = time.time() - t1

        # Filter valid values
        t2 = time.time()
        mask = np.isfinite(result) & (result > -1) & (result < 1)
        valid = result[mask]
        t_filter = time.time() - t2

        # Stats
        t3 = time.time()
        if len(valid) > 0:
            mean_val = float(np.mean(valid))
            std_val = float(np.std(valid))
            max_val = float(np.max(valid))
            min_val = float(np.min(valid))
        t_stats = time.time() - t3

        total = time.time() - t0
        log(f"    alloc={t_alloc:.2f}s compute={t_compute:.2f}s "
            f"filter={t_filter:.2f}s stats={t_stats:.2f}s TOTAL={total:.2f}s")

        results.append({
            "size_mb": size_mb,
            "pixels": n_pixels,
            "alloc_s": t_alloc,
            "compute_s": t_compute,
            "filter_s": t_filter,
            "stats_s": t_stats,
            "total_s": total,
            "status": "ok",
        })

        del band_a, band_b, result, valid, mask
        gc.collect()

        if total > max_seconds:
            log(f"    Exceeded {max_seconds}s — stopping numpy tests")
            break

    return {"test": "numpy_speed", "results": results}


# ---------------------------------------------------------------------------
# Test 3 — Full pipeline simulation
# ---------------------------------------------------------------------------

def test_pipeline(max_seconds: float) -> dict:
    """Simulate the full raster→filter→H3→serialize pipeline."""
    print("\n" + "=" * 60)
    print("TEST: Full pipeline (raster → H3 → JSON)")
    print("=" * 60)

    import h3

    # Simulate different request sizes that would come from the frontend
    # (bbox area determines pixel count, h3_res is auto-selected)
    scenarios = [
        # (label, rows, cols, h3_res, description)
        ("tiny_bbox",      200,   200, 9, "~0.02° span"),
        ("small_bbox",     500,   500, 8, "~0.1° span"),
        ("medium_bbox",   1000,  1000, 7, "~0.3° span"),
        ("city_bbox",     2000,  2000, 6, "~1° span"),
        ("region_bbox",   3000,  3000, 5, "~3° span"),
        ("country_bbox",  4000,  4000, 4, "~8° span"),
        ("continent_bbox",5000,  5000, 3, "~20° span"),
    ]

    results: list[dict] = []

    for label, rows, cols, h3_res, desc in scenarios:
        total_px = rows * cols
        est_mb = (rows * cols * 4 * 2) / _MB + 50  # grid + overhead

        if not is_safe(est_mb):
            log(f"  {label}: SKIP (memory)")
            results.append({"scenario": label, "pixels": total_px,
                            "h3_res": h3_res, "status": "skip_memory"})
            break

        log(f"  {label} ({desc}): {rows}x{cols} px, H3 res {h3_res}")

        # --- Phase 1: Simulate raster read ---
        t0 = time.time()
        band = np.random.rand(rows, cols).astype(np.float32) * 10000
        t_read = time.time() - t0

        # --- Phase 2: Filter (like MODIS fire filter: val >= 7 & val <= 9) ---
        t1 = time.time()
        # Simulate ~5% of pixels passing filter (realistic for fire/vegetation)
        threshold = np.percentile(band, 95)
        mask = band >= threshold
        valid_lats_idx, valid_lons_idx = np.where(mask)
        n_valid = len(valid_lats_idx)
        t_filter = time.time() - t1

        # --- Phase 3: H3 aggregation (only on filtered pixels) ---
        t2 = time.time()
        lats = np.linspace(40.0, 41.0, rows)
        lons = np.linspace(-4.0, -3.0, cols)

        hexagons: dict[str, dict] = {}
        timed_out = False

        for i in range(n_valid):
            lat_idx = valid_lats_idx[i]
            lon_idx = valid_lons_idx[i]
            val = float(band[lat_idx, lon_idx])
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

            # Check time every 10000 pixels
            if i > 0 and i % 10000 == 0:
                elapsed_h3 = time.time() - t2
                if elapsed_h3 > max_seconds:
                    fraction = i / n_valid
                    est_h3 = elapsed_h3 / fraction
                    log(f"    H3 TIMEOUT at {i}/{n_valid} filtered px "
                        f"({elapsed_h3:.1f}s, est ~{est_h3:.0f}s)")
                    timed_out = True
                    break

        t_h3 = time.time() - t2

        # --- Phase 4: Serialize to JSON ---
        t3 = time.time()
        output = []
        for cell_id, data in hexagons.items():
            n = data["pixel_count"]
            output.append({
                "h3": cell_id,
                "value": data["value_sum"] / n if n > 0 else 0,
                "count": n,
            })
        json_str = json.dumps(output)
        t_serialize = time.time() - t3

        total = time.time() - t0
        n_hex = len(hexagons)

        log(f"    read={t_read:.2f}s filter={t_filter:.2f}s "
            f"h3={t_h3:.2f}s serialize={t_serialize:.2f}s "
            f"TOTAL={total:.2f}s | {n_valid:,} valid px → {n_hex:,} hex")

        results.append({
            "scenario": label,
            "desc": desc,
            "pixels": total_px,
            "valid_pixels": n_valid,
            "h3_res": h3_res,
            "hexagons": n_hex,
            "t_read": t_read,
            "t_filter": t_filter,
            "t_h3": t_h3,
            "t_serialize": t_serialize,
            "total_s": total,
            "status": "timeout" if timed_out else "ok",
            "json_kb": len(json_str) / 1024,
        })

        del band, hexagons, output, json_str, valid_lats_idx, valid_lons_idx
        gc.collect()

        if timed_out or total > max_seconds:
            log(f"    Exceeded time limit — stopping pipeline tests")
            break

    return {"test": "pipeline", "results": results}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(all_results: list[dict], max_seconds: float) -> None:
    print("\n")
    print("=" * 60)
    print("  PERFORMANCE PROFILING REPORT")
    print(f"  (max acceptable time per request: {max_seconds}s)")
    print("=" * 60)

    for section in all_results:
        test_name = section["test"]
        results = section["results"]

        if test_name == "h3_speed":
            print(f"\n  [H3 CONVERSION SPEED]")
            print(f"  {'Res':>3s} {'Grid':>10s} {'Pixels':>12s} {'Time':>8s} "
                  f"{'px/sec':>10s} {'Hexagons':>10s} {'Status':>8s}")
            print(f"  {'-'*3} {'-'*10} {'-'*12} {'-'*8} {'-'*10} {'-'*10} {'-'*8}")
            for r in results:
                print(f"  {r['res']:>3d} {r['grid']:>10s} {r['pixels']:>12,} "
                      f"{r['elapsed_s']:>7.1f}s {r['px_per_sec']:>10,} "
                      f"{r['hexagons']:>10,} {r['status']:>8s}")

            # Find the largest OK grid for each resolution within max_seconds
            print(f"\n  Max grid processable within {max_seconds}s per H3 resolution:")
            for res in sorted(set(r["res"] for r in results)):
                ok_results = [r for r in results if r["res"] == res and r["status"] == "ok"
                              and r["elapsed_s"] <= max_seconds]
                if ok_results:
                    best = max(ok_results, key=lambda x: x["pixels"])
                    print(f"    res {res}: {best['grid']} ({best['pixels']:,} px) in {best['elapsed_s']:.1f}s")
                else:
                    timeout_results = [r for r in results if r["res"] == res]
                    if timeout_results:
                        print(f"    res {res}: < {timeout_results[0]['grid']} (too slow)")

        elif test_name == "numpy_speed":
            print(f"\n  [NUMPY OPERATIONS SPEED]")
            print(f"  {'Size':>8s} {'Pixels':>14s} {'Alloc':>8s} {'Compute':>8s} "
                  f"{'Filter':>8s} {'Stats':>8s} {'Total':>8s}")
            print(f"  {'-'*8} {'-'*14} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
            for r in results:
                if r["status"] == "ok":
                    print(f"  {r['size_mb']:>6d}MB {r['pixels']:>14,} "
                          f"{r['alloc_s']:>7.2f}s {r['compute_s']:>7.2f}s "
                          f"{r['filter_s']:>7.2f}s {r['stats_s']:>7.2f}s "
                          f"{r['total_s']:>7.2f}s")
            ok = [r for r in results if r["status"] == "ok" and r["total_s"] <= max_seconds]
            if ok:
                best = max(ok, key=lambda x: x["size_mb"])
                print(f"\n  Max numpy operation within {max_seconds}s: "
                      f"{best['size_mb']} MB ({best['pixels']:,} pixels)")

        elif test_name == "pipeline":
            print(f"\n  [FULL PIPELINE (raster → filter → H3 → JSON)]")
            print(f"  {'Scenario':>16s} {'Pixels':>12s} {'Valid':>10s} {'Res':>3s} "
                  f"{'Hex':>8s} {'Read':>6s} {'Filter':>6s} {'H3':>7s} "
                  f"{'JSON':>6s} {'Total':>7s} {'Status':>7s}")
            print(f"  {'-'*16} {'-'*12} {'-'*10} {'-'*3} "
                  f"{'-'*8} {'-'*6} {'-'*6} {'-'*7} "
                  f"{'-'*6} {'-'*7} {'-'*7}")
            for r in results:
                print(f"  {r['scenario']:>16s} {r['pixels']:>12,} "
                      f"{r['valid_pixels']:>10,} {r['h3_res']:>3d} "
                      f"{r['hexagons']:>8,} {r['t_read']:>5.2f}s "
                      f"{r['t_filter']:>5.2f}s {r['t_h3']:>6.2f}s "
                      f"{r['t_serialize']:>5.2f}s {r['total_s']:>6.2f}s "
                      f"{r['status']:>7s}")

            ok = [r for r in results if r["status"] == "ok" and r["total_s"] <= max_seconds]
            if ok:
                best = max(ok, key=lambda x: x["pixels"])
                print(f"\n  Largest request completable within {max_seconds}s:")
                print(f"    {best['scenario']} — {best['pixels']:,} total px, "
                      f"{best['valid_pixels']:,} valid px, "
                      f"H3 res {best['h3_res']}, {best['hexagons']:,} hexagons")
                print(f"    Response JSON: {best['json_kb']:.1f} KB")

    # Final recommendations
    print(f"\n  {'='*56}")
    print(f"  RECOMMENDED LIMITS (for <{max_seconds}s response time):")
    print(f"  {'='*56}")

    for section in all_results:
        if section["test"] == "pipeline":
            ok = [r for r in section["results"]
                  if r["status"] == "ok" and r["total_s"] <= max_seconds]
            if ok:
                best = max(ok, key=lambda x: x["pixels"])
                # Apply 70% safety factor for real-world overhead
                safe_px = int(best["pixels"] * 0.7)
                safe_valid = int(best["valid_pixels"] * 0.7)
                print(f"    MAX_WINDOW_PIXELS  = {safe_px:_}  "
                      f"# {safe_px:,} ({best['scenario']})")
                print(f"    MAX_H3_INPUT_PIXELS = {safe_valid:_}  "
                      f"# filtered pixels going to H3")
                # H3 res recommendations
                print(f"\n    H3 resolution bounds (for acceptable speed):")
                for r in section["results"]:
                    if r["status"] == "ok":
                        print(f"      {r['desc']:>12s} → res {r['h3_res']}: "
                              f"{r['total_s']:.1f}s")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="SAFE performance profiling")
    parser.add_argument("--test", choices=["h3", "numpy", "pipeline", "all"],
                        default="all")
    parser.add_argument("--max-seconds", type=float, default=30,
                        help="Max acceptable time per operation (default: 30s)")
    parser.add_argument("--headroom-mb", type=float, default=500)
    args = parser.parse_args()

    global HEADROOM_MB
    HEADROOM_MB = args.headroom_mb

    print("=" * 60)
    print("EarthAnalyser SAFE Performance Profiler")
    print("=" * 60)
    log(f"PID: {os.getpid()}")
    log(f"Max time per test: {args.max_seconds}s")
    print()

    all_results: list[dict] = []

    if args.test in ("h3", "all"):
        r = test_h3_speed(args.max_seconds)
        all_results.append(r)
        gc.collect()

    if args.test in ("numpy", "all"):
        r = test_numpy_speed(args.max_seconds)
        all_results.append(r)
        gc.collect()

    if args.test in ("pipeline", "all"):
        r = test_pipeline(args.max_seconds)
        all_results.append(r)
        gc.collect()

    print_report(all_results, args.max_seconds)
    log("Done — process alive!")


if __name__ == "__main__":
    main()
