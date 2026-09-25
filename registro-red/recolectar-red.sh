#!/bin/bash
# Recolecta el estado vivo de la red en lineas planas. El parseo se hace fuera.
#
# Formato:  TIPO <tab> campo1 <tab> campo2 <tab> campo3
#   LEASE   mac  ip  hostname-que-el-aparato-declara
#   STATIC  mac  ip  nombre-que-le-pusimos-nosotros
#   STA     mac  ap  ssid
#
# Tres fuentes, cada una aporta lo que las otras no. La tercera es la que dice
# DONDE esta un aparato inalambrico: preguntar a cada AP por una MAC da la zona
# de la casa. Un inventario sin eso te dice que algo existe, no donde buscarlo.
#
# Se emiten lineas y no JSON a proposito: construir JSON a mano en bash con
# bucles y subshells es fragil, y ya nos costo un fichero corrupto.
set -u
R=root@192.168.0.1

ssh -n -o ConnectTimeout=8 $R "cat /tmp/dhcp.leases 2>/dev/null" 2>/dev/null |
    awk '{print "LEASE\t" tolower($2) "\t" $3 "\t" $4}'

ssh -n -o ConnectTimeout=8 $R "uci show dhcp 2>/dev/null" 2>/dev/null |
    grep -E '\.(name|mac|ip)=' |
    awk -F'[.=]' '{gsub(/'"'"'/,"",$0); k=$2; f=$3; sub(/^[^=]*=/,"",$0); v=$0; print k "\t" f "\t" v}' |
    awk -F'\t' '{d[$1"|"$2]=$3; if(!($1 in seen)){orden[++n]=$1; seen[$1]=1}}
        END{for(i=1;i<=n;i++){k=orden[i]; m=d[k"|mac"]; if(m!="") print "STATIC\t" tolower(m) "\t" d[k"|ip"] "\t" d[k"|name"]}}'

for r in 192.168.0.1:Principal 192.168.1.6:Salon 192.168.1.7:Sotano 192.168.1.8:Arriba 192.168.1.3:Caseta; do
    ip=${r%%:*}; ap=${r##*:}
    ssh -n -o ConnectTimeout=6 -o BatchMode=yes root@$ip "
        for i in \$(iw dev 2>/dev/null | grep Interface | awk '{print \$2}'); do
          s=\$(iw dev \$i info 2>/dev/null | grep -w ssid | awk '{print \$2}')
          iw dev \$i station dump 2>/dev/null | grep '^Station' | awk -v S=\"\${s:-desconocida}\" '{print \$2 \" \" S}'
        done" 2>/dev/null |
        awk -v AP="$ap" '{print "STA\t" tolower($1) "\t" AP "\t" $2}'
done
