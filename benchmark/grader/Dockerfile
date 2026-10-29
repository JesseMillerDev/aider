# Linux runner for the original SWE-bench Lite dataset schema.
FROM python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /grading
ENTRYPOINT ["python", "-m", "swebench.harness.run_evaluation"]
