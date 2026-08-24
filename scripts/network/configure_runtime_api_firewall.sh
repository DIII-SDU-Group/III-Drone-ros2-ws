#!/usr/bin/env bash
set -euo pipefail

OPERATOR_SUBNET=""
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --operator-subnet)
      OPERATOR_SUBNET="${2:-}"
      shift 2
      ;;
    --apply)
      APPLY=1
      shift
      ;;
    -h|--help)
      echo "Usage: $(basename "$0") --operator-subnet <IPv4 CIDR> [--apply]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! python3 - "$OPERATOR_SUBNET" <<'PY'
import ipaddress
import sys

try:
    network = ipaddress.ip_network(sys.argv[1], strict=True)
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if network.version == 4 and network.is_private and network.prefixlen >= 8 else 1)
PY
then
  echo "A private IPv4 CIDR (/8 or narrower) is required with --operator-subnet." >&2
  exit 2
fi

RULESET="table inet iii_operator {
  chain input {
    type filter hook input priority filter; policy accept;
    ct state established,related accept
    ip saddr $OPERATOR_SUBNET tcp dport 8765 accept
    tcp dport 8765 drop
    ip saddr $OPERATOR_SUBNET udp dport 5353 accept
    udp dport 5353 drop
  }
}"

if [[ "$APPLY" != "1" ]]; then
  printf '%s\n' "$RULESET"
  echo "Dry run only. Re-run with --apply on the runtime host."
  exit 0
fi

[[ "$EUID" == "0" ]] || { echo "--apply must run as root." >&2; exit 3; }
command -v nft >/dev/null || { echo "nftables is required on the runtime host." >&2; exit 3; }
install -d -m 0755 /etc/nftables.d
printf '%s\n' "$RULESET" > /etc/nftables.d/iii-operator.nft
chmod 0644 /etc/nftables.d/iii-operator.nft
touch /etc/nftables.conf
if ! grep -Fq 'include "/etc/nftables.d/*.nft"' /etc/nftables.conf; then
  printf '%s\n' 'include "/etc/nftables.d/*.nft"' >> /etc/nftables.conf
fi
nft delete table inet iii_operator 2>/dev/null || true
nft -f /etc/nftables.d/iii-operator.nft
nft list table inet iii_operator
