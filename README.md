# RELAY WebSocket server

Plaintext WebSocket relay for FoloToy AI Passport / RELAY devices. Path is `/ws`. There is no TLS.

Devices fill `HOST:PORT` (example: `203.0.113.10:7300`). Do not use `ws://` or HTTPS. `--allow-mac` is a filter, not authentication.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python relay/server.py \
  --host 0.0.0.0 \
  --port 7300 \
  --group sidecar-home \
  --allow-mac <12-hex-mac> \
  --allow-mac <12-hex-mac>
```

Default `--host` is `127.0.0.1` and will not accept devices. Use `0.0.0.0` on a remote host and open TCP 7300. `--group` must match the device GROUP exactly. MAC values are 12 lowercase hex digits, no colons.

Do not put TLS/nginx `wss` in front unless the firmware is changed to speak TLS. Current devices only speak plaintext WebSocket.

## Check

```bash
.venv/bin/python relay/check.py
```
