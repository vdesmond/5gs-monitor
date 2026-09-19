FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 iputils-ping iperf3 procps \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENTRYPOINT ["fivegs-monitor"]
