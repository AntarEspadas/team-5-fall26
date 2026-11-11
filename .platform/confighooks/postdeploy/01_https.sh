#!/bin/bash
# Serve HTTPS with a Let's Encrypt certificate; see .platform/https/enable_https.sh.
exec bash "$(dirname "$0")/../../https/enable_https.sh"
