#!/bin/bash

# Create the systemd service file
cat << 'EOF' | sudo tee /etc/systemd/system/lerobot.service
[Unit]
Description=LeRobot Teleoperation Service
After=network.target

[Service]
Type=simple
User=daniel
# Ensure all necessary paths are included
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/daniel/miniconda3/bin:/home/daniel/miniconda3/condabin"
Environment="CONDA_ROOT=/home/daniel/miniconda3"
WorkingDirectory=/home/daniel/github/lerobot

# Use the full conda activation command
ExecStart=/bin/bash -c '\
    eval "$(/home/daniel/miniconda3/bin/conda shell.bash hook)" && \
    conda activate lerobot && \
    python lerobot/scripts/control_robot.py teleoperate \
    --robot-path lerobot/configs/robot/so100.yaml \
    --robot-overrides "~cameras" \
    --display-cameras 0'

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd daemon to recognize new service
sudo systemctl daemon-reload

# Stop the existing service if it's running
sudo systemctl stop lerobot.service

# Enable the service to start on boot
sudo systemctl enable lerobot.service

# Start the service immediately
sudo systemctl start lerobot.service

# Check status
systemctl status lerobot.service
