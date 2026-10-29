"""Deterministic stdio peer for transport tests; never executes repository code."""

import argparse
import json
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--mode", default="normal")
parser.add_argument("--log", required=True)
args = parser.parse_args()
root = None
documents = {}


def send(message):
    data = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode("utf-8")
    frame = f"Content-Length: {len(data)}\r\n\r\n".encode() + data
    # Fragment both framing and multi-byte UTF-8 payloads.
    for offset in range(0, len(frame), 7):
        sys.stdout.buffer.write(frame[offset : offset + 7])
        sys.stdout.buffer.flush()


def diagnostic(uri):
    version = documents[uri][0]
    send(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "version": version - 1 if args.mode == "stale_diagnostics" else version,
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "severity": 1,
                        "message": f"fake error 😀 version {version}",
                        "code": "fake",
                    }
                ],
            },
        }
    )


while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    with open(args.log, "a", encoding="utf-8") as log:
        log.write(json.dumps(message, ensure_ascii=False) + "\n")
    method = message.get("method")
    if method == "initialize":
        if args.mode == "slow_init":
            time.sleep(5)
        if args.mode == "malformed":
            sys.stdout.buffer.write(b"Content-Length: nope\r\n\r\n")
            sys.stdout.buffer.flush()
            continue
        root = message["params"]["rootUri"]
        send(
            {
                "method": "workspace/configuration",
                "id": "config",
                "params": {"items": [{"section": "python"}]},
            }
        )
        send(
            {
                "method": "workspace/applyEdit",
                "id": "edit",
                "params": {"edit": {"changes": {}}},
            }
        )
        send(
            {
                "id": message["id"],
                "result": {
                    "capabilities": {
                        "positionEncoding": (
                            "utf-8" if args.mode == "utf8_only" else "utf-16"
                        ),
                        "definitionProvider": True,
                        "referencesProvider": True,
                        "textDocumentSync": 1 if args.mode == "full_sync" else 2,
                    }
                },
            }
        )
    elif method == "initialized" and args.mode == "stop_reading":
        time.sleep(10)
    elif method == "textDocument/didOpen":
        document = message["params"]["textDocument"]
        documents[document["uri"]] = (document["version"], document["text"])
        diagnostic(document["uri"])
    elif method == "textDocument/didChange":
        document = message["params"]["textDocument"]
        change = message["params"]["contentChanges"][0]
        documents[document["uri"]] = (document["version"], change["text"])
        diagnostic(document["uri"])
    elif method in ("textDocument/definition", "textDocument/references"):
        if args.mode == "die":
            sys.exit(1)
        if args.mode == "slow_definition":
            time.sleep(5)
        span = {
            "start": {"line": 1, "character": 2},
            "end": {"line": 1, "character": 5},
        }
        result = [{"uri": root + "/target.py", "range": span}]
        if args.mode == "links":
            result = [
                {
                    "targetUri": root + "/target.py",
                    "targetRange": span,
                    "targetSelectionRange": span,
                }
            ]
        if args.mode == "empty":
            result = None
        send({"id": message["id"], "result": result})
    elif method == "shutdown":
        send({"id": message["id"], "result": None})
    elif method == "exit":
        break
