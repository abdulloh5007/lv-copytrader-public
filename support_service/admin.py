"""Owner-only inspection via docker exec; no public inbox API."""
import argparse
import json
import os
from pathlib import Path
from .app import Inbox


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['list','show'])
    p.add_argument('id',nargs='?')
    args=p.parse_args()
    inbox=Inbox(Path(os.environ.get('SUPPORT_DATA_DIR','/data'))/'support.sqlite3',os.environ['SUPPORT_KEY'])
    if args.command=='show':
        if not args.id:p.error('show requires report ID')
        print(json.dumps(inbox.read(args.id),indent=2,ensure_ascii=False))
    else:
        with inbox.db() as db:
            rows=db.execute('SELECT id,created,tg,email,tg_attempts,email_attempts,tg_error,email_error FROM reports ORDER BY created DESC LIMIT 50').fetchall()
        print(json.dumps([dict(x) for x in rows],indent=2))

if __name__=='__main__':main()
