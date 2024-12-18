#!/bin/bash

# Start nginx in the background
nginx

# Start the Python server
python3 /usr/local/server.py &

# Keep the container running
tail -f /dev/null
