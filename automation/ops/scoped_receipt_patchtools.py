#!/usr/bin/env python3
"""Patchtools limited to exact IDs from guarded promotion receipts."""

from __future__ import annotations

import argparse, fcntl, hashlib, json, os, stat, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId, json_util
from bson.json_util import CANONICAL_JSON_OPTIONS

SCHEMA_VERSION = 1


def canon(v: Any) -> str:
    return json_util.dumps(v, json_options=CANONICAL_JSON_OPTIONS,
                           sort_keys=True, separators=(",", ":"))


def sha_bytes(v: bytes) -> str: return hashlib.sha256(v).hexdigest()


def sha_file(p: Path) -> str:
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()


def now() -> str: return datetime.now(timezone.utc).isoformat()


def db_for(root: Path):
    sys.path.insert(0,str(root/'getnewurls_new'))
    from core.base_pipeline import MONGO_DB,MONGO_URI,MongoClient
    return MongoClient(MONGO_URI,serverSelectionTimeoutMS=20000)[MONGO_DB]


def observation_languages(plan: dict) -> dict[str,str]:
    result={}
    for line in (Path(plan['run_dir'])/'articles.jsonl').open():
        row=json.loads(line)
        if row.get('url') and row.get('language'): result[row['url']]=row['language']
    return result


def dry_run(root: Path, receipts: list[Path], out: Path):
    if out.exists() and any(out.iterdir()): raise RuntimeError('artifact dir not empty')
    out.mkdir(parents=True,exist_ok=True); db=db_for(root)
    operations=[]; receipt_hashes={}; countries=set()
    for rp in receipts:
        receipt=json.loads(rp.read_text()); countries.add(receipt['country'])
        plan=json.loads((rp.parent/'manifest.json').read_text())
        langs=observation_languages(plan)
        receipt_hashes[str(rp.resolve())]=sha_file(rp)
        for item in receipt['document_ids']:
            col=item['collection']; oid=ObjectId(item['id'])
            doc=db[col].find_one({'_id':oid})
            if not doc: raise RuntimeError(f'missing promoted row {item}')
            if doc.get('include') is not None: raise RuntimeError(f'already patched {item}')
            if not str(doc.get('title') or '').strip() or not str(doc.get('maintext') or '').strip() or not isinstance(doc.get('date_publish'),datetime):
                raise RuntimeError(f'invalid promoted row {item}')
            language=langs.get(doc['url'])
            language_source='observation_article'
            if not language:
                source_meta=db.sources.find_one({'source_domain':doc['source_domain']},{'primary_language':1}) or {}
                language=source_meta.get('primary_language')
                language_source='source_metadata_primary_language'
            if not language: raise RuntimeError(f'missing observed and source-metadata language {doc["url"]}')
            dup=[]
            for key in ('url','title','maintext'):
                match=db[col].find_one({'_id':{'$ne':oid},'source_domain':doc['source_domain'],key:doc[key]},{'_id':1})
                if match: dup.append(key)
            update={'include':True,'year':doc['date_publish'].year,'month':doc['date_publish'].month,'language':language}
            if language=='en': update.update({'language_translated':'en','title_translated':doc['title'],'maintext_translated':doc['maintext']})
            operations.append({'collection':col,'id':item['id'],'url':doc['url'],'source_domain':doc['source_domain'],
                               'action':'delete' if dup else 'update','duplicate_keys':dup,'update_set':None if dup else update,
                               'language_source':language_source,
                               'source_sha256':sha_bytes(canon(doc).encode())})
    if len(countries)!=1: raise RuntimeError('one country per manifest required')
    backup=out/'precommit_backup.jsonl'
    with backup.open('xb') as f:
        for op in operations:
            doc=db[op['collection']].find_one({'_id':ObjectId(op['id'])})
            f.write(canon({'collection':op['collection'],'document':doc}).encode()+b'\n')
        f.flush(); os.fsync(f.fileno())
    os.chmod(backup,stat.S_IRUSR|stat.S_IRGRP|stat.S_IROTH)
    m={'schema_version':SCHEMA_VERSION,'mode':'dry_run','generated_at':now(),'country':next(iter(countries)),
       'framework_root':str(root.resolve()),'receipt_hashes':receipt_hashes,
       'backup':{'path':backup.name,'sha256':sha_file(backup),'line_count':len(operations)},
       'summary':{'operations':len(operations),'updates':sum(o['action']=='update' for o in operations),'deletes':sum(o['action']=='delete' for o in operations)},
       'operations':operations}
    p=out/'manifest.json'; raw=(json.dumps(m,indent=2,sort_keys=True)+'\n').encode(); p.write_bytes(raw)
    p.with_name('manifest.json.sha256').write_text(f'{sha_bytes(raw)}  manifest.json\n')
    print(json.dumps({'manifest':str(p),'sha256':sha_bytes(raw),**m['summary']},sort_keys=True))


