# Sonos Art

Headless Sonos artwork display for a 64x64 HUB75 RGB panel.

The service discovers Sonos speakers on the local network, displays album artwork
for the selected speaker,
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

On first start, the service discovers the available Sonos speakers locally. The
speaker selected by `SONOS_ROOM_IP` is saved as the initial selection. After
MQTT discovery is enabled, Home Assistant exposes a `Sonos Speaker` select
entity; changing it restarts the service cleanly so subscriptions move to the
new speaker.

The MQTT discovery also creates a `Sonos Art Display` light entity. It provides
on/off and brightness controls that can be exposed by Home Assistant's HomeKit
Bridge integration, along with the display-enabled switch.

## HomeKit

In Home Assistant, add the **HomeKit Bridge** integration, include the
`Sonos Art Display` light and/or `Display Enabled` switch, then pair the bridge
in Apple Home. The select and diagnostic sensors are intentionally left out of
HomeKit because HomeKit has no useful native representation for them.

## Pi configuration

Copy `sonos-art.env.example` to `/etc/sonos-art.env` on the Pi and set the
Sonos room address, MQTT broker, MQTT credentials, and optional device URL.
The environment file is intentionally not tracked by Git.
