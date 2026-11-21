# Spectrum Hub: web UI + phone link + MCP server + llama.cpp (CUDA) in one image.
#
# CUDA 12.4 on purpose: it still supports Pascal cards (GTX 10xx, compute 6.1), which CUDA 13
# dropped, and it runs on NVIDIA driver 550+ (what TrueNAS SCALE 24.10/25.04 ship).
# CUDA_ARCHS covers Pascal through Ada (10xx, 16xx/20xx, 30xx, 40xx); 50xx works through PTX.

ARG CUDA_VERSION=12.4.1
ARG LLAMA_CPP_TAG=b12102

# ---- llama-server ---------------------------------------------------------------------------
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04 AS llama
ARG LLAMA_CPP_TAG
ARG CUDA_ARCHS="61;75;86;89"
RUN apt-get update && apt-get install -y --no-install-recommends git cmake build-essential ca-certificates libssl-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${LLAMA_CPP_TAG} https://github.com/ggml-org/llama.cpp /src
WORKDIR /src
# GGML_NATIVE=OFF: don't tune the CPU code for the build machine, the NAS CPU may differ.
# No web UI (the hub has its own), static so it's a single binary.
RUN cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHS}" \
        -DGGML_NATIVE=OFF -DBUILD_SHARED_LIBS=OFF \
        -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON \
        -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined \
    && cmake --build build --config Release -j"$(nproc)" --target llama-server \
    && strip build/bin/llama-server

# ---- runtime --------------------------------------------------------------------------------
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    DATA_DIR=/data MODELS_DIR=/models LLAMA_SERVER_BIN=/usr/local/bin/llama-server
RUN apt-get update && apt-get install -y --no-install-recommends python3.11 python3.11-venv libgomp1 ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && python3.11 -m venv /venv
ENV PATH=/venv/bin:$PATH
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY --from=llama /src/build/bin/llama-server /usr/local/bin/llama-server
COPY app /app/app
COPY web /app/web
WORKDIR /app
VOLUME ["/data", "/models"]
EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fs http://127.0.0.1:8780/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8780", "--proxy-headers", "--forwarded-allow-ips", "*"]
