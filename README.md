# rtmp-streaming
Streaming to a local network from OBS using NGINX-RTMP

```
docker compose up --build -d
```

Use `rtmp://<host-ip>:1935/live` as the OBS server URL and your stream key as
the stream name. The web player and HLS output are available on port `8080`.
The API is exposed on port `8000`.

Stop the service with:

```
docker compose down
```
