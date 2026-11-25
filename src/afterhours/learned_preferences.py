"""Explicitly adopted, allowlisted preferences with manual-override precedence."""
import copy
import math
import re


def valid_effect(effect):
    if not isinstance(effect,dict) or set(effect)!={'kind','value'}:return False
    kind,value=effect['kind'],effect['value']
    if kind=='discover':return type(value) is bool
    if kind=='style':return (isinstance(value,dict) and set(value)=={'id','stop'} and value['id'] in ('energy','adv','vocals','blend')
        and type(value['stop']) is int and 0<=value['stop']<=4)
    return kind=='rule' and isinstance(value,dict) and set(value)=={'id','on'} and value['id']=='reorder' and type(value['on']) is bool


def key(effect):return effect['kind']+(':'+effect['value']['id'] if isinstance(effect['value'],dict) else '')


def describe(effect):
    from afterhours import dj_settings
    if effect['kind']=='style':
        value=effect['value'];name={'energy':'Energy','adv':'Adventure','vocals':'Vocals','blend':'Blends'}[value['id']]
        label=dj_settings.STYLE_LABELS[value['id']][value['stop']]
        return name+': '+label,f'You explicitly set {name.lower()} to {label.lower()} during this run. Keeping this carries that supported preference into future planning.'
    if effect['kind']=='discover':
        return ('Discover new music' if effect['value'] else 'Choose from the saved crate',
                'You explicitly changed new-music discovery during this run. Keeping this retains that choice for future planning.')
    return ('Allow future queue reordering' if effect['value']['on'] else 'Keep the chosen queue order',
            'You explicitly changed the future queue-order preference during this run. Playing and cued music stays protected.')


def initial_base(state):
    return {'style':copy.deepcopy(state['style']),'discover':state['discover'],'allowReorder':state['allowReorder']}


def assign(base,effect):
    if effect['kind']=='style':base['style'][effect['value']['id']]=effect['value']['stop']
    elif effect['kind']=='discover':base['discover']=effect['value']
    else:base['allowReorder']=effect['value']['on']


def restore(state,saved):
    base=saved.get('preferenceBase') if isinstance(saved,dict) else None
    try:
        from afterhours import dj_settings
        if not isinstance(base,dict) or set(base)!={'style','discover','allowReorder'}:raise ValueError()
        dj_settings.validate_style(base['style'])
        if type(base['discover']) is not bool or type(base['allowReorder']) is not bool:raise ValueError()
        state['preferenceBase']=copy.deepcopy(base)
    except (ValueError,KeyError,TypeError):state['preferenceBase']=initial_base(state)
    state['learned']=[]
    rows=saved.get('learned',[]) if isinstance(saved,dict) else []
    for row in rows[-32:] if isinstance(rows,list) else []:
        if (not isinstance(row,dict) or not re.fullmatch(r'[a-f0-9]{32}',str(row.get('archiveId','')))
                or not re.fullmatch(r'[a-f0-9]{24}',str(row.get('lessonId',''))) or not valid_effect(row.get('effect'))
                or type(row.get('on')) is not bool or not isinstance(row.get('title'),str) or len(row['title'])>300
                or not isinstance(row.get('detail'),str) or len(row['detail'])>800
                or row.get('id')!='lesson:'+row['archiveId']+':'+row['lessonId']
                or type(row.get('changedAt')) not in (int,float) or not math.isfinite(row['changedAt']) or row['changedAt']<0):continue
        state['learned']=[v for v in state['learned'] if v['id']!=row['id']]
        state['learned'].append({k:copy.deepcopy(row[k]) for k in ('id','archiveId','lessonId','title','detail','effect','on','changedAt')})
    effective(state)


def effective(state):
    result=copy.deepcopy(state['preferenceBase'])
    for row in state['learned']:
        if row['on']:assign(result,row['effect'])
    state.update(result)
    return result


def manual(state,effect):
    if not valid_effect(effect):return
    assign(state['preferenceBase'],effect)
    for row in state['learned']:
        if key(row['effect'])==key(effect):row['on']=False
    effective(state)


def adopt(state,archive,lesson,keep,at):
    identifier='lesson:'+archive['id']+':'+lesson['id']
    known=[row for row in state['learned'] if row['id']!=identifier]
    if len(known)>=32:
        dropped=next((row for row in known if not row['on']),None)
        if dropped:known.remove(dropped)
        else:raise ValueError('The learned preference limit is reached. Drop an older learned preference before keeping another.')
    known.append({'id':identifier,'archiveId':archive['id'],'lessonId':lesson['id'],
        'title':lesson['title'],'detail':lesson['detail'],'effect':copy.deepcopy(lesson['effect']),
        'on':keep,'changedAt':at})
    state['learned']=known
    return effective(state)


def rules(state):
    return [{k:copy.deepcopy(row[k]) for k in ('id','title','detail','on','changedAt')}|{'group':'learned'} for row in state['learned']]
