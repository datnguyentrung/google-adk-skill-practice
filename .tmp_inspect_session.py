import sqlite3, json
DB = r'D:\Thuc_tap_MB\google-adk-skill-practice\app\.adk\session.db'
SID = 'dd0dbab6-32f0-49f0-9499-76c7f92eea86'
c = sqlite3.connect(DB)
rows = c.execute('select timestamp,event_data from events where session_id=? order by timestamp', (SID,)).fetchall()
print('events', len(rows))
for ts, raw in rows:
    d = json.loads(raw)
    parts = ((d.get('content') or {}).get('parts') or [])
    if not parts:
        continue
    u = d.get('usage_metadata') or {}
    print('\n---', round(ts, 3), d.get('author'), 'prompt=', u.get('prompt_token_count'), 'cached=', u.get('cached_content_token_count'))
    for p in parts:
        if 'function_call' in p:
            fc = p['function_call']
            print('CALL', fc['name'], json.dumps(fc.get('args', {}), ensure_ascii=True)[:1800])
        elif 'function_response' in p:
            fr = p['function_response']
            print('RESP', fr['name'], json.dumps(fr.get('response', {}), ensure_ascii=True)[:3500])
        elif 'text' in p:
            text = p['text'][:1800].replace('\n', ' | ')
            print('TEXT', text.encode('ascii', 'backslashreplace').decode('ascii'))
