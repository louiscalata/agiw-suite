"""AGIW Suite · Inference Monitor, Windows edition.

A loopback observer for the PC's llama-server lanes, GPUs, memory, route queue and
its LAN peer (the Mac). It serves the same dashboard as the Mac edition with
``host: "windows"``. Standard library only; nothing here loads, unloads or
restarts a model.
"""

VERSION = "0.1.0"
SERVICE = "agiw-win-observer"
