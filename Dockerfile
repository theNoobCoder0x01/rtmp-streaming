FROM tiangolo/nginx-rtmp

# Install system dependencies first
RUN apt-get update && apt-get -y install python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements.txt first to leverage Docker cache
COPY requirements.txt /usr/local/requirements.txt

# Install Python requirements
RUN python3 -m pip install -r /usr/local/requirements.txt

# Set up config file and create directories
COPY nginx.conf /etc/nginx/nginx.conf
RUN mkdir -p /tmp/hls{,1,2,3,4,5} && \
    chmod -R 777 /tmp/hls{,1,2,3,4,5}

# Expose ports
EXPOSE 1935-1940 8080-8085 8000

# Copy application files
COPY *.html /usr/local/nginx/html/
COPY public/ /usr/local/nginx/html/public/
# COPY player.html /usr/local/nginx/html/
COPY server.py /usr/local/
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Use the startup script as the entry point
ENTRYPOINT ["/bin/bash", "/start.sh"]
