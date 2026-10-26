"""AGIW Suite · Inference Monitor, Windows edition.

A loopback observer for the PC's llama-server lanes, GPUs, memory, route queue,
NVIDIA PAIR's presence and the other machines' share reports (the Mac, and the Linux
node relayed by the Mac). It serves the same dashboard as the Mac edition with
``host: "windows"``. Standard library only; nothing here loads, unloads or
restarts a model.
"""

VERSION = "0.2.0"
SERVICE = "agiw-win-observer"
