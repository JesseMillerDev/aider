"""Build an offline worker from the pinned control source, never the working Aider tree."""

import argparse
import hashlib
import io
import shutil
import subprocess
import tarfile

from benchmark.run_baseline import AGENT_IMAGE, CONTROL, ROOT, command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=AGENT_IMAGE)
    args = parser.parse_args(argv)
    worker = ROOT / "benchmark/agent_worker.py"
    fingerprint = hashlib.sha256(worker.read_bytes()).hexdigest()
    context = (
        ROOT
        / "benchmark/.cache"
        / ("agent-build-" + CONTROL[:8] + "-" + fingerprint[:8])
    )
    context.mkdir(parents=True, exist_ok=True)
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", CONTROL, "aider", "requirements.txt"],
        cwd=ROOT,
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(context, filter="data")
    (context / "benchmark").mkdir(exist_ok=True)
    shutil.copyfile(worker, context / "benchmark/agent_worker.py")
    (context / "benchmark/__init__.py").touch()
    shutil.copyfile(ROOT / "benchmark/runner/Dockerfile", context / "Dockerfile")
    command(
        ["docker", "build", "--tag", args.image, str(context)], context / "build.log"
    )
    actual = command(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sha256sum",
            args.image,
            "/app/benchmark/agent_worker.py",
        ]
    ).split()[0]
    if actual != fingerprint:
        raise RuntimeError("Built image does not contain the expected worker source")
    freeze = command(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            args.image,
            "-m",
            "pip",
            "freeze",
        ]
    )
    (context / "environment.txt").write_text(freeze + "\n", encoding="utf-8")
    print(f"Built {args.image} from control {CONTROL}; worker {fingerprint}")
    print(f"Build log and dependency snapshot: {context}")


if __name__ == "__main__":
    main()
