FROM python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"
COPY aider /app/aider
COPY benchmark /app/benchmark
ENV PYTHONPATH=/app HOME=/tmp/home PYTHONHASHSEED=0 AIDER_ANALYTICS=false
USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["python", "-u", "/app/benchmark/agent_worker.py", "/artifacts/job.json"]
