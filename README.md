# Sonos Art

Headless Sonos artwork display for a 64x64 HUB75 RGB panel.

The service discovers Sonos speakers on the local network, displays album artwork
for the selected speaker,
shows short track/volume banners, and exposes health/configuration through
Home Assistant MQTT Discovery and a local HTTP endpoint.

TV/HDMI playback is detected from Sonos's `x-sonos-htastream:` source URI and
uses a TV glyph when no album artwork is available.

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
entity; changing it moves the event subscriptions to the new speaker without
restarting the Python process.

The MQTT discovery also creates a `Sonos Art Display` light entity. It provides
on/off and brightness controls that can be exposed by Home Assistant's HomeKit
Bridge integration. The tuning controls and diagnostics are categorized as
configuration/diagnostic entities so HomeKit does not expose them by default;
they remain available in Home Assistant.

## HomeKit

In Home Assistant, add the **HomeKit Bridge** integration and expose only the
`Sonos Art TV` media player, then pair the bridge in Apple Home using accessory
mode. It appears as one Television with power and source selection. The older
`Sonos Art Display` light remains useful in Home Assistant for brightness, but
should not also be exposed to HomeKit.

Source selection can command `TV / HDMI` and `Line In` when the selected Sonos
speaker supports them. `Sonos Music` is currently report-only because Sonos
does not provide a generic way to restore the previous music queue after an
external input is selected.

## Pi configuration

Copy `sonos-art.env.example` to `/etc/sonos-art.env` on the Pi and set the
Sonos room address, MQTT broker, MQTT credentials, and optional device URL.
The environment file is intentionally not tracked by Git.
