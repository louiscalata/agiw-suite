"""One owner-supplied ephemeral CPU-only extraction smoke. Never touches existing servers."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import once_common as c  # noqa: E402

TAG = "tiny-extraction"
PROGRAM = "tiny-extraction.mjs"


def main():
    args = c.parse_args(PROGRAM, __doc__)
    try:
        model = c.model_metadata(args.model_path, args.allow_tiny_model)
        server = c.resolve_server(args.llama_server)
    except c.Refused as exc:
        c.emit(f"BLOCKED: {exc}")
        return 2
    record = {"startedUTC": c.started_utc(), "model": model, "inferenceAttempted": False}
    port = c.free_port()
    proc, log = None, None
    try:
        proc, log = c.start_server(server, model["path"], port, TAG)
        record["pid"] = proc.pid
        c.wait_health(port, proc, record, args.rss_cap_mb)
        record["health"] = c.get_health(port)
        record["marker"] = c.timestamped_marker(TAG, record["startedUTC"])
        record["inferenceAttempted"] = True
        record["pilot"] = c.execute(
            "live", f"http://127.0.0.1:{port}/v1/chat/completions", "agiw-owner-smoke", program=PROGRAM
        )
    except c.RUN_ERRORS as exc:
        record["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        c.reap(proc, record)
        if log is not None:
            log.close()
        record["finishedUTC"] = c.started_utc()
        c.write_result(TAG, record)
    return 0 if record.get("pilot", {}).get("report", {}).get("outcome") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
