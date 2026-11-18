import os,time,json,logging
from pathlib import Path
from redis import Redis
from sqlalchemy import select
from .database import Session
from .models import FuelSample
from .density_model import compare
from .drift_service import evaluate_pending
logging.basicConfig(level=logging.INFO)
r=Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
last_drift=0.0
while True:
    Path('/artifacts/bunker-heartbeat').write_text(str(time.time()))
    item=r.blpop('density-check',timeout=2)
    if item:
        id=int(item[1])
        try:
            with Session.begin() as s:
                x=s.scalar(select(FuelSample).where(FuelSample.id==id).with_for_update())
                if not x or x.status!='queued':continue
                x.log_density,x.threshold,passed,ev=compare(s,x);x.status='qualified'if passed else'anomaly';x.evidence=json.dumps(ev,ensure_ascii=False)
                Path(f'/artifacts/sample-{id}-evidence.json').write_text(x.evidence)
        except Exception as e:
            with Session.begin() as s:
                x=s.get(FuelSample,id)
                if x:x.status='failed';x.evidence=str(e)
            logging.exception('density check failed')
    # Rolling drift scan: resumes after interruption from reviewed samples that
    # have not entered a window yet; inclusion is committed once per sample.
    if time.time()-last_drift>=5:
        last_drift=time.time()
        try:
            for result in evaluate_pending('worker'):
                Path(f'/artifacts/drift-{result["grade"]}.json').write_text(json.dumps(result,ensure_ascii=False))
        except Exception:
            logging.exception('drift evaluation failed')
