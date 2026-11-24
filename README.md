# Sonos Art

Headless Sonos artwork display for a 64x64 HUB75 RGB panel.

The service monitors the Dining Room Sonos speaker, displays album artwork,
shows short track/volume banners, and exposes health/configuration through
Home Assistant MQTT Discovery and a local HTTP endpoint.

## Security

Runtime credentials and configuration are intentionally excluded from Git:

- `/etc/sonos-art.env` contains MQTT credentials on the Pi.
- `/opt/sonos-art/config.json` contains runtime display settings.

Do not commit either file or place passwords in the repository.

## Deployment

From the Mac:

```sh
SONOS_ART_HOST=nick@sonos-art.local bash /Users/nicholaswinans/dev/sonos-art/deploy.sh
```

The deployment script installs the Python MQTT dependency, updates the systemd
service, and restarts the display service.

## Pi configuration

Copy `sonos-art.env.example` to `/etc/sonos-art.env` on the Pi and set the
Sonos room address, MQTT broker, MQTT credentials, and optional device URL.
The environment file is intentionally not tracked by Git.
