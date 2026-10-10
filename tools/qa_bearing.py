#!/usr/bin/env python3
"""QA gate: checks a tseriz-generated CSV against the config that produced it.
Usage: python qa_bearing.py config.json data.csv [--json]
Idea: rebuild the deterministic part of the config (oscillations + impulse trains), subtract it,
and check that what is left behaves like the noise the config promises. Exit code 1 on any FAIL."""
import json, sys
import numpy as np, pandas as pd
from scipy.signal import butter, sosfiltfilt, hilbert, welch, lfilter

def impulse_unit_peak(tau, wn, z):
    a = z * wn; wd = wn * np.sqrt(1 - z * z); tp = np.arctan2(wd, a) / wd
    return np.exp(-a * tau) * np.sin(wd * tau) / (np.exp(-a * tp) * np.sin(wd * tp))

def ramp(t, r):
    return np.interp(t, [r["start_seconds"], r["stop_seconds"]], [1.0, r["end_scale"]])

def onsets_of(tr, end):
    stop = end if tr.get("stop_seconds") is None else min(tr["stop_seconds"], end)
    n = int(np.floor((stop - tr["onset_seconds"]) * tr["repeat_hz"] + 1e-9)) + 1
    return tr["onset_seconds"] + np.arange(n) / tr["repeat_hz"]

def expected_train(tr, t):
    dt = t[1] - t[0]; on = onsets_of(tr, t[-1]); w = np.full(len(on), float(tr["amplitude"]))
    if tr.get("ramp"): w *= ramp(on, tr["ramp"])
    idx = np.rint(on / dt).astype(int); x = np.zeros(len(t)); np.add.at(x, idx, w)
    wn = 2 * np.pi * tr["natural_frequency_hz"]; z = tr["damping_ratio"]; a = z * wn
    h1 = float(impulse_unit_peak(np.array([dt]), wn, z)[0])
    return lfilter([0, h1], [1, -2 * np.exp(-a * dt) * np.cos(wn * np.sqrt(1 - z * z) * dt), np.exp(-2 * a * dt)], x), on

LIM = 0.20

