"""Per-grade rolling drift evaluation.

Statistical notes
-----------------
* The baseline is always the fixed reference samples of the same fuel grade;
  business samples never enter the reference set.
* Only already-reviewed business samples enter a monitoring window.  Every
  inclusion is written to a permanent ledger exactly once (unique sample id),
  so a restarted worker resumes from reviewed samples that have not entered a
  window yet and a retry can never count the same bottle twice.
* The rolling window keeps the most recent ``DRIFT_WINDOW_SIZE`` included
  samples; older inclusions are marked ``slid`` and stay in the ledger.
* Tank-balanced weights (every source tank contributes equal total weight) stop
  a dense sampling run from one tank from dominating the verdict.
* Fewer than six active samples or samples from a single tank are not enough
  evidence; the window reports ``pending`` (待观察) and can never alert.
"""
from datetime import datetime,timezone
from collections import Counter
import numpy as np
from fastapi import HTTPException
from sqlalchemy import select,exists,func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .models import Tank,FuelSample
from .drift_models import GradeDrift,DriftWindow,DriftWindowSample,DriftAlert
from .transfer_service import obj,audit
from .constants import GRADES
from .database import Session

DRIFT_WINDOW_SIZE=12
DRIFT_MIN_SAMPLES=6
DRIFT_MIN_TANKS=2
DRIFT_MIN_PER_TANK=2
DRIFT_MIN_REFERENCE=20
DRIFT_Z_LIMIT=2.5
DRIFT_MIN_SHIFT=4.0
DRIFT_REVIEWED=('qualified','retested')
DRIFT_LIMITS={'window_size':DRIFT_WINDOW_SIZE,'min_samples':DRIFT_MIN_SAMPLES,'min_tanks':DRIFT_MIN_TANKS,'min_per_tank':DRIFT_MIN_PER_TANK,'min_reference':DRIFT_MIN_REFERENCE,'z_limit':DRIFT_Z_LIMIT,'min_shift':DRIFT_MIN_SHIFT}


def now():return datetime.now(timezone.utc).isoformat()


def grade_gate(s,grade,lock=False):
    """Per-grade gate row.  Release and evaluation both lock it first (same order)."""
    def lookup():
        q=select(GradeDrift).where(GradeDrift.grade==grade)
        if lock:q=q.with_for_update().execution_options(populate_existing=True)
        return s.scalar(q)
    g=lookup()
    if g:return g
    s.execute(pg_insert(GradeDrift).values(grade=grade,state='normal',cursor_sample_id=0,last_evaluated_at='',updated_at=now()).on_conflict_do_nothing(index_elements=['grade']))
    return lookup()


def effective_density(x):
    """Verified review density: a signed retest supersedes the original reading."""
    if x.status=='retested' and x.retest_density is not None:return x.retest_density
    return x.density_15


def baseline(s,grade):
    refs=s.scalars(select(FuelSample).join(Tank,FuelSample.tank_id==Tank.id).where(FuelSample.reference==True,Tank.grade==grade).order_by(FuelSample.id)).all()
    values=np.array([float(x.density_15) for x in refs])
    if len(values)<DRIFT_MIN_REFERENCE:
        return None,{'reference_count':len(values),'reason':f'本等级固定参考样不足{DRIFT_MIN_REFERENCE}条，暂不能建立漂移基准'}
    median=float(np.median(values))
    sigma=1.4826*float(np.median(np.abs(values-median)))
    if sigma<.5:sigma=max(float(np.std(values)),.5)
    return {'grade':grade,'reference_count':len(values),'reference_median':median,'reference_sigma':sigma,'sigma_method':'1.4826 × MAD（MAD 过小时回退到标准差，且不低于0.5）','baseline_scope':'reference=True 且同燃油等级；业务样不参与基准；参考样自身不进入业务窗口'},{}


def _reviewed_unincluded(s,grade):
    borrowed=select(DriftWindowSample.sample_id).where(DriftWindowSample.sample_id==FuelSample.id)
    return s.scalars(select(FuelSample).join(Tank,FuelSample.tank_id==Tank.id).where(FuelSample.reference==False,Tank.grade==grade,FuelSample.status.in_(DRIFT_REVIEWED),~exists(borrowed)).order_by(FuelSample.id)).all()


def _open_window(s,grade,lock=False):
    q=select(DriftWindow).where(DriftWindow.grade==grade,DriftWindow.state=='open').order_by(DriftWindow.seq.desc())
    if lock:q=q.with_for_update().execution_options(populate_existing=True)
    return s.scalar(q)


def _window_rows(s,window,states=None):
    q=select(DriftWindowSample).where(DriftWindowSample.window_id==window.id)
    if states:q=q.where(DriftWindowSample.state.in_(states))
    return s.scalars(q.order_by(DriftWindowSample.sample_id)).all()


