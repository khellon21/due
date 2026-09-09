# Assignment due notifier

Watches the Blackboard calendar feed and pushes escalating notifications to
your phone until you mark each assignment done.

## Notification schedule

For something due Thursday at 11:59 PM:

| When | What |
|---|---|
| 8:00 PM on the latest free evening before the due date | "Due tomorrow" |
| 6:59 PM Thursday | 5 hours left |
| 9:59 PM Thursday | 2 hours left |
| 10:59 PM Thursday | 1 hour left |
| 11:29 PM Thursday onward | every 30 minutes, continuing past the deadline |
| midnight - 8 AM | quiet |

A work shift blocks the whole day, so the heads-up moves back to the last free
evening. Tapping **Done** stops everything for that item.

## Phone setup

1. Install the **ntfy** app (iOS / Android / F-Droid).
2. Subscribe to the topic name you put in `config.json` as `ntfy_topic`.
   The topic name is the only secret, so make it long and random.

## VM setup (Ubuntu 24.04)

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <this repo> ~/due && cd ~/due
python3 -m venv .venv && .venv/bin/pip install icalendar flask waitress
cp config.example.json config.json
```

Generate the two secrets and put them in `config.json`:

```bash
python3 -c "import secrets; print('ntfy_topic:', secrets.token_urlsafe(24)); print('web_token:', secrets.token_hex(16))"
```

Then set `base_url` to `http://<your VM public IP>:8080` and fill in
`work_schedule`. Verify:

```bash
.venv/bin/python test_due.py
.venv/bin/python due.py tick
```

## First run: clear the backlog BEFORE enabling cron

Anything already past its deadline is treated as still outstanding, so the
first tick will notify you about all of it at once — at the time of writing
that is 12 assignments, at urgent priority, repeating every 30 minutes until
you mark them done.

So do this in order:

1. Start the web page (next section) and open it on your phone.
2. Tap **Done** on everything you have already handed in.
3. Only then enable cron, below.

If you skip this you will get a wall of notifications and then have to clear
them from the page anyway.

## Cron

```bash
crontab -e
```

```
*/5 * * * * cd ~/due && .venv/bin/python due.py tick >> tick.log 2>&1
```

## Web page as a service

```bash
sudo tee /etc/systemd/system/due-web.service >/dev/null <<'EOF'
[Unit]
Description=Assignment due notifier web page
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/due
ExecStart=/home/ubuntu/due/.venv/bin/waitress-serve --host 0.0.0.0 --port 8080 web:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now due-web
```

## Firewall — both layers

This is where Oracle VMs usually appear dead from the internet. **Both** of
these are required.

1. **Oracle Cloud console:** Networking > Virtual Cloud Networks > your VCN >
   Security Lists > Default > Add Ingress Rule. Source `0.0.0.0/0`, IP
   protocol TCP, destination port `8080`.

2. **On the VM.** Ubuntu images on Oracle ship iptables rules that drop
   everything, and they survive reboots, so the rule must be inserted and
   persisted:

```bash
sudo iptables -I INPUT 6 -p tcp --dport 8080 -j ACCEPT
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

Confirm from your phone: `http://<vm-ip>:8080/t/<web_token>/`

## Log rotation

```bash
sudo tee /etc/logrotate.d/due >/dev/null <<'EOF'
/home/ubuntu/due/tick.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
EOF
```

## Changing your work schedule

Edit it on the web page. Days with a shift are work days; days with an empty
list are free. Changes take effect on the next tick, within five minutes.

## Upgrading to HTTPS

The page is plain HTTP protected by the token in the URL. To encrypt it,
install Tailscale on the VM and your phone, set `base_url` to the VM's
Tailscale name, and remove the two firewall rules above. No domain needed.
