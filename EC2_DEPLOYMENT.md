# EC2 Deployment Guide

This is the fastest way to put the chatbot on a public EC2 instance and share an open link with your team.

## What makes it publicly accessible?

Yes, this can be an open link.

For teammates in India or anywhere else to access it, the EC2 instance needs all of the following:

- A **public IPv4 address** or, preferably, an **Elastic IP**
- A **security group** allowing inbound `80/tcp` from `0.0.0.0/0`
- The app bound to `0.0.0.0` inside the instance
- No company VPN or IP allowlist blocking access

If those are in place, the shareable URL is:

```text
http://<elastic-ip>/
```

## Recommended EC2 setup

- AMI: Ubuntu 22.04 LTS
- Instance type: `t3.small` or larger
- Storage: 16 GB+
- Security group:
  - `22/tcp` from your admin IP only
  - `80/tcp` from `0.0.0.0/0`
  - `443/tcp` from `0.0.0.0/0` if you add HTTPS later

## 1. Install Docker on EC2

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin git
sudo usermod -aG docker "$USER"
newgrp docker
```

## 2. Copy the repo onto the instance

```bash
git clone <repo-url>
cd gaia-chatbot
```

## 3. Create the environment file

```bash
cp .env.example .env
```

Populate `.env` with at least:

```env
OPENAI_API_KEY=sk-...
ADMIN_PASSWORD=<strong-password>
HOST=0.0.0.0
PORT=8000
```

Do not set `DB_PATH` or `STORAGE_DIR` for the EC2 Docker flow unless you want to override the defaults injected by the deploy script.

## 4. Deploy the container

```bash
chmod +x deploy/ec2/deploy.sh
./deploy/ec2/deploy.sh
```

The script will:

- Build the Docker image
- Start the app container
- Persist SQLite data and uploads under `.deploy-data/`
- Publish the app on port `80`

## 5. Verify from the browser

Open:

```text
http://<elastic-ip>/
```

Health check:

```text
http://<elastic-ip>/health
```

## Updating after code changes

```bash
git pull
./deploy/ec2/deploy.sh
```

## Next production-hardening steps

- Add a domain name and point it to the Elastic IP
- Put Nginx or an ALB in front for HTTPS
- Add an ACM certificate if moving behind a load balancer
- Move SQLite/image storage off-instance if you need stronger durability
