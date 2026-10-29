"""Execute an offline READ → REVIEW → RECONCILE lifecycle on synthetic records."""
from contextlib import closing
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).parent/'src'))
from autohunt.business_batch import BusinessBatch
from autohunt.context import provision_candidate, select_candidate


def run(out):
    out.mkdir(parents=True,exist_ok=True)
    # Fresh state on each run. A pre-existing database is never reset.
    with tempfile.TemporaryDirectory() as temporary:
        database=Path(temporary)/'workflow.sqlite'
        provision_candidate(database,candidate_id='demo',display_name='Demo Candidate',job_policy={'allowed_work_formats':['remote']},automation_policy={},account_binding={'hh_identity':'demo','resume_id':'synthetic-resume','resume_title':'Operations'},job_policy_version='demo-policy',automation_policy_version='demo-automation',binding_id='demo-binding')
        select_candidate(database,'demo')
        batch=BusinessBatch(database)
        run_id=batch.start_read(queries=['remote operations'])
        checkpoints=[{'phase':'READ_RUNNING','meaning':'Collect full source records before review'}]
        batch.complete_read(run_id,full_read=3,incidents=0)
        checkpoints.append({'phase':'SEMANTIC_PENDING','meaning':'Durable review handoff; no browser session held open'})
        try:
            batch.begin_apply(run_id)
        except RuntimeError as error:
            checkpoints.append({'phase':'ACTION_BLOCKED','meaning':str(error)})
        # Same database, new object: process restart preserves current phase.
        recovered=BusinessBatch(database).status()
        assert recovered['runs'][0]['status']=='awaiting_semantic'
        with closing(sqlite3.connect(database)) as db, db:
            snapshot=[dict(zip(['name','value'],row)) for row in db.execute('SELECT name,value_json FROM candidate_checkpoints')]
        report={'scenario':'synthetic-offline','records_read':3,'state_after_restart':recovered['runs'][0]['status'],'action_count':0,'checkpoints':checkpoints,'durable_checkpoints':snapshot}
    (out/'workflow.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('READ → REVIEW → blocked unapproved action → recovered review state.')
    print(f'Output: {out/"workflow.json"}')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=Path('output'))
    run(parser.parse_args().out)
