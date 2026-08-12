#!/usr/bin/env bash
# Живой лог бота на сервере. Ctrl+C — выйти.
# Использование:  ./logs.sh        (последние 40 строк + слежение)
#                 ./logs.sh 200    (последние 200 строк + слежение)
N="${1:-40}"
ssh -o BatchMode=yes root@bertam.online "journalctl -u zakupki-bot -n $N -f"
