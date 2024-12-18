FROM tiangolo/nginx-rtmp

# Install system dependencies including FFmpeg
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    python3-pip \
    git \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install WebRTC dependencies
RUN apt-get update && apt-get install -y \
    libsrtp2-dev \
    libnice-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements.txt first to leverage Docker cache
COPY requirements.txt /usr/local/requirements.txt

# Install Python requirements
RUN python3 -m pip install -r /usr/local/requirements.txt

# Set up config file and create directories
COPY nginx.conf /etc/nginx/nginx.conf
RUN mkdir -p /tmp/recordings && \
    chmod -R 777 /tmp/recordings

# Expose ports
EXPOSE 1935-1940 8080-8085 8000

# Copy application files
COPY *.html /usr/local/nginx/html/
COPY server.py /usr/local/
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Use the startup script as the entry point
ENTRYPOINT ["/bin/bash", "/start.sh"]
