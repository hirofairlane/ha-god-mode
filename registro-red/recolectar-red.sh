#!/bin/bash
# Collects the network's live state as flat lines. Parsing happens elsewhere.
#
# Format:  TYPE <tab> field1 <tab> field2 <tab> field3
#   LEASE   mac  ip  hostname-the-device-declares
#   STATIC  mac  ip  name-we-gave-it
#   STA     mac  ap  ssid
#
# Three sources, each contributing what the others can't. The third one is
# what tells you WHERE a wireless device is: asking each AP about a MAC
# gives you the zone of the house. An inventory without that tells you
# something exists, not where to look for it.
#
# Lines are emitted on purpose instead of JSON: building JSON by hand in
# bash with loops and subshells is fragile, and it already cost us one
# corrupted file.
set -u

# Real router/AP addresses live in infra.local.sh (gitignored). Copy
# infra.example.sh to infra.local.sh and fill in your own.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/infra.local.sh" 2>/dev/null || source "$HERE/infra.example.sh"

ssh -n -o ConnectTimeout=8 $R "cat /tmp/dhcp.leases 2>/dev/null" 2>/dev/null |
    awk '{print "LEASE\t" tolower($2) "\t" $3 "\t" $4}'

ssh -n -o ConnectTimeout=8 $R "uci show dhcp 2>/dev/null" 2>/dev/null |
    grep -E '\.(name|mac|ip)=' |
    awk -F'[.=]' '{gsub(/'"'"'/,"",$0); k=$2; f=$3; sub(/^[^=]*=/,"",$0); v=$0; print k "\t" f "\t" v}' |
    awk -F'\t' '{d[$1"|"$2]=$3; if(!($1 in seen)){orden[++n]=$1; seen[$1]=1}}
        END{for(i=1;i<=n;i++){k=orden[i]; m=d[k"|mac"]; if(m!="") print "STATIC\t" tolower(m) "\t" d[k"|ip"] "\t" d[k"|name"]}}'

for r in $APS; do
    ip=${r%%:*}; ap=${r##*:}
    ssh -n -o ConnectTimeout=6 -o BatchMode=yes root@$ip "
        for i in \$(iw dev 2>/dev/null | grep Interface | awk '{print \$2}'); do
          s=\$(iw dev \$i info 2>/dev/null | grep -w ssid | awk '{print \$2}')
          iw dev \$i station dump 2>/dev/null | grep '^Station' | awk -v S=\"\${s:-desconocida}\" '{print \$2 \" \" S}'
        done" 2>/dev/null |
        awk -v AP="$ap" '{print "STA\t" tolower($1) "\t" AP "\t" $2}'
done
