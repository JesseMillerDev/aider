"""Build a pinned LSP treatment overlay while retaining control Python dependencies."""

import argparse
import hashlib
import io
import shutil
import subprocess
import tarfile

from benchmark.ab_support import digest, read_json
from benchmark.run_baseline import ROOT, atomic_json, command, image_id

SOURCE = "3432f71f0f398013c00d87ac6d0c7359891b57f9"
IMAGE = "better-aider-lsp:3432f71"
BUILD_RECORD = ROOT / "benchmark/.cache/lsp-build.json"


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
    dockerfile = ROOT / "benchmark/runner/Dockerfile.lsp"
    record = {
        "source_revision": command(["git", "rev-parse", args.source]),
        "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
        "dockerfile_sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest(),
        "base_image_id": base["agent_image_id"],
        "agent_image": args.image,
        "node_image": "node@sha256:b21fe589dfbe5cc39365d0544b9be3f1f33f55f3c86c87a76ff65a02f8f5848e",
        "node_version": "v22.20.0",
        "pyright_version": "1.1.414",
    }
    context = ROOT / "benchmark/.cache" / ("lsp-build-" + digest(record)[:16])
    context.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(context, filter="data")
    (context / "benchmark").mkdir(exist_ok=True)
    shutil.copyfile(worker, context / "benchmark/agent_worker.py")
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
    command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "pyright",
            args.image,
            "--version",
        ]
    )
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
    record["agent_image_id"] = image_id(args.image)
    record["build_directory"] = str(context.relative_to(ROOT))
    atomic_json(BUILD_RECORD, record)
    print(f"Built {args.image} at {record['agent_image_id']}", flush=True)


if __name__ == "__main__":
    main()
