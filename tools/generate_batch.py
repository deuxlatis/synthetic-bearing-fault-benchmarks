#!/usr/bin/env python3
"""Generate the bearing-degradation series with the tseriz API.

Usage:
  python tools/generate_batch.py --reference-set   # data/full/{baseline,case_a,case_b}.csv, seed 4242 (3 generations)
  python tools/generate_batch.py                   # data/full/mc/: Cases A/B/C x seeds 1001-1030 (90 generations)
  python tools/generate_batch.py --qa-only         # only run qa_bearing.py on files that already exist

Options: --base URL (default $TSERIZ_BASE_URL or https://tseriz.com).
The API key is read from $TSERIZ_API_KEY and is never printed. Run it from anywhere; paths are relative to the repo.

Steps: preflight (quota, capabilities, validate), seed-4242 check against the reference CSVs when present
(stops on mismatch), generate the missing series, keep mc/manifest.json up to date, then run qa_bearing.py
on every series. Existing CSVs are skipped, so the script can be re-run.
Each 20 s series has 163,841 points: the plan must allow that many points per generation.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIGS = ROOT / "configs"
DATA = ROOT / "data" / "full"  # not tracked by git
OUT = DATA / "mc"
MANIFEST = OUT / "manifest.json"
QA_SCRIPT = HERE / "qa_bearing.py"

# case -> (exported config, reference CSV for seed 4242 or None). Reference CSVs live in data/full/.
CASES = {
    "baseline": ("baseline.json", "baseline.csv"),
    "a": ("case_a.json", "case_a.csv"),
    "b": ("case_b.json", "case_b.csv"),
    "c": ("case_c.json", None),  # Case B with constant 0.1 g impacts (no growth)
}
MC_CASES = ("a", "b", "c")  # cases run for every Monte Carlo seed
REFERENCE_SET = ("baseline", "a", "b")
SEEDS = range(1001, 1031)
REF_SEED = 4242
F32_HALF_ULP = 2.0**-24  # relative rounding error of the float32 values in the CSVs

GEN = "/api/v1/gen"
SCHEMA = "/api/schema/?format=json"


@dataclass
class Case:
    name: str
    config_path: Path
    reference: Path | None
    exported: dict
    config: dict
    description: str
    n_points: int


# ── Config handling ─────────────────────────────────────────────────────────


def _drop_none(obj):
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj


def api_config(exported: dict) -> tuple[dict, str]:
    """Exported UI config -> (API `config`, top-level description). Seed goes top-level too."""
    cfg = copy.deepcopy(exported)
    description = cfg.pop("description", "")
    cfg.pop("seed", None)
    cfg.pop("chart_title", None)
    return _drop_none(cfg), description


def load_cases() -> dict[str, Case]:
    cases = {}
    for name, (cfg_file, ref_file) in CASES.items():
        exported = json.loads((CONFIGS / cfg_file).read_text())
        config, description = api_config(exported)
        seconds = sum(exported.get(f"duration_{u}", 0) * s for u, s in (("days", 86400), ("hours", 3600)))
        seconds += exported.get("duration_minutes", 0) * 60 + exported.get("duration_seconds", 0)
        n_points = int(round(seconds * exported["sampling_frequency_hz"])) + 1
        cases[name] = Case(name, CONFIGS / cfg_file, ref_file and DATA / ref_file, exported, config, description, n_points)
    return cases


def _resolve(node: dict, comps: dict) -> dict:
    while True:
        if "$ref" in node:
            node = comps[node["$ref"].rsplit("/", 1)[1]]
        elif len(node.get("allOf", [])) == 1:
            node = node["allOf"][0]
        else:
            return node


def schema_diff(value, node: dict, comps: dict, path: str = "config") -> tuple[set[str], set[str]]:
    """Keys the API schema does not declare, and nulls sent for non-nullable fields."""
    node = _resolve(node, comps)
    unknown, nulls = set(), set()
    if isinstance(value, dict) and "properties" in node:
        for k, v in value.items():
            prop, p = node["properties"].get(k), f"{path}.{k}"
            if prop is None:
                unknown.add(p)
            elif v is None:
                if not prop.get("nullable"):
                    nulls.add(p)
            else:
                u, n = schema_diff(v, prop, comps, p)
                unknown |= u
                nulls |= n
    elif isinstance(value, list) and "items" in node:
        for v in value:
            u, n = schema_diff(v, node["items"], comps, f"{path}[]")
            unknown |= u
            nulls |= n
    return unknown, nulls


# ── API client ──────────────────────────────────────────────────────────────


def load_api_key() -> str:
    key = os.environ.get("TSERIZ_API_KEY", "").strip()
    if not key:
        raise SystemExit("error: set the TSERIZ_API_KEY environment variable (create a key in your tseriz account)")
    return key


class Api:
    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Api-Key {key}", "Content-Type": "application/json"})
        self.min_interval = 0.0  # seconds between create calls, from the advertised throttle
        self._last_create = 0.0

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(8):
            r = self.session.request(method, self.base + path, json=body, timeout=600)
            if r.status_code == 429 or (r.status_code >= 500 and attempt < 3):
                try:
                    wait = float(r.headers["Retry-After"])
                except (KeyError, ValueError):
                    wait = min(2 ** (attempt + 1), 64)
                print(f"    {r.status_code} on {path}; retrying in {wait:.0f} s")
                time.sleep(wait)
                continue
            if not r.ok:
                raise SystemExit(f"error: {method} {path} -> {r.status_code}: {r.text[:2000]}")
            return r.json()
        raise SystemExit(f"error: {method} {path} gave up after repeated 429/5xx responses")

    def create(self, body: dict) -> dict:
        time.sleep(max(0.0, self._last_create + self.min_interval - time.monotonic()))
        self._last_create = time.monotonic()
        return self.call("POST", f"{GEN}/generations/", body)


def request_body(case: Case, seed: int, config: dict | None = None) -> dict:
    return {
        "name": f"bearing-mc case_{case.name} seed{seed}",
        "description": case.description,
        "seed": seed,
        "config": case.config if config is None else config,
        "destination": {"type": "inline"},
    }


def preflight(api: Api, cases: dict[str, Case], needed: int) -> dict:
    q = api.call("GET", f"{GEN}/quota/")
    remaining = q["generations_remaining"]
    print(f"plan {q['plan_display']} ({q['plan_slug']}): {q['generations_used']} generations used, "
          f"{'unlimited' if remaining is None else remaining} remaining, resets {q['reset_at']}")
    if remaining is not None and remaining < needed:
        raise SystemExit(f"error: {needed} generations needed, only {remaining} left this month")

    limits = api.call("GET", f"{GEN}/capabilities/")["limits"]
    rate = limits.get("throttle_generations_per_minute")
    print(f"max_inline_points {limits['max_inline_points']:,}, advertised generations/min {rate}")
    biggest = max(c.n_points for c in cases.values())
    if limits["max_inline_points"] < biggest:
        raise SystemExit(f"error: inline cap {limits['max_inline_points']:,} < {biggest:,} points per series")
    if rate:
        api.min_interval = 60.0 / rate

    notes: dict = {"ignored_fields": [], "null_fields": [], "raw_validate_errors": {}}
    try:
        schema = requests.get(api.base + SCHEMA, timeout=60).json()
        comps = schema["components"]["schemas"]
        for case in cases.values():
            u, n = schema_diff(case.exported, comps["GenerationCreate"]["properties"]["config"], comps)
            notes["ignored_fields"] = sorted(set(notes["ignored_fields"]) | u)
            notes["null_fields"] = sorted(set(notes["null_fields"]) | n)
        print(f"exported keys not in the API schema: {', '.join(notes['ignored_fields']) or 'none'}")
        print(f"nulls sent for non-nullable fields: {', '.join(notes['null_fields']) or 'none'}")
    except (requests.RequestException, KeyError, ValueError) as exc:
        print(f"  (schema diff skipped: {exc})")

    for case in cases.values():
        raw = {k: v for k, v in case.exported.items() if k not in ("seed", "description")}
        r_raw = api.call("POST", f"{GEN}/generations/validate/", request_body(case, REF_SEED, raw))
        if not r_raw["valid"]:
            notes["raw_validate_errors"][case.name] = r_raw.get("errors")
        r = api.call("POST", f"{GEN}/generations/validate/", request_body(case, REF_SEED))
        print(f"validate case {case.name}: exported-as-is valid={r_raw['valid']}, cleaned valid={r['valid']}, "
              f"estimated_points={r['estimated_points']:,}, path={r['estimated_path']}, warnings={len(r['warnings'])}")
        if not r_raw["valid"]:
            print(f"    exported-as-is errors: {json.dumps(r_raw.get('errors'))}")
        for w in r["warnings"]:
            print(f"    warning: {w.get('message')}")
        if not r["valid"]:
            raise SystemExit(f"error: cleaned config for case {case.name} rejected: {json.dumps(r.get('errors'))}")
        if r["estimated_points"] != case.n_points:
            print(f"    note: estimate {r['estimated_points']:,} differs from expected {case.n_points:,} rows")
    return notes


# ── Generation, CSV, sanity check ───────────────────────────────────────────


def generate(api: Api, case: Case, seed: int) -> tuple[list[str], str, list, dict]:
    t0 = time.monotonic()
    res = api.create(request_body(case, seed))
    data = res.get("data") or {}
    channel = (data.get("channels") or [{}])[0]
    values, timestamps = channel.get("values") or [], data.get("timestamps") or []
    if res.get("status") != "completed" or res.get("seed") != seed or not len(values) == len(timestamps) == case.n_points:
        raise SystemExit(f"error: case {case.name} seed {seed}: status={res.get('status')} seed={res.get('seed')} "
                         f"rows={len(values)}/{len(timestamps)} (expected {case.n_points}) {res.get('error_message', '')}")
    meta = {"case": case.name, "seed": res["seed"], "id": res["id"], "status": res["status"],
            "created_at": res.get("created_at"), "rows": len(values), "seconds": round(time.monotonic() - t0, 2)}
    return timestamps, channel["name"], values, meta


def f32(v) -> str:
    """Same value formatting as the tseriz UI download (apps/phoenix/downloads.py)."""
    return "" if v is None else np.format_float_positional(np.float32(v), unique=True, trim="0")


def write_csv(path: Path, timestamps: list[str], name: str, values: list) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="") as fh:  # csv's default \r\n line ending, like the UI export
        writer = csv.writer(fh)
        writer.writerow(["timestamp", name])
        writer.writerows(zip(timestamps, map(f32, values)))
    os.replace(tmp, path)


def sanity_check(api: Api, cases: dict[str, Case]) -> dict:
    """Regenerate seed 4242 in memory and compare with the UI CSVs; exit 1 on any mismatch."""
    results, ok = {}, True
    for case in cases.values():
        if case.reference is None or not case.reference.exists():
            continue
        _, name, values, meta = generate(api, case, REF_SEED)
        ref = pd.read_csv(case.reference, dtype=str, keep_default_na=False)
        res = {"reference": case.reference.name, "id": meta["id"], "rows": len(values), "ref_rows": len(ref)}
        if len(ref) != len(values) or name not in ref.columns:
            res["passed"] = False
        else:
            # The CSV holds the shortest decimal that round-trips the float32, so compare against the
            # exact float32 value (half-ULP bound) and require the formatted strings to match too.
            api_v, ref_dec = np.asarray(values, float), ref[name].astype(float).to_numpy()
            ref_v = ref_dec.astype(np.float32).astype(float)
            diff = np.abs(api_v - ref_v)
            within = diff <= F32_HALF_ULP * np.maximum(np.abs(api_v), np.abs(ref_v)) + 1e-12
            same = np.fromiter(map(f32, values), dtype=object, count=len(values)) == ref[name].to_numpy(dtype=object)
            within &= same
            res.update(max_abs_diff_vs_csv_decimal=float(np.abs(api_v - ref_dec).max()),
                       max_abs_diff_vs_float32=float(diff.max()), within_float32_rounding=bool(within.all()),
                       identical_strings=int(same.sum()), passed=bool(within.all()))
            if not within.all():
                i = int(np.argmax(~within))
                res.update(first_mismatch_index=i, mismatches=int((~within).sum()),
                           first_mismatch=f"api {api_v[i]!r} vs csv {ref_v[i]!r}")
        ok &= res["passed"]
        print(f"sanity case {case.name} seed {REF_SEED} vs '{case.reference.name}': {json.dumps(res)}")
        results[case.name] = res
    if not ok:
        raise SystemExit("STOP: seed-4242 regeneration does not match the UI CSVs; nothing else was generated.")
    return results


# ── Manifest and QA ─────────────────────────────────────────────────────────


def load_manifest() -> dict:
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    manifest["series"] = {s["file"]: s for s in manifest.get("series", [])}
    return manifest


def save_manifest(manifest: dict) -> None:
    out = dict(manifest, series=sorted(manifest["series"].values(), key=lambda s: (s["case"], s["seed"])))
    tmp = MANIFEST.with_name(MANIFEST.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, MANIFEST)


def csv_path(case: str, seed: int) -> Path:
    return OUT / f"case_{case}_seed{seed}.csv"


def controls(cases: dict[str, Case]) -> list[Case]:
    """Cases whose seed-4242 reference CSV exists on disk."""
    return [c for c in cases.values() if c.reference is not None and c.reference.exists()]


def run_qa(cases: dict[str, Case], jobs: list[tuple[int, str]]) -> dict[str, str]:
    items = [(f"control: {c.reference.name} (seed {REF_SEED})", c.config_path, c.reference) for c in controls(cases)]
    items += [(csv_path(c, s).name, cases[c].config_path, csv_path(c, s)) for s, c in jobs if csv_path(c, s).exists()]

    def one(item):
        label, cfg, data = item
        p = subprocess.run([sys.executable, str(QA_SCRIPT), str(cfg), str(data)], capture_output=True, text=True)
        return label, p.returncode, p.stdout + p.stderr

    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
        outcomes = list(pool.map(one, items))
    status = {}
    for label, code, out in outcomes:
        status[label] = "PASS" if code == 0 else "FAIL"
        if code:
            print(f"QA FAIL {label}")
            for line in out.splitlines():
                if line.startswith("FAIL") or "Error" in line:
                    print(f"    {line}")
    series = {k: v for k, v in status.items() if not k.startswith("control")}
    missing = len(jobs) - len(series)
    print(f"QA: {sum(v == 'PASS' for v in series.values())} PASS, {sum(v == 'FAIL' for v in series.values())} FAIL"
          f"{f', {missing} missing' if missing else ''} (of {len(jobs)}); controls: "
          + ", ".join(f"{k.split(': ')[1]} {v}" for k, v in status.items() if k.startswith("control")))
    return status


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=os.environ.get("TSERIZ_BASE_URL", "https://tseriz.com"))
    parser.add_argument("--qa-only", action="store_true", help="skip the API and only run QA on existing files")
    parser.add_argument("--reference-set", action="store_true",
                        help="generate the seed-4242 series data/full/{baseline,case_a,case_b}.csv, then QA them")
    args = parser.parse_args(argv)

    cases = load_cases()
    jobs = [(seed, case) for seed in SEEDS for case in MC_CASES]
    OUT.mkdir(parents=True, exist_ok=True)

    if args.reference_set:
        api = Api(args.base, load_api_key())
        todo = [n for n in REFERENCE_SET if not cases[n].reference.exists()]
        preflight(api, {n: cases[n] for n in REFERENCE_SET}, needed=len(todo))
        for n in todo:
            timestamps, name, values, meta = generate(api, cases[n], REF_SEED)
            write_csv(cases[n].reference, timestamps, name, values)
            print(f"  wrote {cases[n].reference.relative_to(ROOT)}  id={meta['id']}  {meta['seconds']:.1f} s")
        status = run_qa({n: cases[n] for n in REFERENCE_SET}, [])
        return 0 if all(v == "PASS" for v in status.values()) else 1

    if not args.qa_only:
        api = Api(args.base, load_api_key())
        missing = [(s, c) for s, c in jobs if not csv_path(c, s).exists()]
        notes = preflight(api, cases, needed=len(missing) + len(controls(cases)))
        sanity = sanity_check(api, cases)
        manifest = load_manifest()
        manifest.update(base_url=api.base, updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        configs={k: c.config_path.name for k, c in cases.items() if k in MC_CASES}, api_notes=notes, sanity=sanity)
        save_manifest(manifest)
        print(f"{len(jobs) - len(missing)} of {len(jobs)} series already exist; generating {len(missing)}")
        t0 = time.monotonic()
        for i, (seed, case) in enumerate(missing, 1):
            timestamps, name, values, meta = generate(api, cases[case], seed)
            path = csv_path(case, seed)
            write_csv(path, timestamps, name, values)
            manifest["series"][path.name] = {"file": path.name, **meta}
            save_manifest(manifest)
            print(f"  [{i}/{len(missing)}] {path.name}  id={meta['id']}  {meta['seconds']:.1f} s")
        if missing:
            print(f"generated {len(missing)} series in {time.monotonic() - t0:.0f} s")

    status = run_qa(cases, jobs)
    if MANIFEST.exists():
        manifest = load_manifest()
        for file, entry in manifest["series"].items():
            if file in status:
                entry["qa"] = status[file]
        save_manifest(manifest)
    return 0 if all(v == "PASS" for v in status.values()) and len(status) == len(jobs) + len(controls(cases)) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
