"""Run after installing DrainTracer: python python_session.py worker.py [args...]."""

from __future__ import annotations

import argparse

from draintracer.api import Session, SessionOptions
from draintracer.report_contract import validate_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor a target using only supported Python imports.")
    parser.add_argument("target")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    options = SessionOptions(sample_interval_ms=250, windows_seconds=(2,),
                             timeout_seconds=5, fail_on_inconclusive=True)
    session = Session.launch(arguments.target, args=arguments.arguments, options=options)
    try:
        with session:
            result = session.wait()
    except KeyboardInterrupt:
        result = session.wait(0)
    if result is None:
        raise RuntimeError("cleanup completion was not confirmed")
    validate_report(session.report())
    print(f"{result.reason}: measurement={result.measurement}, cleanup={result.cleanup}")
    if result.exports:
        print(f"JSON: {result.exports.json_path}; HTML: {result.exports.html_path}")
    for error in result.errors:
        print(f"Monitor error: {error}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
