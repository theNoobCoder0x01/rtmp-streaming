# rtmp-streaming
Streaming to a local network from OBS using NGINX-RTMP

```
docker build . -t streaming
docker run -p 1935:1935 -p 8080:8080 -p 8000:8000 streaming
```
