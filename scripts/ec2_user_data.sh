#!/bin/bash
# Runs once, at first boot. Installs Docker and the compose plugin and prepares
# the application directory. Nothing application specific happens here: the
# bundle is copied in afterwards, so a rebuild never means a new instance.
set -eux

dnf update -y
dnf install -y docker tar gzip

systemctl enable --now docker
usermod -aG docker ec2-user

install -d -m 0755 /usr/local/lib/docker/cli-plugins
curl -fsSL \
  https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

install -d -o ec2-user -g ec2-user /opt/copilot

# A marker the deploy script can wait on, so it does not race the bootstrap.
touch /opt/copilot/.bootstrap-complete
