# CPU is the default target. The GPU target uses Paddle's CUDA/cuDNN image,
# rather than merely exposing NVIDIA devices to a CPU image.
FROM python:3.11-slim-bookworm AS cpu
FROM paddlepaddle/paddle:3.2.2-gpu-cuda12.6-cudnn9.5 AS gpu

FROM cpu AS cpu-final
ENV PADDLEOCR_DEVICE=cpu
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    "paddlepaddle==3.2.2" \
    "paddleocr==3.2.0" \
    "aiohttp==3.12.15"
COPY server.py /service/server.py
WORKDIR /service
EXPOSE 12345
CMD ["python", "server.py"]

FROM gpu AS gpu-final
ENV PADDLEOCR_DEVICE=gpu:0
RUN pip install --no-cache-dir "paddleocr==3.2.0" "aiohttp==3.12.15"
COPY server.py /service/server.py
WORKDIR /service
EXPOSE 12345
CMD ["python", "server.py"]
