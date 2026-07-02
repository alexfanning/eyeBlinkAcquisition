# eyeBlinkAcquisition

Spike2 scripts and Python code for controlling multi-channel data acquisition during eye-blink conditioning experiments, including synchronized control of air puff delivery, LED/tone stimuli, Neuropixels recording, and high-speed camera triggering.

## Overview

This repository contains the acquisition control system for eye-blink conditioning experiments with simultaneous Neuropixels electrophysiology. The system coordinates: conditioned stimulus (CS) delivery via LED or tone, unconditioned stimulus (US) delivery via air puff, Neuropixels probe recording via NIDQ, and high-speed camera triggering for eyelid closure video capture — all with precise inter-system timing via TTL pulses.

The system was built from scratch to enable simultaneous behavioral conditioning and large-scale neural recording, enabling alignment of single-unit and LFP responses to conditioning trial events.

## Contents

### Spike2 Acquisition Scripts (PLSQL/s2s)
- `eyeblink.s2s` — Primary Spike2 sequencer script for eye-blink conditioning; controls CS/US timing, inter-trial intervals, and TTL output for multi-system synchronization
- `eyeblinkV2.s2s` — Updated version with refined timing parameters and additional output channels
- `ParametersUniversal.s2s` — Shared parameter configuration file; defines CS duration, US duration, inter-trial interval, and number of trials

### Spike2 Sequencer Files
- `Sequencer_Eyeblink.pls` — Pulse sequencer file for precise hardware-timed stimulus delivery
- `Sequencer_EyeblinkV2.pls` — Updated sequencer with refined pulse timing

### Python Camera Control
- `capture_triggered.py` — Python script for hardware-triggered high-speed camera capture; listens for TTL trigger from Spike2 and captures frames at each CS onset for eyelid closure tracking
- `capture_triggeredV2.py` — Updated camera capture script with improved buffer management and timestamp recording

### Documentation
- `runningProgramCode.txt` — Step-by-step protocol for initializing and running the acquisition system across all hardware components

## Hardware Requirements

- CED Power1401 or similar (Spike2 interface)
- Neuropixels probe + IMEC NIDQ acquisition board
- High-speed camera (e.g., Basler, FLIR)
- Air puff solenoid and delivery system
- LED or speaker for CS delivery

## Requirements

- Spike2 (CED)
- Python 3.7+ (opencv-python, numpy)
