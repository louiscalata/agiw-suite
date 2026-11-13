"""AGIW Suite · Inference Monitor, Windows edition.

A loopback observer for the PC's llama-server lanes, GPUs, memory, route queue,
NVIDIA PAIR's presence and the Mac's own share report. Linux share reports remain
peer diagnostics only and never become machine rows. It serves the same dashboard
as the Mac edition with ``host: "windows"``. Standard library only; nothing here
loads, unloads or restarts a model.
"""

VERSION = "1.1.0"
SERVICE = "agiw-win-observer"


def _prefer_shared_root_over_site_packages():
    """Make the shared root modules (machine_state, fleet_state ...) win over an installed copy.

    The shared root is appended to sys.path elsewhere so it never shadows windows/tests with the
    root test_*.py modules. Appended, it comes after site-packages, so a stale installed
    machine_state.py (the root suite's package tests leave one behind) was imported instead of
    the checkout's and lacked newer constants. Placing the root just before the first
    site-packages entry keeps the test packages first and the checkout's modules ahead of any
    installed copy.
    """
    import sys
    from pathlib import Path

    here = Path(__file__).resolve()
    for root in (here.parents[1], here.parents[2]):
        if (root / "machine_state.py").is_file():
            entry = str(root)
            if entry in sys.path:
                break
            for index, item in enumerate(sys.path):
                if "site-packages" in item or "dist-packages" in item:
                    sys.path.insert(index, entry)
                    break
            else:
                sys.path.append(entry)
            break


_prefer_shared_root_over_site_packages()
