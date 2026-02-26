#!/usr/bin/env bash
set -euo pipefail

mode="tls"
engine="selectors"
host="127.0.0.1"
port="8080"
https_port="8443"
log_format="json"
cert_dir="certs"
cert_file=""
key_file=""
redirect_http="1"
drain_timeout="5"
dry_run="0"

if [[ "${1:-}" == "http" || "${1:-}" == "tls" ]]; then
  mode="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --engine)
      engine="${2:-}"
      shift 2
      ;;
    --host)
      host="${2:-}"
      shift 2
      ;;
    --port)
      port="${2:-}"
      shift 2
      ;;
    --https-port)
      https_port="${2:-}"
      shift 2
      ;;
    --log-format)
      log_format="${2:-}"
      shift 2
      ;;
    --cert-dir)
      cert_dir="${2:-}"
      shift 2
      ;;
    --cert-file)
      cert_file="${2:-}"
      shift 2
      ;;
    --key-file)
      key_file="${2:-}"
      shift 2
      ;;
    --drain-timeout)
      drain_timeout="${2:-}"
      shift 2
      ;;
    --no-redirect)
      redirect_http="0"
      shift
      ;;
    --dry-run)
      dry_run="1"
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/start_server.sh [http|tls] [options]

Modes:
  tls   Start HTTPS server with redirect (default)
  http  Start plain HTTP server

Options:
  --engine <threadpool|selectors>   Default: selectors
  --host <host>                     Default: 127.0.0.1
  --port <port>                     HTTP port (or server port in http mode). Default: 8080
  --https-port <port>               HTTPS port in tls mode. Default: 8443
  --log-format <plain|json>         Default: json
  --cert-dir <dir>                  Auto cert dir in tls mode. Default: certs
  --cert-file <path>                Optional explicit cert file path
  --key-file <path>                 Optional explicit key file path
  --drain-timeout <seconds>         Default: 5
  --no-redirect                     Disable HTTP->HTTPS redirect in tls mode
  --dry-run                         Print resolved command without executing
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage"
      exit 1
      ;;
  esac
done

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if [[ -z "$cert_file" ]]; then
  cert_file="$cert_dir/dev-cert.pem"
fi
if [[ -z "$key_file" ]]; then
  key_file="$cert_dir/dev-key.pem"
fi

cmd=(python3 server.py --engine "$engine" --host "$host" --port "$port" --log-format "$log_format" --drain-timeout "$drain_timeout")

if [[ "$mode" == "tls" ]]; then
  if [[ "$dry_run" != "1" ]]; then
    if [[ ! -f "$cert_file" || ! -f "$key_file" ]]; then
      echo "TLS certs not found. Generating in $cert_dir ..."
      tools/generate_dev_cert.sh "$cert_dir"
    fi
  fi

  cmd+=(--enable-tls --cert-file "$cert_file" --key-file "$key_file" --https-port "$https_port")
  if [[ "$redirect_http" == "1" ]]; then
    cmd+=(--redirect-http)
  else
    cmd+=(--no-redirect-http)
  fi
fi

if [[ "$dry_run" == "1" ]]; then
  printf 'Resolved command: '
  printf '%q ' "${cmd[@]}"
  printf '\n'
  exit 0
fi

printf 'Starting server with command: '
printf '%q ' "${cmd[@]}"
printf '\n'
exec "${cmd[@]}"