def run(cfg, df):
    ch = cfg["channels"][0]; fs = cfg["sampling_frequency_hz"]; res = []
    def chk(name, ok, detail): res.append((name, "PASS" if ok else "FAIL", detail))
    n = int(round(cfg["duration_seconds"] * fs)) + 1
    col = [c for c in df.columns if c != "timestamp"][0]; x = df[col].to_numpy(float); t = np.arange(n) / fs
    # G0 structure
    chk("G0 rows", len(x) == n, f"{len(x)} rows, expected {n}")
    if len(x) != n: return res
    chk("G0 finite", bool(np.isfinite(x).all()), "no NaN/inf" if np.isfinite(x).all() else "NaN/inf present")
    ts = pd.to_datetime(df["timestamp"], format="ISO8601"); el = (ts - ts.iloc[0]).dt.total_seconds().to_numpy()
    err = np.max(np.abs(el - t)); chk("G0 timestamps", err < 1 / fs, f"max deviation from k/fs = {err*1e6:.2f} us = {err*fs:.2f} sample ({err/cfg['duration_seconds']*1e6:.1f} ppm clock drift); analysis should use the sample index")
    # deterministic part
    det = np.zeros(n); trains = []
    for o in ch.get("oscillations", []):
        f = o.get("frequency_hz") or 1 / o["period_seconds"]; det += o["amplitude"] * np.sin(2 * np.pi * f * t + o.get("phase", 0))
    for tr in ch.get("transients", []):
        if tr.get("repeat_hz"):
            y, on = expected_train(tr, t); det += y; trains.append((tr, on))
    if ch.get("trend_slope"): det += np.arange(n) * ch["trend_slope"]
    chk("G1 no trend", not ch.get("trend_slope"), f"trend_slope={ch.get('trend_slope')} (a per-sample slope is the bug that produced the 8,200 ramp)")
    r = x - det
    # G2 noise profile per second vs config
    # Noise is checked above 250 Hz, where many independent bins make the std stable (pink noise
    # is dominated by a few low-frequency bins, so its raw segment std varies a lot between seeds).
    FC = 250.0; hp = butter(4, FC, "hp", fs=fs, output="sos"); rh = sosfiltfilt(hp, r)
    def frac(c):  # share of a component's variance above FC
        return 1.0 if c["color"] == "white" else np.log(fs / 2 / FC) / np.log(fs / 2 / (fs / n))
    def exp_var(tc):
        v = np.zeros_like(tc)
        for c in ch["noise_components"]:
            sc = ramp(tc, c["ramp"]) if c.get("ramp") else 1.0; v += (c["amplitude"] * sc) ** 2 * frac(c)
        return v
    D = cfg["duration_seconds"]; worst = 0.0
    for a_, b_ in [(0, D / 2), (D / 2, D), (D - 2, D)]:
        m = (t >= a_) & (t < b_); worst = max(worst, abs(rh[m].std() / np.sqrt(exp_var(t[m]).mean()) - 1))
    chk("G2 noise above 250 Hz vs config", worst < LIM, f"worst segment (0-5, 5-10, 8-10 s) deviates {worst*100:.1f}% from promised (limit {LIM*100:.0f}%)")
    # G3 shaft lines
    for o in ch.get("oscillations", []):
        f = o.get("frequency_hz") or 1 / o["period_seconds"]; z = np.exp(-2j * np.pi * f * t); amp = 2 * abs(np.mean(r * z + det * 0 * z) + np.mean(det * z)) 
        chk(f"G3 line {f:g} Hz", abs(amp / o["amplitude"] - 1) < 0.05, f"{amp:.4f} g vs {o['amplitude']} g")
    # G4 pink spectrum on healthy residual (second half-time excluded if ramped)
    pk = [c for c in ch["noise_components"] if c["color"] == "pink"]
    if pk:
        seg = r[t < min([tr["onset_seconds"] for tr, _ in trains] + [min([c["ramp"]["start_seconds"] for c in ch["noise_components"] if c.get("ramp")] + [cfg["duration_seconds"]])])]
        f, P = welch(seg, fs, nperseg=4096); k = (f > 20) & (f < 200)
        sl = np.polyfit(np.log10(f[k]), np.log10(P[k]), 1)[0]
        chk("G4 pink slope 20-200 Hz", -1.25 < sl < -0.75, f"PSD slope {sl:.2f} (pink ~ -1)")
    # G5 fault
    ft = [(tr, on) for tr, on in trains]
    sos = butter(4, [400, 800], "bp", fs=fs, output="sos"); env = np.abs(hilbert(sosfiltfilt(sos, x)))
    env = env - env.mean()
    if ft:
        tr, on = ft[0]; rate = tr["repeat_hz"]
        pre = t < tr["onset_seconds"]; post = t >= tr["onset_seconds"]
        def line(e, m):
            e = e[m] - e[m].mean(); F = np.abs(np.fft.rfft(e * np.hanning(len(e)))); fr = np.fft.rfftfreq(len(e), 1 / fs)
            k = np.abs(fr - rate) < 1.5; bg = (np.abs(fr - rate) > 5) & (np.abs(fr - rate) < 40)
            return F[k].max() / np.median(F[bg])
        a, b = line(env, pre), line(env, post)
        chk("G5 envelope line absent before onset", a < 5, f"line/background = {a:.1f}")
        chk("G5 envelope line present after onset", b > 10, f"line/background = {b:.1f} at {rate:g} Hz")
        # burst amplitude vs time (peak of residual-matched band in each period)
        idx = np.rint(on * fs).astype(int); idx = idx[idx < n - 8]
        pkv = np.array([np.max(np.abs(x[i:i + 8] - 0)) for i in idx])
        bandx = sosfiltfilt(butter(4, [450, 800], "bp", fs=fs, output="sos"), x)
        w = int(fs / rate)
        pkb = np.array([np.max(np.abs(bandx[i:i + w])) for i in idx]); tt = on[:len(pkb)]; sl = np.polyfit(tt, pkb, 1)[0]
        ex = sosfiltfilt(butter(4, [450, 800], "bp", fs=fs, output="sos"), expected_train(tr, t)[0])
        pke = np.array([np.max(np.abs(ex[i:i + w])) for i in idx]); sle = np.polyfit(tt, pke, 1)[0]
        if not tr.get("ramp"):
            # constant-amplitude fault: no growth expected; compare mean burst level with the noiseless model and check flatness
            h = len(pkb) // 2; flat = pkb[h:].mean() / pkb[:h].mean()
            chk("G5 burst level matches model (no growth)", 0.7 < pkb.mean() / pke.mean() < 1.3 and 0.85 < flat < 1.15,
                f"mean band-peak {pkb.mean():.3f} g, noiseless model {pke.mean():.3f} g, ratio {pkb.mean()/pke.mean():.2f}; late/early {flat:.2f}")
        else:
          chk("G5 burst growth matches model", sle > 0 and 0.7 < sl / sle < 1.3, f"band-peak slope {sl:.3f} g/s, noiseless model {sle:.3f} g/s, ratio {sl/sle:.2f}")
    else:
        pre = t < cfg["duration_seconds"] / 2; post = ~pre; rate = 162
        def line(e, m):
            e = e[m] - e[m].mean(); F = np.abs(np.fft.rfft(e * np.hanning(len(e)))); fr = np.fft.rfftfreq(len(e), 1 / fs)
            k = np.abs(fr - rate) < 1.5; bg = (np.abs(fr - rate) > 5) & (np.abs(fr - rate) < 40)
            return F[k].max() / np.median(F[bg])
        v = line(env, post)
        chk("G5 no spurious 162 Hz line in a no-impulse config", v < 5, f"line/background = {v:.1f}")
    return res

if __name__ == "__main__":
    cfg = json.load(open(sys.argv[1])); df = pd.read_csv(sys.argv[2]); res = run(cfg, df)
    for n_, s, d in res: print(f"{s:4s}  {n_:42s} {d}")
    bad = [r for r in res if r[1] == "FAIL"]; print(f"\n{len(res)-len(bad)}/{len(res)} checks passed"); sys.exit(1 if bad else 0)
