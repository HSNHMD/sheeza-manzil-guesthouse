# Domain / DNS / SSL runbook

> **Last updated:** 2026-05-03.
> Two domains are in active use; everything is Certbot-managed via
> nginx vhosts on a single VPS.

## Domains in scope

| Hostname | Purpose | Where it points | nginx vhost | TLS |
|---|---|---|---|---|
| `sheezamanzil.com` | **Production** PMS | A record → 187.127.112.36 (Hostinger VPS) | `/etc/nginx/sites-enabled/sheeza-manzil` | Certbot (Let's Encrypt) |
| `www.sheezamanzil.com` | Production alias | CNAME → `sheezamanzil.com` | Same vhost (`server_name sheezamanzil.com www.sheezamanzil.com`) | Same cert (SAN) |
| `staging.husn.cloud` | **Staging** PMS | A record → 187.127.112.36 | `/etc/nginx/sites-enabled/guesthouse-staging` | Certbot (Let's Encrypt) |

> If the user adds a new domain (demo / per-tenant), follow the
> "Adding a new vhost" section below. Don't reuse the staging vhost.

## Quick checks

```bash
# DNS resolution (from a developer machine):
dig +short sheezamanzil.com
dig +short staging.husn.cloud

# Both should return 187.127.112.36.

# Live cert (will tell you the issuer + expiry):
echo | openssl s_client -showcerts -servername sheezamanzil.com \
        -connect sheezamanzil.com:443 2>/dev/null \
     | openssl x509 -noout -dates -subject

echo | openssl s_client -showcerts -servername staging.husn.cloud \
        -connect staging.husn.cloud:443 2>/dev/null \
     | openssl x509 -noout -dates -subject
```

## Cert renewal

Certbot auto-renews via the system cron / systemd timer that ships
with the `certbot` package. Manual renewal (only if auto-renew has
been failing):

```bash
ssh root@187.127.112.36
sudo certbot renew --dry-run                  # safe to run anytime
sudo certbot renew                            # actual renewal
sudo systemctl reload nginx                   # pick up new certs
```

If `certbot renew --dry-run` fails:
- Check DNS is still pointing at the VPS.
- Check port 80 is open (Let's Encrypt uses HTTP-01 challenge by
  default).
- Check the nginx vhost still has the `.well-known/acme-challenge/`
  location block (Certbot inserts it; don't strip it during edits).

## nginx vhost shape (current)

Both vhosts follow the same pattern (anonymized):

```nginx
server {
    listen 80;
    server_name <hostname>;
    # Certbot inserts a .well-known/acme-challenge block here
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;             # managed by Certbot
    server_name <hostname>;

    ssl_certificate     /etc/letsencrypt/live/<hostname>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<hostname>/privkey.pem;

    client_max_body_size 32m;

    location / {
        proxy_pass         http://127.0.0.1:<port>;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Production gunicorn binds to one port; staging to a different port.
Confirm via `systemctl cat sheeza.service` / `systemctl cat
guesthouse-staging.service` and grep for the `--bind` argument.

## Adding a new vhost

```bash
ssh root@187.127.112.36

# 1. DNS: add A record pointing the new hostname at the VPS IP.
#    Wait until `dig +short <hostname>` returns the right answer.

# 2. Copy the staging vhost as a template:
sudo cp /etc/nginx/sites-available/guesthouse-staging \
        /etc/nginx/sites-available/<new-hostname>
sudo vim /etc/nginx/sites-available/<new-hostname>
#  - swap server_name
#  - swap proxy_pass port (use a fresh port; pick one not in use)

# 3. Symlink + reload:
sudo ln -s /etc/nginx/sites-available/<new-hostname> \
           /etc/nginx/sites-enabled/<new-hostname>
sudo nginx -t                       # syntax check
sudo systemctl reload nginx

# 4. Issue a cert:
sudo certbot --nginx -d <new-hostname>

# 5. Verify:
curl -sk -o /dev/null -w "%{http_code}\n" https://<new-hostname>/
```

## DNS provider notes

The user owns multiple domains; the relevant entries in the public DNS
config (Cloudflare or registrar) need to be updated by the domain
owner. Don't rotate DNS without coordinating.

## Which domains are prod vs staging

- `sheezamanzil.com` / `www.sheezamanzil.com` — **production only**.
  Never point staging at these.
- `staging.husn.cloud` — **staging only**. Never point production at
  this.
- Any new `*.husn.cloud` subdomain is fair game for staging / demo /
  preview environments. The pattern is `<purpose>.husn.cloud`.

## Recovering from "site is down"

```bash
# 1. Service alive?
sudo systemctl is-active sheeza.service guesthouse-staging.service

# 2. nginx alive?
sudo systemctl is-active nginx
sudo nginx -t

# 3. Cert valid (not expired)?
echo | openssl s_client -servername <hostname> -connect <hostname>:443 \
       2>/dev/null | openssl x509 -noout -dates

# 4. DNS pointing at the right IP?
dig +short <hostname>
```

If the cert expired and `certbot renew` won't fix it, see the Certbot
section above for `--dry-run` debugging.
