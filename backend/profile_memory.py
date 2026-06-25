"""
Memory profiling script for EarthAnalyser backend.

Progressively increases data load sizes to find the threshold at which
memory is exhausted. Tests:
  1. Raw numpy array allocation (baseline)
  2. ERA5 Zarr loads (xarray .compute())
  3. Raster reads via rasterio (COG from Planetary Computer)
  4. H3 hexagon dict build-up

Usage:
    python profile_memory.py [--test {numpy,era5,raster,h3,all}] [--max-gb 8]

The script logs RSS (resident set size) after each step and stops
when allocation fails or RSS exceeds --max-gb.
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import time
import traceback
from datetime import date, timedelta

import psutil
import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROCESS = psutil.Process(os.getpid())
_MB = 1024 ** 2
_GB = 1024 ** 3


def rss_mb() -> float:
    """Current resident set size in MB."""
    return _PROCESS.memory_info().rss / _MB


def available_mb() -> float:
    """Available system memory in MB."""
    return psutil.virtual_memory().available / _MB


def log(msg: str) -> None:
    print(f"[profile] RSS={rss_mb():,.0f} MB | avail={available_mb():,.0f} MB | {msg}",
          flush=True)


def check_limit(max_gb: float) -> bool:
    """Return True if RSS exceeds the safety ceiling."""
    return rss_mb() > max_gb * 1024


# ---------------------------------------------------------------------------
# Test 1 — raw numpy allocations
# ---------------------------------------------------------------------------

def test_numpy_allocation(max_gb: float) -> None:
    """Allocate increasingly large float32 arrays until OOM or limit."""
    print("\n" + "=" * 60)
    print("TEST: Raw numpy allocation (float32)")
    print("=" * 60)

    arrays: list[np.ndarray] = []
    step_mb = 256  # allocate in 256 MB increments
    step_pixels = int(step_mb * _MB / 4)  # float32 = 4 bytes

    iteration = 0
    while True:
        iteration += 1
        target_mb = step_mb * iteration
        log(f"Allocating {step_mb} MB chunk (total target: {target_mb} MB)...")
        try:
            arr = np.empty(step_pixels, dtype=np.float32)
            arr[:] = 1.0  # force physical allocation
            arrays.append(arr)
            log(f"Allocated chunk #{iteration} OK")
        except MemoryError:
            log(f"MemoryError at chunk #{iteration} ({target_mb} MB target)")
            break

        if check_limit(max_gb):
            log(f"RSS exceeded {max_gb} GB ceiling — stopping")
            break

    total_allocated = sum(a.nbytes for a in arrays) / _MB
    log(f"Total numpy allocated: {total_allocated:,.0f} MB in {len(arrays)} chunks")

    # Cleanup
    del arrays
    gc.collect()
    log("Cleaned up numpy arrays")


# ---------------------------------------------------------------------------
# Test 2 — ERA5 Zarr loads
# ---------------------------------------------------------------------------

def test_era5_loads(max_gb: float) -> None:
    """Load ERA5 data with increasing bbox / date ranges."""
    print("\n" + "=" * 60)
    print("TEST: ERA5 Zarr loads (.compute())")
    print("=" * 60)

    try:
        import xarray as xr
    except ImportError:
        log("xarray not installed — skipping ERA5 test")
        return

    ERA5_STORAGE_OPTIONS = {"account_name": "cpdataeuwest"}
    ERA5_VAR = "air_temperature_at_2_metres"

    # Define increasingly large loads:
    # (label, bbox [west, south, east, north], days)
    scenarios = [
        ("tiny",     [-3.70, 40.40, -3.68, 40.42],  7),
        ("small",    [-3.80, 40.30, -3.60, 40.50], 14),
        ("medium",   [-4.00, 40.00, -3.00, 41.00], 30),
        ("large",    [-6.00, 38.00, -2.00, 42.00], 30),
        ("xlarge",   [-10.0, 36.00,  4.00, 44.00], 30),
        ("xxlarge",  [-10.0, 36.00,  4.00, 44.00], 90),
        ("huge",     [-20.0, 30.00, 20.00, 60.00], 30),
        ("massive",  [-20.0, 30.00, 20.00, 60.00], 90),
        ("extreme",  [-180, -90, 180, 90],          30),
    ]

    results: list[dict] = []

    for label, bbox, days in scenarios:
        west, south, east, north = bbox
        dt_start = date(2023, 6, 1)
        dt_end = dt_start + timedelta(days=days - 1)

        log(f"ERA5 scenario '{label}': bbox={bbox}, days={days}")

        try:
            # Determine months needed
            months_needed = []
            cur = dt_start.replace(day=1)
            while cur <= dt_end:
                months_needed.append((cur.year, cur.month))
                if cur.month == 12:
                    cur = cur.replace(year=cur.year + 1, month=1)
                else:
                    cur = cur.replace(month=cur.month + 1)

            # Open zarr stores
            arrays = []
            for year, month in months_needed:
                url = f"abfs://era5/ERA5/{year}/{month:02d}/{ERA5_VAR}.zarr"
                ds = xr.open_zarr(url, storage_options=ERA5_STORAGE_OPTIONS,
                                  consolidated=True, chunks={})
                arrays.append(ds[ERA5_VAR])

            if len(arrays) == 1:
                da = arrays[0]
            else:
                da = xr.concat(arrays, dim="time")

            lon_west = west % 360
            lon_east = east % 360
            if lon_west <= lon_east:
                lon_slice = slice(lon_west, lon_east)
            else:
                lon_slice = slice(0, 360)
            lat_slice = slice(north, south)
            time_slice = slice(str(dt_start), str(dt_end))

            subset = da.sel(lon=lon_slice, lat=lat_slice, time=time_slice)

            # Check size before loading
            nbytes_est = subset.nbytes
            log(f"  Estimated array size: {nbytes_est / _MB:,.0f} MB")

            if nbytes_est / _GB > max_gb:
                log(f"  Would exceed {max_gb} GB ceiling — skipping compute()")
                results.append({"scenario": label, "est_mb": nbytes_est / _MB,
                                "status": "skipped"})
                del subset, da, arrays
                gc.collect()
                continue

            t0 = time.time()
            daily = subset.resample(time="1D").mean()
            data = daily.compute()
            elapsed = time.time() - t0

            actual_mb = data.nbytes / _MB
            log(f"  Loaded {actual_mb:,.0f} MB in {elapsed:.1f}s — shape {data.shape}")
            results.append({"scenario": label, "est_mb": nbytes_est / _MB,
                            "actual_mb": actual_mb, "elapsed_s": elapsed,
                            "status": "ok"})

            del data, daily, subset, da, arrays
            gc.collect()

        except MemoryError:
            log(f"  MemoryError on scenario '{label}'")
            results.append({"scenario": label, "status": "oom"})
            gc.collect()
            break
        except Exception as exc:
            log(f"  Error on scenario '{label}': {exc}")
            results.append({"scenario": label, "status": f"error: {exc}"})
            gc.collect()

        if check_limit(max_gb):
            log(f"RSS exceeded {max_gb} GB ceiling — stopping ERA5 tests")
            break

    print("\n--- ERA5 summary ---")
    for r in results:
        print(f"  {r['scenario']:>10s}  est={r.get('est_mb', '?'):>8}  "
              f"actual={r.get('actual_mb', '?'):>8}  {r['status']}")


# ---------------------------------------------------------------------------
# Test 3 — Raster reads (Planetary Computer COGs)
# ---------------------------------------------------------------------------

def test_raster_reads(max_gb: float) -> None:
    """Read rasters with increasing window sizes."""
    print("\n" + "=" * 60)
    print("TEST: Raster reads (COG via rasterio)")
    print("=" * 60)

    try:
        import rasterio
        from rasterio.windows import Window, from_bounds
        from rasterio.warp import transform_bounds
        import planetary_computer
        import pystac_client
    except ImportError as e:
        log(f"Missing dependency ({e}) — skipping raster test")
        return

    CATALOG_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

    # We'll query a MODIS fire item and read increasingly large windows
    scenarios = [
        ("small",   [-3.80, 40.30, -3.60, 40.50]),
        ("medium",  [-5.00, 39.00, -2.00, 42.00]),
        ("large",   [-10.0, 35.00,  5.00, 45.00]),
        ("xlarge",  [-20.0, 25.00, 40.00, 60.00]),
        ("global",  [-180, -60, 180, 75]),
    ]

    results: list[dict] = []
    test_date = date(2023, 7, 15)

    catalog = pystac_client.Client.open(CATALOG_URL)

    for label, bbox in scenarios:
        log(f"Raster scenario '{label}': bbox={bbox}")

        try:
            search = catalog.search(
                collections=["modis-14A1-061"],
                bbox=bbox,
                datetime=test_date.isoformat(),
                limit=1,
            )
            items = list(search.item_collection())
            if not items:
                log(f"  No items found for bbox={bbox}")
                results.append({"scenario": label, "status": "no items"})
                continue

            item = items[0]
            href = planetary_computer.sign(item.assets["FireMask"].href)

            with rasterio.open(href) as ds:
                west, south, east, north = bbox
                minx, miny, maxx, maxy = transform_bounds(
                    "EPSG:4326", ds.crs, west, south, east, north, densify_pts=21)

                candidate = from_bounds(minx, miny, maxx, maxy, transform=ds.transform)
                bounded = candidate.intersection(Window(0, 0, ds.width, ds.height))
                bounded = bounded.round_offsets().round_lengths()

                if bounded.width <= 0 or bounded.height <= 0:
                    log(f"  Empty window")
                    results.append({"scenario": label, "status": "empty window"})
                    continue

                total_px = bounded.width * bounded.height
                est_mb = total_px * 4 / _MB  # float32
                log(f"  Window: {bounded.width}x{bounded.height} = {total_px:,} px "
                    f"(~{est_mb:,.0f} MB)")

                if est_mb > max_gb * 1024:
                    log(f"  Would exceed ceiling — skipping read")
                    results.append({"scenario": label, "pixels": total_px,
                                    "est_mb": est_mb, "status": "skipped"})
                    continue

                t0 = time.time()
                band = ds.read(1, window=bounded, masked=True).astype("float32")
                elapsed = time.time() - t0
                actual_mb = band.nbytes / _MB

                log(f"  Read {actual_mb:,.0f} MB in {elapsed:.1f}s — "
                    f"shape {band.shape}")
                results.append({"scenario": label, "pixels": total_px,
                                "actual_mb": actual_mb, "elapsed_s": elapsed,
                                "status": "ok"})

                del band
                gc.collect()

        except MemoryError:
            log(f"  MemoryError on scenario '{label}'")
            results.append({"scenario": label, "status": "oom"})
            gc.collect()
            break
        except Exception as exc:
            log(f"  Error on scenario '{label}': {exc}")
            traceback.print_exc()
            results.append({"scenario": label, "status": f"error: {exc}"})
            gc.collect()

        if check_limit(max_gb):
            log(f"RSS exceeded {max_gb} GB ceiling — stopping raster tests")
            break

    print("\n--- Raster summary ---")
    for r in results:
        print(f"  {r['scenario']:>10s}  px={r.get('pixels', '?'):>12}  "
              f"mb={r.get('actual_mb', '?'):>8}  {r['status']}")


# ---------------------------------------------------------------------------
# Test 4 — H3 hexagon dict build-up
# ---------------------------------------------------------------------------

def test_h3_buildup(max_gb: float) -> None:
    """Build H3 hexagon dicts with increasing pixel counts."""
    print("\n" + "=" * 60)
    print("TEST: H3 hexagon dictionary build-up")
    print("=" * 60)

    try:
        import h3
    except ImportError:
        log("h3 not installed — skipping H3 test")
        return

    from collections import defaultdict

    # Simulate grid→H3 conversion with increasing pixel grids
    scenarios = [
        ("100x100",       100,   100, 5),
        ("500x500",       500,   500, 5),
        ("1000x1000",    1000,  1000, 5),
        ("2000x2000",    2000,  2000, 5),
        ("4000x4000",    4000,  4000, 5),
        ("8000x8000",    8000,  8000, 5),
        ("10000x10000", 10000, 10000, 5),
    ]

    results: list[dict] = []

    for label, rows, cols, h3_res in scenarios:
        total_px = rows * cols
        log(f"H3 scenario '{label}': {total_px:,} pixels → H3 res {h3_res}")

        try:
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
                            "status": "ok"})

            del hexagons, values
            gc.collect()

        except MemoryError:
            log(f"  MemoryError on scenario '{label}'")
            results.append({"scenario": label, "pixels": total_px, "status": "oom"})
            gc.collect()
            break
        except Exception as exc:
            log(f"  Error: {exc}")
            results.append({"scenario": label, "pixels": total_px,
                            "status": f"error: {exc}"})
            gc.collect()

        if check_limit(max_gb):
            log(f"RSS exceeded {max_gb} GB ceiling — stopping H3 tests")
            break

    print("\n--- H3 summary ---")
    for r in results:
        print(f"  {r['scenario']:>12s}  px={r.get('pixels', '?'):>12,}  "
              f"hex={r.get('hexagons', '?'):>8}  "
              f"time={r.get('elapsed_s', '?'):>6}  {r['status']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Memory profiling for EarthAnalyser backend")
    parser.add_argument("--test", choices=["numpy", "era5", "raster", "h3", "all"],
                        default="all", help="Which test to run (default: all)")
    parser.add_argument("--max-gb", type=float, default=8.0,
                        help="Safety ceiling in GB — stop tests if RSS exceeds this (default: 8)")
    args = parser.parse_args()

    print("=" * 60)
    print("EarthAnalyser Memory Profiler")
    print("=" * 60)
    log(f"PID: {os.getpid()}")
    log(f"System RAM: {psutil.virtual_memory().total / _GB:.1f} GB")
    log(f"Available:  {psutil.virtual_memory().available / _GB:.1f} GB")
    log(f"Safety ceiling: {args.max_gb} GB")
    print()

    tests = {
        "numpy":  test_numpy_allocation,
        "era5":   test_era5_loads,
        "raster": test_raster_reads,
        "h3":     test_h3_buildup,
    }

    if args.test == "all":
        for name, fn in tests.items():
            fn(args.max_gb)
            gc.collect()
    else:
        tests[args.test](args.max_gb)

    print("\n" + "=" * 60)
    log("Profiling complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
