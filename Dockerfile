FROM tiangolo/nginx-rtmp

# Set up config file
COPY nginx.conf /etc/nginx/nginx.conf

# Create the /tmp/hls directory
RUN mkdir -p /tmp/hls && chmod -R 777 /tmp/hls

EXPOSE 1935
EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]

