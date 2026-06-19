#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "    KALSHI TRADING BOT VPS INSTALLATION SCRIPT"
echo "=========================================================="

if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root or with sudo privileges." >&2
  exit 1
fi

echo "Updating apt repositories..."
apt-get update -y

if ! [ -x "$(command -v docker)" ]; then
  echo "Docker not found. Installing Docker Engine..."
  apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release
  mkdir -p /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io
else
  echo "Docker Engine is already installed."
fi

if ! [ -x "$(command -v docker-compose)" ]; then
  echo "Docker Compose not found. Installing Docker Compose..."
  apt-get install -y docker-compose-plugin
  ln -s /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose || true
else
  echo "Docker Compose is already installed."
fi

echo "Creating persistent database and logs directories..."
mkdir -p data logs

chmod 750 data
chmod 750 logs
chmod 750 data/dlq 2>/dev/null || true

if [ ! -f .env ]; then
  echo "Warning: .env configuration file not found."
  if [ -f .env.example ]; then
    echo "Creating a new .env file from .env.example..."
    cp .env.example .env
    chmod 600 .env
    echo "=========================================================="
    echo "ACTION REQUIRED: Update the .env file with your actual"
    echo "API keys and parameters before spinning up the container."
    echo "=========================================================="
  else
    touch .env
    chmod 600 .env
  fi
else
  chmod 600 .env
fi

if [ -f kalshi_private.pem ]; then
  chmod 600 kalshi_private.pem
  echo "Private key permissions restricted to owner."
fi

echo "Building and tagging image..."
TAG="${1:-latest}"
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

docker-compose build
docker tag kalshi-bot:latest "kalshi-bot:${TAG}"
docker tag kalshi-bot:latest "kalshi-bot:${GIT_SHA}"

echo "Spinning up the trading bot container daemon (tag: ${TAG}, sha: ${GIT_SHA})..."
docker-compose up -d

echo "=========================================================="
echo "DEPLOYMENT INITIATED SUCCESSFULLY!"
echo "Image tags: latest, ${TAG}, ${GIT_SHA}"
echo "To monitor status logs, run: docker-compose logs -f"
echo "=========================================================="
