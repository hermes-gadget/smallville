# Smallville VPS deployment

Target: `hermes-agent@217.154.42.46:32720`, with the repository at `/home/hermes-agent/smallville`. The app runs as plain user processes under systemd; Python 3.9 is supplied by `uv` because the VPS system Python is 3.14 and Django 2.2 is required.

## Initial setup

Copy the `deploy/` directory to the VPS (or run the script from a checkout), then run:

```sh
bash deploy/setup_vps.sh
```

Before starting services, ensure `~/.opencode-go.key` already exists. The setup script checks it but never creates or prints it. The script clones or fast-forwards `~/smallville`, installs Python 3.9 and dependencies, adds `smallville.justarobot.uk` to the Django host allow-list, and enables the two user units.

On the home VM, install `vm-embedding-tunnel.service` at `~/.config/systemd/user/`, then run `systemctl --user daemon-reload && systemctl --user enable --now vm-embedding-tunnel.service`. Do not install or run that tunnel on the VPS.

## Start order and verification

Start the tunnel first, then the backend and frontend:

```sh
systemctl --user start smallville-reverie.service
systemctl --user start smallville-django.service
journalctl --user -u smallville-reverie.service -n 100 --no-pager
journalctl --user -u smallville-django.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8000/
curl -fsS http://smallville.justarobot.uk/
```

Check the tunnel from the home VM with `journalctl --user -u vm-embedding-tunnel.service -n 100 --no-pager`. If Reverie cannot reach embeddings, inspect that journal before restarting the app.

## Updates

```sh
cd ~/smallville
git pull --ff-only
uv pip install -r requirements.txt --python ~/smallville/.venv/bin/python
systemctl --user restart smallville-reverie.service smallville-django.service
```

Then repeat the journal and curl checks. If unit definitions change, copy them from `deploy/`, run `systemctl --user daemon-reload`, and restart both services.
