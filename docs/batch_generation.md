# Batch generation with the tseriz API

How the 90 Monte Carlo series (Cases A, B, C x seeds 1001-1030) were produced. The client is `tools/generate_batch.py`.

## Why a batch

One series per case shows that a detector can find a fault. It does not show how much the detection delay depends on the particular noise realisation. That needs a Monte Carlo study: the same configs run many times, changing only the random seed. Each series has 163,841 points (20 s at 8192 Hz).

## What the API provides

| Need | tseriz feature |
|---|---|
| Build the scenario once, visually | Design the config in the web app and export it as JSON (`configs/`) |
| Change only the noise realisation | `seed` is a top-level request field; the config stays untouched |
| Prove a script reproduces the UI | The same config and seed give the same series, byte for byte |
| Check a config without spending quota | `POST /api/v1/gen/generations/validate/` is a dry run |
| Plan a batch against limits | `GET /api/v1/gen/quota/` and `GET /api/v1/gen/capabilities/` |
| Get data straight into a script | The `inline` destination returns the series in the response body |
| Audit trail | Every run has an `id`, a `status` and the echoed `seed` |

Requests use an API key: `Authorization: Api-Key $TSERIZ_API_KEY`. The key is read from the environment and never written to disk or printed.

## The process

1. **Preflight (free).** Quota, capabilities and a validation of each config. The script stops if the quota cannot cover the batch or the inline limit is below 163,841 points.
2. **Map the exported config onto the API.** `seed` and `description` are top-level request fields, not part of `config`. Null fields such as `trend_slope` must be omitted rather than sent as `null`. The script removes them recursively.
3. **Reproducibility check.** If the seed-4242 reference files exist in `data/full/`, the script regenerates them in memory and compares every value (formatted as the web app formats it). It stops on any mismatch.
4. **Generate.** For each case and seed:
   ```json
   POST /api/v1/gen/generations/
   {
     "name": "bearing-mc case_a seed1001",
     "description": "<from the config>",
     "seed": 1001,
     "config": { "...config, cleaned..." },
     "destination": { "type": "inline" }
   }
   ```
   The client honours `Retry-After` on HTTP 429, retries transient 5xx errors, skips files that already exist, writes through a temporary file, and records every run in `data/full/mc/manifest.json`.
5. **QA.** `tools/qa_bearing.py <config> <csv>` rebuilds the deterministic part of the config, subtracts it, and checks that the remainder behaves like the promised noise and, for Cases B and C, that the fault signature has the configured rate, frequency and amplitude.

## Results of the run behind the article

| Metric | Value |
|---|---|
| Series generated | 90 (30 seeds x 3 cases), 163,841 rows each |
| Seed-4242 reproduction | Identical to the web-app export (all values, Cases A and B) |
| QA gate | All series passed |
| Time per generation | about 1.8 s for 163,841 points inline (measured on the first 60 series) |
| Output | about 6.9 MB per CSV |

## Usage

```bash
export TSERIZ_API_KEY=...
python tools/generate_batch.py --reference-set   # 3 series, seed 4242
python tools/generate_batch.py                   # 90 series
python tools/generate_batch.py --qa-only         # re-check existing files without calling the API
```

Plan limits apply (generations per month, points per generation, requests per minute). Validation calls are free, so iterate on a config with `validate/` before generating.
