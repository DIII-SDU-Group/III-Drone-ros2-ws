#!/bin/bash

# Create docker containers
docker compose -f docker-compose.yml --profile "*" up --build --no-start