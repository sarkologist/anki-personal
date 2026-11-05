"""Evaluate a JS file in the stock editor webview via CDP; print the result."""
import json, sys, urllib.request
import websocket

targets = json.load(urllib.request.urlopen("http://localhost:8081/json"))
ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["title"] == sys.argv[1])
ws = websocket.create_connection(ws_url, suppress_origin=True)
expr = open(sys.argv[2]).read()
ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                    "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
while True:
    msg = json.loads(ws.recv())
    if msg.get("id") == 1:
        print(json.dumps(msg["result"].get("result", {}).get("value", msg["result"])))
        break
