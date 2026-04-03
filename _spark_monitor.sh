#!/bin/bash
# 15-minute spark monitor — samples every 10s, logs state changes
LOG=/tmp/spark_monitor.log
echo "=== Spark Monitor Started: $(date) ===" > "$LOG"
PREV_STATE=""
for i in $(seq 1 90); do
  NOW=$(date +"%H:%M:%S")
  DATA=$(curl -s http://localhost:1903/api/v1/nodes 2>/dev/null)
  if [ -z "$DATA" ]; then
    SPARK="nCore unreachable"
  else
    SPARK=$(echo "$DATA" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n in d.get('nodes',[]):
  nid=n.get('node_id','?')
  if 'local' in nid:
    eps=n.get('endpoints',[])
    act=n.get('activity',{})
    mods=[e.get('model','?') for e in eps]
    st=act.get('state','?')
    am=act.get('model') or '-'
    print(f'{len(eps)} model(s) | activity={st} model={am} | loaded={mods}')
" 2>/dev/null)
  fi
  LINE="$NOW | $SPARK"
  if [ "$SPARK" != "$PREV_STATE" ]; then
    echo "** CHANGE ** $LINE" >> "$LOG"
    echo "** CHANGE ** $LINE"
    PREV_STATE="$SPARK"
  else
    echo "$LINE" >> "$LOG"
  fi
  sleep 10
done
echo "=== Monitor Complete: $(date) ===" >> "$LOG"
echo "=== Monitor Complete ==="
