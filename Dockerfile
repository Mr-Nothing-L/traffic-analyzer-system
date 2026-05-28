# ============================================
# Traffic Agent - Docker Environment (CPU)
# Python 3.11 + YOLOv8 + VLM APIs
# ============================================

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/data

# Install system dependencies (使用阿里云镜像加速)
RUN sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|http://deb.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    wget \
    vim \
    ffmpeg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /data

# Copy and install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# Install torch CPU + YOLOv8 (PyTorch CPU wheels are smaller)
RUN pip install --no-cache-dir --default-timeout=300 \
    torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    ultralytics>=8.0.0 \
    lapx \
    filterpy \
    scipy

# Ensure tools directory is available in PYTHONPATH
ENV PYTHONPATH=/data:/data/traffic_analyzer/tools:${PYTHONPATH}

# Default: keep container running
CMD ["tail", "-f", "/dev/null"]
