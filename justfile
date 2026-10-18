export PATH := env("HOME") / ".local/bin:" + env("HOME") / ".cargo/bin:" + env("PATH")

default:
    @just --list

setup:
    uv sync --all-groups

sync:
    uv sync --frozen --no-dev

run:
    PYTHONPATH=src uv run --frozen --no-dev python -m plant_monitor

install:
    bash service/install.sh

start:
    sudo systemctl enable --now plant-monitor.service

restart:
    sudo systemctl restart plant-monitor.service

status:
    systemctl status plant-monitor.service

logs:
    journalctl -u plant-monitor.service -n 50 --no-pager

deploy tag:
    bash service/deploy.sh '{{ tag }}'
