FROM tiangolo/nginx-rtmp

# Set up config file
COPY nginx.conf /etc/nginx/nginx.conf

# Create the /tmp/hls directory
RUN mkdir -p /tmp/hls && chmod -R 777 /tmp/hls

# Expose ports
EXPOSE 1935
EXPOSE 1936
EXPOSE 1937
EXPOSE 1938
EXPOSE 1939
EXPOSE 1940
EXPOSE 8080
EXPOSE 8081
EXPOSE 8082
EXPOSE 8083
EXPOSE 8084
EXPOSE 8085

# Copy index.html to the container
COPY index.html /usr/local/nginx/html/index.html
COPY index_1.html /usr/local/nginx/html/index_1.html
COPY index_2.html /usr/local/nginx/html/index_2.html
COPY index_3.html /usr/local/nginx/html/index_3.html
COPY index_4.html /usr/local/nginx/html/index_4.html
COPY index_5.html /usr/local/nginx/html/index_5.html

CMD ["nginx", "-g", "daemon off;"]

