"""Build an idle-only paging worker on the unchanged control Python runtime."""

import argparse
import hashlib
import io
import shutil
import subprocess
import tarfile

from benchmark.ab_support import digest, read_json
from benchmark.run_baseline import ROOT, atomic_json, command, image_id

SOURCE = "40f82be43"
IMAGE = "better-aider-pager:40f82be"
BUILD_RECORD = ROOT / "benchmark/.cache/pager-build.json"


def preflight(image):
    import json

    return json.loads(
        command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:rw,exec,mode=1777",
                "--entrypoint",
                "python",
                image,
                "-m",
                "benchmark.pager_preflight",
            ]
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--image", default=IMAGE)
    args = parser.parse_args(argv)
    baseline = read_json(ROOT / "benchmark/baselines/gpt-6.1-sol-subscription-50.json")
    base = baseline["configuration"]
    if image_id(base["agent_image"]) != base["agent_image_id"]:
        raise ValueError("Control runtime image differs from the frozen baseline")
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", args.source, "aider"], cwd=ROOT
    )
    worker = ROOT / "benchmark/agent_worker.py"
    fixture = ROOT / "benchmark/pager_preflight.py"
    dockerfile = ROOT / "benchmark/runner/Dockerfile.pager"
    record = {
        "source_revision": command(["git", "rev-parse", args.source]),
        "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
        "preflight_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "dockerfile_sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest(),
        "base_image_id": base["agent_image_id"],
        "agent_image": args.image,
    }
    context = ROOT / "benchmark/.cache" / ("pager-build-" + digest(record)[:16])
    context.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(context, filter="data")
    (context / "benchmark").mkdir(exist_ok=True)
    for path in (worker, fixture):
        shutil.copyfile(path, context / "benchmark" / path.name)
    (context / "benchmark/__init__.py").touch()
    shutil.copyfile(dockerfile, context / "Dockerfile")
    command(
        [
            "docker",
            "build",
            "--build-arg",
            "BASE_IMAGE=" + base["agent_image"],
            "--tag",
            args.image,
            str(context),
        ],
        context / "build.log",
        timeout=1200,
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
    if actual != record["worker_sha256"]:
        raise ValueError("Treatment worker fingerprint mismatch")
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
    control_freeze = command(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            base["agent_image"],
            "-m",
            "pip",
            "freeze",
        ]
    )
    if freeze.splitlines() != control_freeze.splitlines():
        raise ValueError(
            "Treatment Python dependencies differ from the control runtime"
        )
    (context / "environment.txt").write_text(freeze + "\n", encoding="utf-8")
    record["agent_image_id"] = image_id(args.image)
    record["build_directory"] = str(context.relative_to(ROOT))
    record["preflight"] = preflight(record["agent_image_id"])
    atomic_json(BUILD_RECORD, record)
    print(
        f"Built {args.image} at {record['agent_image_id']}; offline pager preflight passed",
        flush=True,
    )


if __name__ == "__main__":
    main()
