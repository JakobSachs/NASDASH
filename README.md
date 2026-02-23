# NAS Dashboard

Minimal dashboard for ZFS, SMART, and system monitoring.

## Setup (Debian/Ubuntu)

```bash
# Allow smartctl without password
echo '$USER ALL=(ALL) NOPASSWD: /usr/sbin/smartctl' | sudo tee /etc/sudoers.d/smartctl

# Create systemd service
sudo tee /etc/systemd/system/nasdash.service << EOF
[Unit]
Description=NAS Dashboard
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=/usr/bin/python3 server.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now nasdash
```

Dashboard at http://localhost:8080

## Commands

```bash
sudo systemctl status nasdash   # status
sudo systemctl restart nasdash  # restart after edits
sudo journalctl -u nasdash -f   # logs
```