def _sample_evidence(s,rows):
    out=[]
    for r in rows:
        x=s.get(FuelSample,r.sample_id);t=s.get(Tank,r.tank_id)
        if not x or not t:continue
        out.append({'sample_id':x.id,'sample_code':x.code,'tank_id':t.id,'tank_code':t.code,'density_15':float(r.density_15),'sample_status':x.status,'ledger_state':r.state,'included_at':r.included_at})
    return out


def _evaluate(rows,base):
    """Tank-balanced statistics over the window rows (already capped to window size)."""
    per_tank=Counter(r.tank_id for r in rows)
    raw=[1/per_tank[r.tank_id] for r in rows]
    total=sum(raw);weights=[w/total for w in raw]
    mean=sum(w*float(r.density_15) for w,r in zip(weights,rows))
    shift=mean-base['reference_median']
    sum_sq=sum(w*w for w in weights)
    effective_n=1/sum_sq if sum_sq else 0
    se=base['reference_sigma']*np.sqrt(1/effective_n+1/base['reference_count']) if effective_n else 0.0
    z=shift/se if se else 0.0
    threshold=max(DRIFT_MIN_SHIFT,DRIFT_Z_LIMIT*se)
    tanks={}
    for w,r in zip(weights,rows):
        v=tanks.setdefault(r.tank_id,{'tank_id':r.tank_id,'tank_code':None,'samples':0,'weight':0.0})
        v['samples']+=1;v['weight']=round(v['weight']+w,6)
    return {'evaluated_sample_ids':[r.sample_id for r in rows],'evaluated_sample_count':len(rows),'tank_count':len(per_tank),'effective_sample_size':float(effective_n),'tank_weights':list(tanks.values()),'mean_density_15':float(mean),'shift':float(shift),'standard_error':float(se),'z':float(z),'z_limit':DRIFT_Z_LIMIT,'min_shift':DRIFT_MIN_SHIFT,'threshold_shift':float(threshold),'tank_weighting':'按来源罐均衡计权（各罐总权重相等），避免单罐密集取样主导；标准差按均衡后的有效样本量放大'}


def _empty_stats():
    return {'evaluated_sample_ids':[],'evaluated_sample_count':0,'tank_count':0,'effective_sample_size':0.0,'tank_weights':[],'mean_density_15':None,'shift':None,'standard_error':None,'z':None,'z_limit':DRIFT_Z_LIMIT,'min_shift':DRIFT_MIN_SHIFT,'threshold_shift':None,'tank_weighting':'按来源罐均衡计权（各罐总权重相等），避免单罐密集取样主导'}


def _fill_tank_codes(s,stats):
    for tw in stats['tank_weights']:
        t=s.get(Tank,tw['tank_id']);tw['tank_code']=t.code if t else None


def _pending_reason(stats):
    if stats['evaluated_sample_count']<DRIFT_MIN_SAMPLES:return f"窗口已审样本不足{DRIFT_MIN_SAMPLES}条，证据不足待观察"
    if stats['tank_count']<DRIFT_MIN_TANKS:return f"窗口仅来自{stats['tank_count']}个储罐，单罐密集取样不能主导判定，待观察"
    thin=[t for t in stats['tank_weights'] if t['samples']<DRIFT_MIN_PER_TANK]
    if thin:
        names='、'.join(t['tank_code'] or f"罐{t['tank_id']}" for t in thin)
        return f"{names} 入窗样本不足{DRIFT_MIN_PER_TANK}条，证据不足待观察"
    return ''


