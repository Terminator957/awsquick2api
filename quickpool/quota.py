"""Version-bound desktop usage summary. Units are service credits, never tokens."""
from __future__ import annotations

import math
import time

import httpx

from .native_auth import AuthError, trusted_url


def normalize_usage(summary):
    if not isinstance(summary,dict):
        return {'status':'unavailable','reason':'上游未提供额度数据','updated_at':time.time()}
    result = {'status':'available','updated_at':time.time(),'unit':'agent_minutes'}
    for period in ('monthly','session'):
        source = summary.get(period+'Usage')
        if not isinstance(source,dict):
            source = {key:summary.get(period+key[0].upper()+key[1:]) for key in (
                'usedPercentage','resetsAt','resumeInMinutes','availableUnits','provisionedUnits',
                'provisionedUnitsWithoutOverage','overageUnits')}
        values = {}
        for key in ('usedPercentage','availableUnits','provisionedUnits','provisionedUnitsWithoutOverage',
                    'overageUnits','resumeInMinutes'):
            value = source.get(key)
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value >= 0:
                values[key] = value
        reset = source.get('resetsAt')
        if isinstance(reset,(str,int,float)) and len(str(reset)) <= 80:
            values['resetsAt'] = reset
        if values:
            result[period] = values
    if not any(period in result for period in ('monthly','session')):
        result.update(status='unavailable',reason='上游未提供额度数据')
    return result


def gateway_usage(data):
    entitlement = next((e for e in data.get('entitlements',[]) if isinstance(e,dict)
                        and e.get('unitType')=='AUTOMATION_AGENT_HOUR_UNIT'),{})
    available, provisioned = entitlement.get('numMinsAvailable'),entitlement.get('numMinsProvisioned')
    monthly = {'availableUnits':available,'provisionedUnits':provisioned,
               'overageUnits':entitlement.get('numMinsOverage')}
    if (isinstance(available,(int,float)) and isinstance(provisioned,(int,float)) and provisioned>0):
        monthly['usedPercentage'] = max(0,(provisioned-available)/provisioned*100)
    rate = entitlement.get('rateLimitStatus') or {}
    return {'monthlyUsage':monthly,'sessionUsage':{'usedPercentage':rate.get('consumptionPercentage'),
                                                 'resumeInMinutes':rate.get('estimatedMinutesToResume')}}


def is_quota_exhausted(quota: dict | None) -> bool:
    if not isinstance(quota, dict) or quota.get('status') != 'available':
        return False
    session = quota.get('session') or {}
    if isinstance(session, dict):
        used = session.get('usedPercentage')
        avail = session.get('availableUnits')
        if (isinstance(used, (int, float)) and used >= 99.5) or (isinstance(avail, (int, float)) and avail <= 0):
            return True
    monthly = quota.get('monthly') or {}
    if isinstance(monthly, dict):
        used = monthly.get('usedPercentage')
        avail = monthly.get('availableUnits')
        if (isinstance(used, (int, float)) and used >= 99.9) or (isinstance(avail, (int, float)) and avail <= 0):
            return True
    return False


def remaining_score(quota: dict | None) -> float:
    if not isinstance(quota, dict) or quota.get('status') != 'available':
        return 50.0
    session = quota.get('session') or {}
    if isinstance(session, dict) and isinstance(session.get('usedPercentage'), (int, float)):
        return max(0.0, min(100.0, 100.0 - float(session['usedPercentage'])))
    monthly = quota.get('monthly') or {}
    if isinstance(monthly, dict) and isinstance(monthly.get('usedPercentage'), (int, float)):
        return max(0.0, min(100.0, 100.0 - float(monthly['usedPercentage'])))
    return 50.0


class Quotas:
    def __init__(self,auth,transport=None):
        self.auth,self.transport,self.cache = auth,transport,{}

    async def refresh(self,account_id):
        try:
            credentials = await self.auth.credentials(account_id)
            base = trusted_url(credentials['tenantUrl']).rstrip('/')
            headers = {'Authorization':'Bearer '+credentials['access_token']}
            gateway = credentials['provider'] in ('gateway','social_gateway')
            async with httpx.AsyncClient(transport=self.transport,timeout=10,trust_env=False,
                                         follow_redirects=False) as client:
                if gateway:
                    response = await client.get(base+'/agenticmetering/v1/get-available-entitlement-units',
                                                headers=headers | {'Accept':'application/json'})
                else:
                    response = await client.post(base+'/integration/quick-work/user-info',
                                                 json={'excludeUserInfo':True},headers=headers)
            if response.status_code != 200:
                result = {'status':'unavailable','reason':f'上游额度接口 HTTP {response.status_code}',
                          'updated_at':time.time()}
            else:
                data = response.json()
                result = normalize_usage(gateway_usage(data) if gateway else data.get('usageSummary'))
                if gateway:
                    result['unit']='agent_minutes'
        except (AuthError,httpx.HTTPError,ValueError,TypeError,KeyError):
            result = {'status':'unavailable','reason':'暂时无法读取原生额度','updated_at':time.time()}
        self.cache[account_id] = result
        return result

    def cached(self,account_id):
        return self.cache.get(account_id,{'status':'unavailable','reason':'尚未刷新额度'})
