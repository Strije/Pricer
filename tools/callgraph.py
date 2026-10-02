import ast,sys
src=open(sys.argv[1],encoding='utf-8').read()
tree=ast.parse(src)
cls=[n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='SkitchenApp'][0]
methods={n.name:n for n in cls.body if isinstance(n,(ast.FunctionDef,))}
def refs(fn):
    out=set(); attrs=set()
    for n in ast.walk(fn):
        if isinstance(n,ast.Attribute) and isinstance(n.value,ast.Name) and n.value.id=='self':
            (out if n.attr in methods else attrs).add(n.attr)
    return out,attrs
args=[a for a in sys.argv[2:] if not a.startswith('--stop=')]
stop=set(sum([a[7:].split(',') for a in sys.argv[2:] if a.startswith('--stop=')], []))
seen=set(); stack=args; allattrs={}
while stack:
    m=stack.pop()
    if m in seen: continue
    seen.add(m)
    if m in stop:  # метод интерфейса: в движок не берём и по его вызовам не идём
        allattrs[m]=set(); continue
    r,a=refs(methods[m]); allattrs[m]=a; stack+= [x for x in r if x not in seen]
tot=0
for m in sorted(seen, key=lambda m: methods[m].lineno):
    n=methods[m]; ln=n.end_lineno-n.lineno+1; tot+=ln
    print(f"{n.lineno:5} {ln:4} {m}  attrs={sorted(allattrs[m])}")
print('total lines',tot,'methods',len(seen))
