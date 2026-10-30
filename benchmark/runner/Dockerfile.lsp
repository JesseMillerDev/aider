ARG BASE_IMAGE=better-aider-baseline:8400741
FROM node@sha256:b21fe589dfbe5cc39365d0544b9be3f1f33f55f3c86c87a76ff65a02f8f5848e AS language_server
RUN npm install --prefix /opt/lsp pyright@1.1.414 --ignore-scripts --no-audit --no-fund

# Reuse the control's installed Python dependencies byte for byte.
FROM ${BASE_IMAGE}
USER root
COPY --from=language_server /usr/local/bin/node /usr/local/bin/node
COPY --from=language_server /opt/lsp/node_modules /opt/lsp/node_modules
ENV PATH="/opt/lsp/node_modules/.bin:${PATH}"
COPY aider /app/aider
COPY benchmark /app/benchmark
RUN node --version && pyright --version
USER 65532:65532