def _select_window(rows):
    """Pick the active evaluation set: newest rows first, capped per source tank.

    The per-tank cap (window size ÷ minimum tank count) stops one tank's dense
    sampling run from filling the whole window and crowding out every other tank.
    Rows that fall outside stay in the ledger as ``slid`` and are never re-included.
    """
    cap=max(DRIFT_MIN_SAMPLES,DRIFT_WINDOW_SIZE//DRIFT_MIN_TANKS)
    per_tank=Counter();keep=[];drop=[]
    for r in sorted(rows,key=lambda x:x.sample_id,reverse=True):
        if len(keep)<DRIFT_WINDOW_SIZE and per_tank[r.tank_id]<cap:
            keep.append(r);per_tank[r.tank_id]+=1
        else:drop.append(r)
    return sorted(keep,key=lambda x:x.sample_id),drop


def _sync_active_densities(s,window,actor):
    """A later signed retest supersedes the value used when the sample first entered.

    Only rows still counted in the open window are refreshed; slid/cleared rows and
    closed windows keep their audit snapshot so a cleared alert cannot be reopened
    by rewriting history.
    """
    changed=0
    for r in _window_rows(s,window,('active',)):
        x=s.get(FuelSample,r.sample_id)
        if not x:continue
        if x.status not in DRIFT_REVIEWED:
            # A later retest can fail the grade spec; the bottle leaves the
            # counted set but keeps its ledger row (never re-included).
            r.state='slid';changed+=1;continue
        value=effective_density(x)
        if value!=r.density_15:
            r.density_15=value;changed+=1
    if changed:audit(s,{'email':actor},'drift_evidence_updated',f'窗口#{window.seq}：{changed}条纳入证据随复检签注更新')
    return changed


def evaluate_grade(s,grade,actor='worker'):
    gate=grade_gate(s,grade,lock=True)
    base,missing=baseline(s,grade)
    pending=_reviewed_unincluded(s,grade)
    window=_open_window(s,grade,lock=True)
    if pending:
        if window is None:
            seq=int(s.scalar(select(func.coalesce(func.max(DriftWindow.seq),0)).where(DriftWindow.grade==grade)) or 0)+1
            window=DriftWindow(grade=grade,seq=seq,state='open',cursor_sample_id=0,sample_count=0,tank_count=0,evaluated_at=now(),opened_at=now());s.add(window);s.flush()
            audit(s,{'email':actor},'drift_window_opened',f'{grade}#{seq}')
        for x in pending:
            s.add(DriftWindowSample(window_id=window.id,sample_id=x.id,tank_id=x.tank_id,density_15=effective_density(x),included_at=now(),state='active'))
            s.flush()
        audit(s,{'email':actor},'drift_samples_included',f'{grade}:{len(pending)}条')
    if window is None:
        gate.last_evaluated_at=now();gate.updated_at=now()
        ev={'verdict':'pending','reason':missing.get('reason') or '尚无已审业务样进入漂移窗口','evaluated_at':now(),'baseline':base or missing,**_empty_stats()}
        return {'grade':grade,'state':gate.state,'verdict':'pending','evidence':ev}
    active=_window_rows(s,window,('active',))
    if _sync_active_densities(s,window,actor):
        active=_window_rows(s,window,('active',))
    active,rotated=_select_window(active)
    for r in rotated:r.state='slid'
    stats=_evaluate(active,base) if base else _empty_stats()
    _fill_tank_codes(s,stats)
    if base is None:
        verdict='pending';reason=missing['reason']
    elif gate.state=='alerted':
        verdict='alert';reason='该等级已有未解除的漂移告警，新样继续纳入窗口监测'
    else:
        reason=_pending_reason(stats)
        if reason:verdict='pending'
        elif abs(stats['shift'])>=stats['threshold_shift']:verdict='alert';reason=f"窗口均衡均值偏移{stats['shift']:+.2f} kg/m³，达到判定门限±{stats['threshold_shift']:.2f} kg/m³"
        else:verdict='normal';reason=f"窗口均衡均值偏移{stats['shift']:+.2f} kg/m³，低于判定门限±{stats['threshold_shift']:.2f} kg/m³"
    evidence={'verdict':verdict,'reason':reason,'evaluated_at':now(),'baseline':base or missing,**stats}
    window.sample_count=len(active);window.tank_count=stats['tank_count'];window.cursor_sample_id=max([r.sample_id for r in active],default=0);window.evaluated_at=now();window.evidence=evidence
    if verdict=='alert' and gate.state!='alerted':
        alert=DriftAlert(grade=grade,window_id=window.id,state='active',sample_count=stats['evaluated_sample_count'],tank_count=stats['tank_count'],shift=stats['shift'],z=stats['z'],evidence=evidence,detected_at=now(),clear_reason='',watermark_sample_id=None)
        s.add(alert);s.flush()
        gate.state='alerted';gate.active_alert_id=alert.id
        audit(s,{'email':actor},'drift_alerted',f"{grade}: 偏移{stats['shift']:+.3f} kg/m³ / z={stats['z']:.2f}")
    gate.cursor_sample_id=max(gate.cursor_sample_id,window.cursor_sample_id);gate.last_evaluated_at=now();gate.updated_at=now()
    if pending:audit(s,{'email':actor},'drift_evaluated',f'{grade}:{verdict}')
    return {'grade':grade,'state':gate.state,'verdict':verdict,'evidence':evidence}


def evaluate_all(actor='worker'):
    with Session.begin() as s:
        return [evaluate_grade(s,g,actor) for g in sorted(GRADES)]


def evaluate_pending(actor='worker'):
    """Cheap worker check; also lets a restarted worker resume un-windowed samples."""
    with Session.begin() as s:
        ready=[g for g in sorted(GRADES) if _reviewed_unincluded(s,g)]
        return [evaluate_grade(s,g,actor) for g in ready] if ready else []


def pending_count(s,grade):
    return len(_reviewed_unincluded(s,grade))


def window_view(s,x):
    return {**obj(x),'samples':_sample_evidence(s,_window_rows(s,x)),'evidence':x.evidence or {}}


def alert_view(s,x):
    w=s.get(DriftWindow,x.window_id)
    return {**obj(x),'window_seq':w.seq if w else None}


def drift_view(s,grade,limit=5):
    gate=s.scalar(select(GradeDrift).where(GradeDrift.grade==grade))
    base,missing=baseline(s,grade)
    windows=s.scalars(select(DriftWindow).where(DriftWindow.grade==grade).order_by(DriftWindow.seq.desc()).limit(limit)).all()
    alerts=s.scalars(select(DriftAlert).where(DriftAlert.grade==grade).order_by(DriftAlert.id.desc()).limit(limit)).all()
    open_window=next((x for x in windows if x.state=='open'),None)
    active_alert=next((x for x in alerts if x.state=='active'),None)
    latest=open_window or (windows[0] if windows else None)
    if active_alert:state='alert';reason='存在未解除漂移告警，该等级新调拨放行已暂停'
    elif open_window and open_window.evidence:state=open_window.evidence.get('verdict','pending');reason=open_window.evidence.get('reason','')
    elif windows:state='pending';reason='主管已解除上一窗口告警；新样将进入新窗口继续监测，已审样不再触发同一告警'
    else:state='pending';reason='尚无已审业务样进入漂移窗口'
    return {'grade':grade,'state':state,'reason':reason,'gate':obj(gate) if gate else None,'active_alert':alert_view(s,active_alert) if active_alert else None,'latest_window':window_view(s,latest) if latest else None,'windows':[window_view(s,x) for x in windows],'alerts':[alert_view(s,x) for x in alerts],'baseline':base or missing,'pending_reviewed_samples':pending_count(s,grade),'limits':DRIFT_LIMITS}


def drift_status(s):
    return {'grades':[drift_view(s,g) for g in sorted(GRADES)],'limits':DRIFT_LIMITS,'server_time':now()}


def drift_audit(s,limit=80):
    from .models import Audit
    actions=('drift_window_opened','drift_samples_included','drift_evaluated','drift_evidence_updated','drift_alerted','drift_samples_cleared','drift_alert_cleared')
    return [obj(x) for x in s.scalars(select(Audit).where(Audit.action.in_(actions)).order_by(Audit.id.desc()).limit(limit))]


def clear_alert(s,id,reason,u):
    candidate=get_or_404(s,DriftAlert,id)
    gate=grade_gate(s,candidate.grade,lock=True)
    x=get_or_404(s,DriftAlert,id,lock=True)
    if x.state!='active':raise HTTPException(409,'该漂移告警已解除，不会重开')
    if gate.active_alert_id!=x.id:raise HTTPException(409,'告警与当前等级状态不一致')
    window=get_or_404(s,DriftWindow,x.window_id,lock=True)
    rows=_window_rows(s,window)
    watermark=max([r.sample_id for r in rows],default=0)
    for r in rows:r.state='cleared'
    # Samples already reviewed at the moment of clearing but not yet written into
    # the window (for example while the worker was down) belong to this same
    # examination; adopt them as cleared ledger entries so they cannot raise the
    # same alert again.  Samples reviewed later enter the next window normally.
    late=_reviewed_unincluded(s,candidate.grade)
    for y in late:
        s.add(DriftWindowSample(window_id=window.id,sample_id=y.id,tank_id=y.tank_id,density_15=effective_density(y),included_at=now(),state='cleared'))
        watermark=max(watermark,y.id)
    if late:audit(s,u,'drift_samples_cleared',f'{candidate.grade}: 另{len(late)}条本次已审样随告警解除停用，不再触发同一告警')
    window.state='closed';window.closed_at=now()
    x.state='cleared';x.cleared_by=u['email'];x.cleared_at=now();x.clear_reason=reason;x.watermark_sample_id=watermark
    gate.state='normal';gate.active_alert_id=None;gate.updated_at=now()
    audit(s,u,'drift_alert_cleared',f'{x.grade}: 本窗口已审{len(rows)}条不再触发同一告警，watermark={watermark} / {reason}')
    return alert_view(s,x)


def get_or_404(s,model,id,lock=False):
    q=select(model).where(model.id==id)
    if lock:q=q.with_for_update().execution_options(populate_existing=True)
    x=s.scalar(q)
    if not x:raise HTTPException(404,'记录不存在')
    return x
