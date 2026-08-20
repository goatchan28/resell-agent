#!/usr/bin/env bash
# Inspect a stored model call. Usage: bash inspect_trace.sh [trace_id]
TRACE="${1:-2}"
sqlite3 -noheader data/resell.db "SELECT response FROM model_call WHERE id = $TRACE;" \
| python3 -c "
import json, sys
body = json.loads(sys.stdin.read())
print('stop_reason:', body.get('stop_reason'))
for block in body.get('content', []):
    if block.get('type') != 'tool_use':
        print('block:', block.get('type'), str(block.get('text',''))[:200]); continue
    ti = block.get('input')
    print('tool_input type:', type(ti).__name__)
    if not isinstance(ti, dict):
        print('  raw:', str(ti)[:600]); continue
    print('top-level keys:', sorted(ti))
    for key, value in ti.items():
        print(f'--- {key}: {type(value).__name__}')
        if isinstance(value, str):
            print('    length:', len(value))
            print('    first 300 chars:')
            print('   ', value[:300].replace(chr(10), ' '))
            print('    last 120 chars:')
            print('   ', value[-120:].replace(chr(10), ' '))
            try:
                parsed = json.loads(value)
                print('    parses as:', type(parsed).__name__,
                      ('keys ' + str(sorted(parsed)[:8])) if isinstance(parsed, dict)
                      else ('length ' + str(len(parsed))))
                if isinstance(parsed, list) and parsed:
                    print('    first entry:', json.dumps(parsed[0])[:200])
            except ValueError as exc:
                print('    DOES NOT PARSE as JSON:', exc)
        elif isinstance(value, list):
            print('    length:', len(value))
            if value: print('    first entry:', json.dumps(value[0])[:200])
"
