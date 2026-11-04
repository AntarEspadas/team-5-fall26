#!/bin/bash
# Configuration-only deployments (such as `eb setenv`) skip container_commands,
# so rebuild the static files Elastic Beanstalk serves at /static.
set -euo pipefail
cd "$(dirname "$0")/../../.."
source /var/app/venv/*/bin/activate
python3 manage.py collectstatic --noinput
