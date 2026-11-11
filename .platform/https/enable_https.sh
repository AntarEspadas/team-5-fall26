#!/bin/bash
# Serve the site over HTTPS with a free Let's Encrypt certificate.
#
# Browsers only allow the microphone, which voice chat needs, on HTTPS pages.
# Elastic Beanstalk runs this script after every deployment (see the
# postdeploy hooks). It gets or renews a certificate for HTTPS_DOMAIN, serves
# the site on port 443, and redirects plain HTTP there. If any step fails, it
# says why in /var/log/eb-hooks.log and leaves the site on plain HTTP, so a
# deployment never fails because of HTTPS.
#
# The paths below can be overridden to try the script outside AWS.
set -uo pipefail

NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:-/etc/letsencrypt}"
WEBROOT="${WEBROOT:-/var/www/letsencrypt}"
CERTBOT="${CERTBOT:-/opt/certbot/bin/certbot}"
APP_URL="${APP_URL:-http://127.0.0.1:8000}"
STATIC_DIR="${STATIC_DIR:-/var/app/current/staticfiles}"

HTTPS_CONF="$NGINX_DIR/conf.d/https.conf"
REDIRECT_CONF="$NGINX_DIR/conf.d/elasticbeanstalk/https_redirect.conf"

log() {
    echo "[https] $*"
}

# Stop here and keep serving plain HTTP.
http_only() {
    log "$1; serving HTTP only."
    rm -f "$HTTPS_CONF" "$REDIRECT_CONF"
    nginx -t -q && nginx -s reload
    exit 0
}

domain="${HTTPS_DOMAIN:-}"
if [ -z "$domain" ] && [ -x /opt/elasticbeanstalk/bin/get-config ]; then
    domain="$(/opt/elasticbeanstalk/bin/get-config environment -k HTTPS_DOMAIN 2>/dev/null || true)"
fi
[ -n "$domain" ] || http_only "HTTPS_DOMAIN is not set"
[[ "$domain" =~ ^[A-Za-z0-9.-]+$ ]] || http_only "HTTPS_DOMAIN is not a host name"

if [ ! -x "$CERTBOT" ]; then
    log "installing certbot"
    venv="$(dirname "$(dirname "$CERTBOT")")"
    python="$(command -v python3.11 || command -v python3)"
    { "$python" -m venv "$venv" && "$venv/bin/pip" install --quiet --upgrade pip certbot; } \
        || http_only "could not install certbot"
fi

# Let's Encrypt fetches a file from $WEBROOT over plain HTTP to check that we
# control the domain; see .platform/nginx/conf.d/elasticbeanstalk/. The
# certificate is only replaced when it is close to expiring.
mkdir -p "$WEBROOT"
"$CERTBOT" certonly --webroot -w "$WEBROOT" -d "$domain" \
    --config-dir "$LETSENCRYPT_DIR" --non-interactive --agree-tos \
    --register-unsafely-without-email --keep-until-expiring \
    --deploy-hook "nginx -s reload" \
    || log "Let's Encrypt did not issue or renew a certificate for $domain"

# A certificate from an earlier deployment still works if renewing failed.
live="$LETSENCRYPT_DIR/live/$domain"
[ -s "$live/fullchain.pem" ] && [ -s "$live/privkey.pem" ] \
    || http_only "there is no certificate for $domain"
if command -v openssl >/dev/null \
    && ! openssl x509 -checkend 0 -noout -in "$live/fullchain.pem" >/dev/null; then
    http_only "the certificate for $domain has expired"
fi

cat > "$HTTPS_CONF" <<EOF
# Written by .platform/https/enable_https.sh after each deployment.
server {
    listen 443 ssl;
    server_name $domain;

    ssl_certificate     $live/fullchain.pem;
    ssl_certificate_key $live/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;

    client_max_body_size 10m;

    # Keep Elastic Beanstalk's access log and enhanced health log for HTTPS
    # requests too, without its HTTP redirect. The wildcard matches nothing
    # (and is not an error) when enhanced health reporting is turned off.
    access_log /var/log/nginx/access.log main;
    include $NGINX_DIR/conf.d/elasticbeanstalk/healthd*.conf;

    location /static/ {
        alias $STATIC_DIR/;
    }

    location / {
        proxy_pass         $APP_URL;
        proxy_http_version 1.1;
        proxy_set_header   Connection "";
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        # Django trusts this header in production (SECURE_PROXY_SSL_HEADER).
        proxy_set_header   X-Forwarded-Proto https;
    }
}
EOF

cat > "$REDIRECT_CONF" <<EOF
# Written by .platform/https/enable_https.sh: send plain HTTP to HTTPS, except
# Let's Encrypt's checks, which must stay on HTTP.
if (\$request_uri !~ "^/\.well-known/acme-challenge/") {
    return 301 https://$domain\$request_uri;
}
EOF

nginx -t -q || http_only "nginx rejected the HTTPS settings"
nginx -s reload || http_only "nginx did not reload"
log "serving https://$domain"

# Renew twice a day; certbot only replaces certificates near expiry, and its
# deploy hook reloads nginx afterwards.
if [ -d /run/systemd/system ]; then
    cat > /etc/systemd/system/certbot-renew.service <<EOF
[Unit]
Description=Renew the Let's Encrypt certificate

[Service]
Type=oneshot
ExecStart=$CERTBOT renew --quiet --config-dir $LETSENCRYPT_DIR
EOF
    cat > /etc/systemd/system/certbot-renew.timer <<EOF
[Unit]
Description=Renew the Let's Encrypt certificate twice a day

[Timer]
OnCalendar=*-*-* 03,15:17:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF
    { systemctl daemon-reload && systemctl enable --now certbot-renew.timer; } \
        || log "could not schedule certificate renewals"
fi
exit 0
