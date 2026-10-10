# Prompts for the tseriz AI agent

Paste a prompt into the tseriz AI agent to generate the series. The JSON files in `configs/` are the authoritative definitions; the prompts describe the same scenarios in plain English. Each prompt sets the AR coefficient of both noise components to 0 so that the noise matches the configs.

Case B and Case C differ only in the impact growth. All four use seed 4242 and share the same healthy first 10 s.

## Baseline (healthy)

Config: `configs/baseline.json`

> Simulate 20 seconds of single-channel vibration data in g, sampled at 8192 Hz, with seed 4242, for a motor running at 1800 RPM. Include sinusoidal shaft components at 30 Hz (0.5 g), 60 Hz (0.2 g) and 90 Hz (0.1 g), plus pink mechanical noise with standard deviation 0.05 g and white electronic noise with standard deviation 0.02 g. Set the AR coefficient of both noise components to 0.

## Case A: naive noise degradation

Config: `configs/case_a.json`

> Simulate 20 seconds of single-channel vibration data in g, sampled at 8192 Hz, with seed 4242, for a motor running at 1800 RPM. Include sinusoidal shaft components at 30 Hz (0.5 g), 60 Hz (0.2 g) and 90 Hz (0.1 g), plus pink mechanical noise with standard deviation 0.05 g and white electronic noise with standard deviation 0.02 g. Set the AR coefficient of both noise components to 0. Between t=10 s and t=20 s, ramp the pink noise amplitude linearly to 1.5 times its starting value and the white noise amplitude linearly to 9 times. Add no transients.

## Case B: inner-race fault with growing impacts

Config: `configs/case_b.json`

> Simulate 20 seconds of single-channel vibration data in g, sampled at 8192 Hz, with seed 4242, for a motor running at 1800 RPM. Include sinusoidal shaft components at 30 Hz (0.5 g), 60 Hz (0.2 g) and 90 Hz (0.1 g), plus pink mechanical noise with standard deviation 0.05 g and white electronic noise with standard deviation 0.02 g. Set the AR coefficient of both noise components to 0. From t=10 s, add an inner-race bearing fault: an underdamped second-order impulse response with natural frequency 600 Hz, damping ratio 0.1 and peak amplitude 0.1 g, repeating 162 times per second. Ramp its amplitude linearly to 5 times (400% growth) at t=20 s. Over the same window, ramp the pink noise linearly to 1.5 times its starting value.

## Case C: weak inner-race fault, constant amplitude

Config: `configs/case_c.json`

> Simulate 20 seconds of single-channel vibration data in g, sampled at 8192 Hz, with seed 4242, for a motor running at 1800 RPM. Include sinusoidal shaft components at 30 Hz (0.5 g), 60 Hz (0.2 g) and 90 Hz (0.1 g), plus pink mechanical noise with standard deviation 0.05 g and white electronic noise with standard deviation 0.02 g. Set the AR coefficient of both noise components to 0. From t=10 s, add an inner-race bearing fault: an underdamped second-order impulse response with natural frequency 600 Hz, damping ratio 0.1 and constant peak amplitude 0.1 g (no growth), repeating 162 times per second. Over t=10 s to t=20 s, ramp the pink noise linearly to 1.5 times its starting value.