def commit(path: Path):
    side=path.with_name('manifest.json.sha256').read_text().split()[0]
    if sha_file(path)!=side: raise RuntimeError('manifest hash mismatch')
    m=json.loads(path.read_text()); root=Path(m['framework_root']); db=db_for(root)
    for rp,h in m['receipt_hashes'].items():
        if sha_file(Path(rp))!=h: raise RuntimeError('promotion receipt changed')
    backup=path.parent/m['backup']['path']
    if sha_file(backup)!=m['backup']['sha256'] or os.stat(backup).st_mode & stat.S_IWUSR: raise RuntimeError('backup changed or writable')
    lockp=root/'scraping_automation/var/locks/production-observation-promote.lock'; lockp.parent.mkdir(parents=True,exist_ok=True)
    with lockp.open('a+') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        updated=deleted=0
        for op in m['operations']:
            oid=ObjectId(op['id']); doc=db[op['collection']].find_one({'_id':oid})
            if not doc: raise RuntimeError(f'missing prewrite row {op["id"]}')
            if sha_bytes(canon(doc).encode())!=op['source_sha256']: raise RuntimeError(f'row drift {op["id"]}')
            if op['action']=='delete':
                r=db[op['collection']].delete_one({'_id':oid,'include':{'$exists':False}}); deleted+=r.deleted_count
            else:
                r=db[op['collection']].update_one({'_id':oid,'include':{'$exists':False}},{'$set':op['update_set']}); updated+=r.modified_count
        if updated!=m['summary']['updates'] or deleted!=m['summary']['deletes']: raise RuntimeError('write counts mismatch')
        for op in m['operations']:
            doc=db[op['collection']].find_one({'_id':ObjectId(op['id'])})
            if op['action']=='delete' and doc is not None: raise RuntimeError('delete postcheck failed')
            if op['action']=='update' and (doc is None or any(doc.get(k)!=v for k,v in op['update_set'].items())): raise RuntimeError('update postcheck failed')
        receipt={'schema_version':SCHEMA_VERSION,'mode':'committed','country':m['country'],'manifest_sha256':side,
                 'updated':updated,'deleted':deleted,'postchecked':len(m['operations']),'document_ids':[{'collection':o['collection'],'id':o['id'],'url':o['url']} for o in m['operations'] if o['action']=='update'],
                 'completed_at':now()}
        p=path.parent/'commit_receipt.json'; raw=(json.dumps(receipt,indent=2,sort_keys=True)+'\n').encode(); p.write_bytes(raw)
        p.with_name('commit_receipt.json.sha256').write_text(f'{sha_bytes(raw)}  commit_receipt.json\n')
        print(json.dumps(receipt,sort_keys=True))


def main():
    p=argparse.ArgumentParser(); p.add_argument('--framework-root',type=Path); p.add_argument('--promotion-receipt',type=Path,action='append'); p.add_argument('--artifact-dir',type=Path); p.add_argument('--commit-manifest',type=Path)
    a=p.parse_args()
    if a.commit_manifest: commit(a.commit_manifest)
    else:
        if not (a.framework_root and a.promotion_receipt and a.artifact_dir): p.error('dry run args required')
        dry_run(a.framework_root.resolve(),[x.resolve() for x in a.promotion_receipt],a.artifact_dir.resolve())

if __name__=='__main__': main()
