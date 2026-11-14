import asyncio, json, websockets, time, datetime
async def main():
    t0=time.time()
    async with websockets.connect("wss://pumpportal.fun/api/data", max_size=None) as ws:
        await ws.send(json.dumps({"method":"subscribeMigration"}))
        msgs=[]
        while time.time()-t0<900:
            try:
                m=await asyncio.wait_for(ws.recv(),timeout=900-(time.time()-t0))
            except asyncio.TimeoutError: break
            ts=datetime.datetime.utcnow().isoformat()+"Z"
            print(ts, m[:300], flush=True)
            try: d=json.loads(m)
            except: d={"_raw":m}
            msgs.append({"received_at":ts,"msg":d})
            if isinstance(d,dict) and d.get("mint"):
                break
        json.dump(msgs, open("logs/raw/pumpportal_migration.json","w"), indent=2)
asyncio.run(main())
