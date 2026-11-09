"""The feature job's entry point: JOB=spark (default) or JOB=python (docs/spark.md).

The same pod runs either, so the throughput run compares them on the same resources.
"""

import os
import sys


def main():
    job = os.getenv("JOB", "spark")
    if job == "python":
        from stream import python_writer
        sys.argv = [sys.argv[0], "--from", os.getenv("STARTING_OFFSETS", "earliest"),
                    "--metrics-port", os.getenv("METRICS_PORT", "8000")]
        python_writer.main()
    elif job == "spark":
        from stream import spark_job
        spark_job.main()
    else:
        raise SystemExit(f"JOB must be spark or python, not {job!r}")


if __name__ == "__main__":
    main()
