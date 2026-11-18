from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from .database import Session
from .security import current,allow
from .drift_models import DriftAlert
from .drift_schemas import DriftClear
from . import drift_service as svc
from .constants import GRADES
router=APIRouter()
@router.get('/drift-monitor')
def monitor(u=Depends(current)):
    with Session() as s:return svc.drift_status(s)
@router.get('/drift-monitor/{grade}')
def grade_monitor(grade:str,u=Depends(current)):
    if grade not in GRADES:raise HTTPException(404,'未知燃油等级')
    with Session() as s:return svc.drift_view(s,grade)
@router.post('/drift-monitor/{grade}/evaluate')
def evaluate(grade:str,u=Depends(current)):
    allow(u,'admin','chemist')
    if grade not in GRADES:raise HTTPException(404,'未知燃油等级')
    with Session.begin() as s:return svc.evaluate_grade(s,grade,u['email'])
@router.get('/drift-audit')
def drift_audit(u=Depends(current)):
    with Session() as s:return svc.drift_audit(s)
@router.get('/drift-alerts')
def alerts(u=Depends(current)):
    with Session() as s:return [svc.alert_view(s,x) for x in s.scalars(select(DriftAlert).order_by(DriftAlert.id.desc()))]
@router.post('/drift-alerts/{id}/clear')
def clear(id:int,p:DriftClear,u=Depends(current)):
    allow(u,'admin')
    with Session.begin() as s:return svc.clear_alert(s,id,p.reason,u)
