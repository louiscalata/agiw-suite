"""AGIW Suite · Inference Monitor, Windows edition.

A loopback observer for the PC's llama-server lanes, GPUs, memory, route queue,
NVIDIA PAIR's presence and the Mac's own share report. Linux share reports remain
peer diagnostics only and never become machine rows. It serves the same dashboard
as the Mac edition with ``host: "windows"``. Standard library only; nothing here
loads, unloads or restarts a model.
"""

VERSION = "1.1.0"
SERVICE = "agiw-win-observer"
