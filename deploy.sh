#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(realpath -- "$0")")"

if [[ $EUID -ne 0 ]]; then
    exec sudo bash ./deploy.sh "$@"
fi

source /etc/os-release
if [[ ${ID:-} != ubuntu ]] || [[ $(ps -p 1 -o comm=) != systemd ]]; then
    echo "Для установки нужна Ubuntu с systemd." >&2
    exit 1
fi

docker=(/usr/bin/docker --host unix:///var/run/docker.sock)
if ! "${docker[@]}" info >/dev/null 2>&1 || ! "${docker[@]}" compose version >/dev/null 2>&1; then
    if [[ -x /usr/bin/dockerd ]]; then
        systemctl enable --now docker.service
    fi
fi
if ! "${docker[@]}" info >/dev/null 2>&1 || ! "${docker[@]}" compose version >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates curl
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL --retry 3 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    if ! apt-cache policy docker-ce | grep 'download.docker.com' >/dev/null; then
        cat > /etc/apt/sources.list.d/docker.sources <<APT
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
APT
    fi
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker.service
fi

exec 9>.deploy.lock
flock 9
if [[ ! -e .env ]]; then
    umask 077
    secret_file=$(mktemp .env.XXXXXX)
    trap 'rm -f -- "$secret_file"' EXIT
    password=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
    printf 'YOURTUBE_DB_PASSWORD=%s\nYOURTUBE_PORT=8000\n' "$password" > "$secret_file"
    mv -T -- "$secret_file" .env
fi

compose=("${docker[@]}" compose --env-file .env)
if ! "${compose[@]}" up -d --build --wait --wait-timeout 120; then
    "${compose[@]}" logs --tail 30 >&2
    exit 1
fi
endpoint=$("${compose[@]}" port app 8000)
printf 'YourTube: http://localhost:%s/\nSwagger: http://localhost:%s/docs\n' "${endpoint##*:}" "${endpoint##*:}"

