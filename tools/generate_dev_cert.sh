#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:-certs}"
cert_file="${output_dir}/dev-cert.pem"
key_file="${output_dir}/dev-key.pem"

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl not found; please install openssl to generate dev certificates."
  exit 1
fi

mkdir -p "$output_dir"

openssl req \
  -x509 \
  -newkey rsa:2048 \
  -nodes \
  -sha256 \
  -days 365 \
  -subj "/CN=localhost" \
  -keyout "$key_file" \
  -out "$cert_file"

echo "Generated TLS certificate: $cert_file"
echo "Generated TLS private key: $key_file"
