"""Small analysis helpers shared by the quickstart notebook (numpy / scipy / pandas).

The full-length notebook (notebooks/bearing_degradation.ipynb) defines the same functions inline.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import butter, hilbert, sosfiltfilt

FS = 8192          # sampling frequency [Hz]
BPFI = 162.0       # inner-race fault rate [Hz] (5.4 x 30 Hz shaft)
BAND = (400, 800)  # band around the 600 Hz resonance used for the envelope


def band_env(x, band=BAND, fs=FS):
    """Zero-phase band-pass filtered signal and its Hilbert envelope."""
    sos = butter(4, band, "bp", fs=fs, output="sos")
    y = sosfiltfilt(sos, x)
    return y, np.abs(hilbert(y))


def fit_lines(x, t, freqs=(30, 60, 90)):
    """Least-squares fit of sine + cosine at each shaft harmonic. Returns the fitted signal and the amplitudes."""
    A = np.column_stack([f(2 * np.pi * fr * t) for fr in freqs for f in (np.sin, np.cos)])
    c, *_ = np.linalg.lstsq(A, x, rcond=None)
    return A @ c, [float(np.hypot(c[2 * i], c[2 * i + 1])) for i in range(len(freqs))]


def envelope_spectrum(x, fs=FS):
    """Amplitude spectrum of the 400-800 Hz envelope. Returns (frequencies, amplitude)."""
    _, e = band_env(x, fs=fs)
    e = e - e.mean()
    w = np.hanning(len(e))
    return np.fft.rfftfreq(len(e), 1 / fs), 2 * np.abs(np.fft.rfft(e * w)) / w.sum()


def env_line(x, fs=FS, f0=BPFI, bw=1.5, bg=(5, 40)):
    """Envelope-spectrum line-to-background ratio at f0 for a segment x."""
    _, e = band_env(x, fs=fs)
    e = e - e.mean()
    n = len(e)
    F = np.abs(np.fft.rfft(e * np.hanning(n)))
    fr = np.fft.rfftfreq(n, 1 / fs)
    near = np.abs(fr - f0) < bw
    back = (np.abs(fr - f0) > bg[0]) & (np.abs(fr - f0) < bg[1])
    return F[near].max() / np.median(F[back])


def window_features(x, t, win=1.0, step=0.25, fs=FS):
    """Sliding-window indicators: overall RMS, RMS in the resonance band, and the envelope line at the fault rate.
    The band signal is computed once on the whole record. `t_end` is the time at which a window is complete."""
    yb, _ = band_env(x, fs=fs)
    n, s = int(win * fs), int(step * fs)
    rows = []
    for i in range(0, len(x) - n + 1, s):
        rows.append(dict(t=t[i] + win / 2, t_end=t[i] + win, rms=x[i:i + n].std(),
                         band_rms=yb[i:i + n].std(), bpfi_line=env_line(x[i:i + n], fs=fs)))
    return pd.DataFrame(rows)


def alarm_time(tt, v, thr, k=3):
    """First time at which v exceeds thr in k consecutive windows (time of the k-th), or NaN."""
    run = 0
    for ti, vi in zip(tt, v):
        run = run + 1 if vi > thr else 0
        if run >= k:
            return ti
    return float("nan")
