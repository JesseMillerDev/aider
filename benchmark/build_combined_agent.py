"""Overlay current source on the already pinned Node/Pyright runtime."""

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile

from benchmark.ab_support import digest, read_json
from benchmark.run_baseline import ROOT, atomic_json, command, image_id

BUILD_RECORD = ROOT / "benchmark/.cache/combined-build.json"


def preflight(image):
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
                "benchmark.combined_preflight",
            ]
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="HEAD")
    parser.add_argument("--image")
    args = parser.parse_args(argv)
    baseline = read_json(ROOT / "benchmark/baselines/gpt-6.1-sol-subscription-50.json")
    base = baseline["configuration"]
    lsp = read_json(ROOT / "benchmark/.cache/lsp-build.json")
    if image_id(base["agent_image"]) != base["agent_image_id"]:
        raise ValueError("Frozen control image changed")
    if (
        lsp["base_image_id"] != base["agent_image_id"]
        or image_id(lsp["agent_image"]) != lsp["agent_image_id"]
    ):
        raise ValueError("Pinned language-server runtime changed")
    revision = command(["git", "rev-parse", args.source])
    image = args.image or "better-aider-combined:" + revision[:12]
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", args.source, "aider"], cwd=ROOT
    )
    names = ["agent_worker.py", "pager_preflight.py", "combined_preflight.py"]
    files = {name: ROOT / "benchmark" / name for name in names}
    dockerfile = ROOT / "benchmark/runner/Dockerfile.pager"
    record = {
        "source_revision": revision,
        "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "worker_sha256": hashlib.sha256(files["agent_worker.py"].read_bytes()).hexdigest(),
        "preflight_sha256": hashlib.sha256(files["combined_preflight.py"].read_bytes()).hexdigest(),
        "pager_preflight_sha256": (
            hashlib.sha256(files["pager_preflight.py"].read_bytes()).hexdigest()
        ),
        "dockerfile_sha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest(),
        "base_image_id": base["agent_image_id"],
        "language_server_base_image_id": lsp["agent_image_id"],
        "agent_image": image,
    }
    context = ROOT / "benchmark/.cache" / ("combined-build-" + digest(record)[:16])
    context.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(context, filter="data")
    (context / "benchmark").mkdir(exist_ok=True)
    for path in files.values():
        shutil.copyfile(path, context / "benchmark" / path.name)
    (context / "benchmark/__init__.py").touch()
    shutil.copyfile(dockerfile, context / "Dockerfile")
    command(
        [
            "docker",
            "build",
            "--build-arg",
            "BASE_IMAGE=" + lsp["agent_image"],
            "--tag",
            image,
            str(context),
        ],
        context / "build.log",
        timeout=1200,
    )
    for name, path in files.items():
        actual = command(
            ["docker", "run", "--rm", "--entrypoint", "sha256sum", image, "/app/benchmark/" + name]
        ).split()[0]
        if actual != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("Combined worker/fixture fingerprint mismatch: " + name)
    freeze = command(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-m", "pip", "freeze"]
    )
    control = command(
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
    if freeze.splitlines() != control.splitlines():
        raise ValueError("Python dependencies differ from frozen control")
    (context / "environment.txt").write_text(freeze + "\n", encoding="utf-8")
    record["node_version"] = command(
        ["docker", "run", "--rm", "--entrypoint", "node", image, "--version"]
    )
    record["pyright_version"] = command(
        ["docker", "run", "--rm", "--entrypoint", "pyright", image, "--version"]
    )
    record["agent_image_id"] = image_id(image)
    record["preflight"] = preflight(record["agent_image_id"])
    record["build_directory"] = str(context.relative_to(ROOT))
    atomic_json(BUILD_RECORD, record)
    print(
        f"Built {image} at {record['agent_image_id']}; all-feature offline preflight passed",
        flush=True,
    )


if __name__ == "__main__":
    main()
