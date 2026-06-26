import json, collections
F="/private/tmp/claude-501/-Users-gm-stock-tracker/389c50af-1926-4f30-843d-0686f62d39f2/tasks/wr7wdsjfd.output"
raw=json.load(open(F))
def find(o,key):
    if isinstance(o,dict):
        if key in o: return o[key]
        for v in o.values():
            r=find(v,key)
            if r is not None: return r
    elif isinstance(o,list):
        for v in o:
            r=find(v,key)
            if r is not None: return r
    return None
buckets=find(raw,"buckets")
act=collections.Counter(); bad=[]; nres=0
for b in buckets:
    if not b.get("import_ok"): bad.append(b["bucket"])
    for x in b["results"]: act[x["action"]]+=1; nres+=1
print("buckets:", len(buckets), "handled:", nres)
print("actions:", dict(act))
print("import problems:", bad or "NONE")
json.dump([{"id":x["id"],"action":x["action"],"note":x["note"]} for b in buckets for x in b["results"]], open(".scan150/resolution.json","w"), indent=1)
