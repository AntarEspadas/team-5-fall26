#!/bin/bash
# Sync events every hour; see .platform/sync/install_sync_timer.sh.
exec bash "$(dirname "$0")/../../sync/install_sync_timer.sh"
