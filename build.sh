#!/usr/bin/env bash
set -euo pipefail

IMAGE="map-panel:latest"

echo "Building $IMAGE..."
docker build -t "$IMAGE" .

echo ""
echo "Build complete."
echo ""
echo "  Start:   docker compose up -d"
echo "  Logs:    docker compose logs -f"
echo "  Stop:    docker compose down"
echo ""
echo "On first start, login with admin / admin and change your password."
