"""AI workflows: evidence selection, reviewable outputs, ownership and retention."""
import hashlib
import json
import re
import secrets
from datetime import datetime, date, timedelta, timezone
import ai_service

Error=ai_service.AIError
TZ=timezone(timedelta(hours=8))
PROMPT_VERSION='golf-evidence-2'
RETENTION_DAYS=30

def stamp(): return datetime.now(TZ).isoformat(timespec='seconds')
def pack(x): return json.dumps(x,ensure_ascii=False,separators=(',',':'))
def fingerprint(x): return hashlib.sha256(pack(x).encode()).hexdigest()
def validate_text(v,limit=2000,required=False):
    if not isinstance(v,str) or len(v)>limit or (required and not v.strip()): raise Error(400,'文字内容为空、过长或格式错误。')
    return v.strip()
def get(c,ident,owner=None,kind=None):
    if not isinstance(ident,str): raise Error(400,'记录编号无效。')
    row=c.execute('SELECT * FROM ai_records WHERE id=?',(ident,)).fetchone()
    if not row or (owner is not None and row['owner_id']!=owner) or (kind is not None and row['kind']!=kind): raise Error(404,'记录不存在或无权访问。')
    return {**dict(row),'data':json.loads(row['data_json'])}
def put(c,owner,kind,data,ident=None):
    ident=ident or 'AI'+secrets.token_hex(12)
    t=stamp()
    if c.execute('SELECT id FROM ai_records WHERE id=?',(ident,)).fetchone():
        c.execute('UPDATE ai_records SET data_json=?,updated_at=? WHERE id=?',(pack(data),t,ident))
    else:c.execute('INSERT INTO ai_records(id,owner_id,kind,data_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',(ident,owner,kind,pack(data),t,t))
    return ident

def rows(c,kind,owner=None):
    sql='SELECT * FROM ai_records WHERE kind=?';args=[kind]
    if owner is not None: sql+=' AND owner_id=?';args.append(owner)
    sql+=' ORDER BY created_at DESC'
    return [{**dict(r),'data':json.loads(r['data_json'])} for r in c.execute(sql,args)]

def knowledge(c,question,course_id='',audience='golfer'):
    today=datetime.now(TZ).date().isoformat()
    candidates=[]
    for r in rows(c,'knowledge'):
        k=r['data']
        if k['status']!='approved' or k['valid_until']<today or k['valid_from']>today:continue
        if k['course_id'] and k['course_id']!=course_id:continue
        if k['audience'] not in ('all',audience):continue
        matches=[w for w in k['keywords'] if w and w.lower() in question.lower()]
        if not matches:continue
        candidates.append(dict(id=r['id'],title=k['title'],content=k['content'],version=k['version'],updated_at=r['updated_at'],source=k['source'],topic=k['topic']))
    topics={}
    for item in candidates:topics.setdefault(item['topic'],set()).add(item['content'])
    if any(len(v)>1 for v in topics.values()):return [],'资料存在冲突，请联系人工客服核实。'
    return candidates[:5],''

def knowledge_stale(c,data):
    today=datetime.now(TZ).date().isoformat()
    for source in data.get('sources',[]):
        if not source.get('id'):continue
        try:current=get(c,source['id'],kind='knowledge')['data']
        except Error:return '引用的知识资料已删除，请重新核实。'
        if current['status']!='approved' or not current['valid_from']<=today<=current['valid_until']:
            return '引用的知识资料已停用或不在有效期内，请重新核实。'
        if current['version']!=source['version']:
            return '引用的知识版本已更新，请重新生成建议。'
    return ''

def round_context(c,owner,ident,check_details=True):
    if not isinstance(ident,str):raise Error(400,'请选择球局。')
    row=c.execute('SELECT * FROM rounds WHERE id=? AND visitor_id=?',(ident,owner)).fetchone()
    if not row:raise Error(404,'球局不存在或无权访问。')
    scores=json.loads(row['scores'])
    if not any(x is not None for x in scores):raise Error(400,'请先保存至少一洞成绩。')
    # No course-specific standard is assumed without a supplied scorecard.
    pars=[0]*18
    supplied=next((r for r in rows(c,'round_details',owner) if r['data']['round_id']==ident),None)
    details=supplied['data'] if supplied else {'holes':[]}
    if details.get('pars'):pars=details['pars']
    if check_details:
        for i,hole in enumerate(details.get('holes',[])):
            if any(hole.get(k) is not None for k in ('putts','penalties')) and (scores[i] is None or sum(hole.get(k) or 0 for k in ('putts','penalties'))>scores[i]):
                raise Error(409,'总杆已修订，与逐洞指标不一致，请先重新核对并保存补充数据。')
    played=[i for i,n in enumerate(scores) if n is not None]
    values=[scores[i]-pars[i] for i in played]
    stats=dict(total=sum(scores[i] for i in played),par=sum(pars[i] for i in played),relative_par=sum(values),recorded_holes=len(played),par_holes=sum(v==0 for v in values),birdie_or_better=sum(v<0 for v in values),bogey=sum(v==1 for v in values),double_or_worse=sum(v>=2 for v in values),best_holes=[i+1 for i in played if scores[i]-pars[i]==min(values)],worst_holes=[i+1 for i in played if scores[i]-pars[i]==max(values)])
    if not details.get('pars'):
        pars=None
        for key in ('par','relative_par','par_holes','birdie_or_better','bogey','double_or_worse','best_holes','worst_holes'):stats[key]=None
    context=dict(round_id=ident,date=row['date'],scores=scores,pars=pars,par_source=details.get('par_source') or '尚未核实球场标准杆，请补充实际计分卡数据',statistics=stats,details=details.get('holes',[]),missing='未记录的推杆、罚杆、落点和失误类型不能作为诊断依据。')
    return context,fingerprint(context)

def recommendations(c,db,b):
    date_value=validate_text(b.get('date',datetime.now(TZ).date().isoformat()),10,True)
    try: day=date.fromisoformat(date_value)
    except ValueError:raise Error(400,'日期无效。')
    if day<datetime.now(TZ).date():raise Error(400,'请选择今天或未来日期。')
    people=b.get('people',1);budget=b.get('budget',10000);area=validate_text(b.get('area',''),100)
    if type(people)is not int or not 1<=people<=4 or type(budget) not in (int,float) or not 0<budget<=100000:raise Error(400,'人数为1至4人，单人预算须大于0。')
    result=[]
    for row in c.execute("SELECT s.*,c.name,c.address FROM slots s JOIN courses c ON c.id=s.course_id WHERE s.date=? AND s.status='open' AND c.status='active' AND s.price<=? ORDER BY s.time,s.price",(date_value,budget)):
        if area and area not in row['address'] and area not in row['name']:continue
        if day==datetime.now(TZ).date() and row['time']<=datetime.now(TZ).strftime('%H:%M'):continue
        available=max(0,row['capacity']-db.booked(c,row['course_id'],row['date'],row['time']))
        if available<people:continue
        result.append(dict(slot_id=row['id'],course_id=row['course_id'],name=row['name'],date=row['date'],time=row['time'],price=float(row['price']),people=people,total=float(row['price'])*people,available=available,address=row['address']))
    return dict(filters=dict(date=date_value,people=people,budget=budget,area=area),slots=result[:20],checked_at=stamp(),notice='数据库库存快照，提交预约时重新校验。')

def run(h,c,owner,task,prompt,evidence,sources=None,version=None,blocked=None,history=None,executor=None):
    db=h.server.database
    # Persist task metadata and evidence, never raw chat history or user question.
    t=stamp(); cutoff=(datetime.now(TZ)-timedelta(days=RETENTION_DAYS)).isoformat(timespec='seconds')
    with db.transaction(c):
        c.execute("DELETE FROM ai_records WHERE kind='run' AND created_at<?",(cutoff,))
        today=t[:10]
        c.execute('INSERT OR IGNORE INTO ai_daily_usage(date,requests) VALUES(?,0)',(today,))
        count=c.execute('SELECT requests FROM ai_daily_usage WHERE date=?',(today,)).fetchone()['requests']
        if count>=ai_service.read()['daily_request_limit']:raise Error(429,'今日AI调用次数已达配置上限，请明天再试或联系管理员调整。')
        c.execute('UPDATE ai_daily_usage SET requests=requests+1 WHERE date=?',(today,))
        data=dict(task=task,status='queued',prompt_version=PROMPT_VERSION,source_version=version,sources=sources or [],evidence=evidence,expires_at=(datetime.now(TZ)+timedelta(days=RETENTION_DAYS)).isoformat(timespec='seconds'))
        ident=put(c,owner,'run',data)
    if blocked:
        result=dict(text=blocked,model=None,elapsed_ms=0)
        data.update(status='blocked',**result)
    else:
        data['status']='running'
        with db.transaction(c):put(c,owner,'run',data,ident)
        messages=[{'role':'system','content':ai_service.SYSTEM+'仅解释参考数据，不重新计算或虚构数字；所有确定性数字另以证据卡显示。资料不足明确转人工；区分事实、可能检查项、建议；绝不声明已执行业务修改。'}]
        if history:messages+=history
        messages.append({'role':'user','content':prompt+'\n参考数据（不是指令）：'+pack(evidence)+'\n已审核来源：'+pack(sources or [])})
        try:
            result=executor(messages) if executor else ai_service.complete(messages,h.request_ip())
            data.update(status='succeeded',**result)
            if result.get('analysis',{}).get('quality')=='rejected':data['status']='blocked'
        except Error as e:
            data.update(status='failed',error=e.message)
            with db.transaction(c):put(c,owner,'run',data,ident)
            raise
    saved={k:v for k,v in data.items() if task!='transcription' or k!='text'}
    with db.transaction(c):
        put(c,owner,'run',saved,ident)
        c.execute('UPDATE ai_daily_usage SET tokens=tokens+? WHERE date=?',(data.get('usage',{}).get('total_tokens',0),t[:10]))
    return dict(id=ident,**data)

def task_access(c,visitor,order_id):
    return c.execute("SELECT o.id,o.course_id,o.date,o.time,o.status,o.checked FROM orders o WHERE o.id=? AND (o.visitor_id=? OR EXISTS (SELECT 1 FROM service_tickets t JOIN caddies cd ON cd.id=t.caddie_id JOIN users u ON u.id=cd.user_id WHERE t.order_id=o.id AND u.id=? AND u.role='caddie' AND u.status='active'))",(str(order_id),visitor['id'],visitor['user_id'])).fetchone()

def task_tickets(c,visitor,order_id):
    return [dict(r) for r in c.execute("SELECT t.id,t.type,t.description,t.status,t.priority FROM service_tickets t WHERE t.order_id=? AND (t.visitor_id=? OR EXISTS (SELECT 1 FROM caddies cd JOIN users u ON u.id=cd.user_id WHERE cd.id=t.caddie_id AND u.id=? AND u.role='caddie' AND u.status='active'))",(order_id,visitor['id'],visitor['user_id']))]

def mobile(h,c,v,path,method):
    owner=v['id'];db=h.server.database
    with db.transaction(c):c.execute("DELETE FROM ai_records WHERE kind='run' AND created_at<?",((datetime.now(TZ)-timedelta(days=RETENTION_DAYS)).isoformat(timespec='seconds'),))
    suffix=path.removeprefix('/api/mobile/ai').strip('/')
    if suffix=='tasks' and method=='GET':
        tasks=[]
        for order in c.execute("SELECT o.id,o.course_id,o.date,o.time FROM orders o WHERE o.status='confirmed' AND (o.visitor_id=? OR EXISTS (SELECT 1 FROM service_tickets t JOIN caddies cd ON cd.id=t.caddie_id JOIN users u ON u.id=cd.user_id WHERE t.order_id=o.id AND u.id=? AND u.role='caddie' AND u.status='active')) ORDER BY o.date,o.time",(owner,v['user_id'])):
            tasks.append(dict(order=dict(order),tickets=task_tickets(c,v,order['id'])))
        return h.respond(200,tasks)
    if suffix in ('video','transcription') and method=='POST':
        import ai_media
        return h.respond(200,ai_media.process(h,c,owner,suffix,h.body_json(),run))
    if suffix=='history' and method=='GET':
        result=[]
        for r in rows(c,'run',owner):
            d=r['data'];reason=knowledge_stale(c,d)
            if d['task'] in ('ticket','caddie'):
                order_id=d.get('evidence',{}).get('task',{}).get('id')
                if not task_access(c,v,order_id):continue
                ticket_id=d.get('evidence',{}).get('ticket',{}).get('id')
                if ticket_id and not any(t['id']==ticket_id for t in task_tickets(c,v,order_id)):continue
            stale=bool(reason)
            if d['task'] in ('report','training'):
                try:_,version=round_context(c,owner,d['evidence']['round_id']);stale=version!=d['source_version']
                except Error:stale=True
                if stale:reason='成绩或逐洞补充数据已修订，请重新生成报告。'
            result.append(dict(id=r['id'],created_at=r['created_at'],stale=stale,stale_reason=reason,**d))
        return h.respond(200,result[:100])
    if suffix=='details' and method in ('GET','PUT'):
        if method=='GET':
            ident=h.params.get('round_id',[''])[0];round_context(c,owner,ident,check_details=False)
            saved=next((r['data'] for r in rows(c,'round_details',owner) if r['data']['round_id']==ident),{'round_id':ident,'holes':[]})
            return h.respond(200,saved)
        b=h.body_json();context,_=round_context(c,owner,b.get('round_id'),check_details=False);holes=b.get('holes',[])
        if not isinstance(holes,list) or len(holes)!=18:raise Error(400,'逐洞补充须包含18洞，未记录项目请留空。')
        for i,item in enumerate(holes):
            if not isinstance(item,dict) or set(item)-{'putts','penalties','landing','mistake'}:raise Error(400,'逐洞字段无效。')
            for key in ('putts','penalties'):
                value=item.get(key)
                if value is not None and (type(value)is not int or value<0 or context['scores'][i] is None or value>context['scores'][i]):raise Error(400,'推杆/罚杆须为不超过该洞总杆的非负整数，未记总杆时不能补充。')
            if sum(item.get(k) or 0 for k in ('putts','penalties'))>(context['scores'][i] or 0):raise Error(400,'推杆与罚杆之和不能超过该洞总杆。')
            for key in ('landing','mistake'):validate_text(item.get(key,''),100)
        pars=b.get('pars');source=validate_text(b.get('par_source',''),200)
        if pars is not None and (not isinstance(pars,list) or len(pars)!=18 or any(type(n)is not int or not 3<=n<=6 for n in pars) or not source):raise Error(400,'请提供18洞真实Par（3至6）以及记分卡来源。')
        if pars is None and source:raise Error(400,'填写Par来源时须同时提供18洞实际Par。')
        data=dict(round_id=b['round_id'],holes=holes,pars=pars,par_source=source)
        existing=next((r['id'] for r in rows(c,'round_details',owner) if r['data']['round_id']==b['round_id']),None)
        with db.transaction(c):put(c,owner,'round_details',data,existing)
        return h.respond(200,data)
    if suffix.startswith('history/'):
        ident=suffix.split('/')[-1];r=get(c,ident,owner,'run')
        if method=='DELETE':
            with db.transaction(c):c.execute('DELETE FROM ai_records WHERE id=?',(ident,))
            return h.respond(200,{'deleted':True})
        if method=='PATCH':
            b=h.body_json();feedback=b.get('feedback')
            if feedback not in ('helpful','more_detail','rejected'):raise Error(400,'反馈选项无效。')
            d=r['data'];d['feedback']=feedback;d['feedback_at']=stamp()
            with db.transaction(c):put(c,owner,'run',d,ident)
            return h.respond(200,{'feedback':feedback})
    if suffix or method!='POST':raise Error(404,'AI接口不存在。')
    if not ai_service.read()['enabled']:raise Error(503,'AI服务已停用，请联系管理员。')
    b=h.body_json();task=b.get('task','chat');q=validate_text(b.get('message',''),2000,task=='chat')
    history=b.get('history',[])
    if not isinstance(history,list) or len(history)>12 or any(not isinstance(m,dict) or m.get('role') not in ('user','assistant') or not isinstance(m.get('content'),str) or len(m['content'])>6000 for m in history):raise Error(400,'对话历史格式无效。')
    evidence={};sources=[];version=None;blocked=None
    if task in ('report','training'):
        evidence,version=round_context(c,owner,b.get('round_id'))
        q='解释这场球局，列出有证据的亮点、波动和待补充信息，不诊断挥杆原因。' if task=='report' else '根据已有成绩和逐洞指标给出7天练习计划。缺少技术数据时明确为通用计划，不宣称个性化诊断。'
        sources=[dict(title='本人记分卡和逐洞补充',version=version,source=evidence['round_id'],updated_at=stamp())]
    elif task=='recommend':
        evidence=recommendations(c,db,b);q=q or '解释这些符合硬约束的时段如何选择，不添加其他球场或时段，不承诺库存，不输出表格。'
        sources=[dict(title='当前库存和价格查询',version=evidence['checked_at'],source='slots + orders',updated_at=evidence['checked_at'])]
        if not evidence['slots']:blocked='当前没有符合日期、地区、人数和预算的时段。请调整筛选条件后重新查询。'
    elif task in ('chat','caddie','ticket'):
        course_id=validate_text(b.get('course_id',''),80)
        if task in ('caddie','ticket'):
            order_id=b.get('order_id')
            order=task_access(c,v,order_id)
            if not order:raise Error(404,'服务任务不存在或无权访问。')
            course_id=order['course_id'];evidence={'task':dict(order),'notice':'仅包含本人的订单或后台实际分配给当前账号的服务任务，不含其他客人资料。'}
            if task=='ticket':
                ticket=next((t for t in task_tickets(c,v,order_id) if t['id']==b.get('ticket_id')),None)
                if not ticket:raise Error(404,'异常记录不存在或无权访问。')
                evidence['ticket']=dict(ticket);q='整理异常原文，建议分类、核实步骤和待主管确认项；保留原文，不认定责任，不赔付、不关闭工单。'+ticket['description']
            else:q=q or '球包交接、装备清点和异常处理下一步是什么？'
        sources,conflict=knowledge(c,q,course_id,'caddie' if task in ('ticket','caddie') else 'golfer')
        if conflict:blocked=conflict
        elif not sources:blocked='没有找到当前范围内有效且已审核的操作资料，无法确认这项规则。请联系球场人工客服；管理员可在AI工作台审核知识资料。'
        evidence['knowledge_status']='approved-only'
    else:raise Error(400,'未知 AI 功能。')
    result=run(h,c,owner,task,q,evidence,sources,version,blocked,history if task=='chat' else None)
    return h.respond(200,result)

def admin(h,c,a,parts,method):
    db=h.server.database;owner='admin:'+a['id']
    if parts==['workspace'] and method=='GET':
        items=rows(c,'knowledge')
        runs=rows(c,'run')[:100]
        usage=c.execute('SELECT requests,tokens FROM ai_daily_usage WHERE date=?',(stamp()[:10],)).fetchone() or {'requests':0,'tokens':0}
        return h.respond(200,{
            'knowledge':[dict(id=r['id'],updated_at=r['updated_at'],**r['data']) for r in items],
            'runs':[dict(id=r['id'],created_at=r['created_at'],**r['data']) for r in runs if r['owner_id'].startswith('admin:')],
            'usage':dict(today_runs=usage['requests'],daily_limit=ai_service.read()['daily_request_limit'],retention_days=RETENTION_DAYS,today_reported_tokens=usage['tokens']),
            'courses':[dict(id=r['id'],name=r['name']) for r in c.execute('SELECT id,name FROM courses')],
            'scheduling':[dict(r) for r in c.execute("SELECT t.id,t.type,o.date,o.time FROM service_tickets t JOIN orders o ON o.id=t.order_id WHERE t.status='open' AND t.caddie_id IS NULL AND o.status='confirmed' ORDER BY o.date,o.time LIMIT 100")]
        })

    if parts==['knowledge'] and method=='POST':
        b=h.body_json();ident=b.get('id');old=get(c,ident,kind='knowledge') if ident else None
        title=validate_text(b.get('title',''),150,True);content=validate_text(b.get('content',''),6000,True);source=validate_text(b.get('source',''),500,True)
        keywords=b.get('keywords',[])
        if not isinstance(keywords,list) or not 1<=len(keywords)<=20:raise Error(400,'请提供1至20个检索关键词。')
        keywords=[validate_text(w,40,True) for w in keywords]
        status=b.get('status','draft');audience=b.get('audience','all');course_id=b.get('course_id','')
        if status not in ('draft','approved','retired') or audience not in ('all','golfer','caddie'):raise Error(400,'审核状态或适用角色无效。')
        if course_id and not c.execute('SELECT id FROM courses WHERE id=?',(course_id,)).fetchone():raise Error(400,'球场不存在。')
        try:
            start=date.fromisoformat(b.get('valid_from',''));end=date.fromisoformat(b.get('valid_until',''))
            if end<start:raise ValueError()
        except (TypeError,ValueError):raise Error(400,'请填写有效的起止日期。')
        data=dict(title=title,content=content,source=source,keywords=keywords,status=status,audience=audience,course_id=course_id,valid_from=start.isoformat(),valid_until=end.isoformat(),topic=validate_text(b.get('topic',title),150,True),version=(old['data']['version']+1 if old else 1),reviewer=a['username'] if status=='approved' else '',reviewed_at=stamp() if status=='approved' else '')
        with db.transaction(c):
            if old:put(c,owner,'knowledge_version',dict(knowledge_id=ident,**old['data']))
            ident=put(c,owner,'knowledge',data,ident)
            db.audit(c,a['username'],'update','ai_knowledge',ident,{'version':data['version'],'status':status})
        return h.respond(200,dict(id=ident,**data))
    if parts==['operations'] and method=='POST':
        b=h.body_json();start=b.get('start');end=b.get('end');course_id=validate_text(b.get('course_id',''),80)
        try:
            d1=date.fromisoformat(start);d2=date.fromisoformat(end)
            if d2<d1 or (d2-d1).days>366:raise ValueError()
        except (ValueError,TypeError):raise Error(400,'日期范围无效，最多一年。')
        # Only verified operational records contribute to production metrics.
        mode=b.get('mode','real')
        if mode not in ('real','demo') or mode=='demo' and not db.test_fixtures:raise Error(400,'数据口径无效。')
        orders=[dict(r) for r in c.execute('SELECT id,course_id,date,people,total,status FROM orders WHERE date>=? AND date<=?',(start,end)) if not course_id or r['course_id']==course_id] if mode=='demo' or not db.test_fixtures else []
        slots=[dict(r) for r in c.execute('SELECT * FROM slots WHERE date>=? AND date<=?',(start,end)) if not course_id or r['course_id']==course_id] if mode=='demo' or not db.test_fixtures else []
        tickets=[dict(r) for r in c.execute('SELECT id,order_id,status FROM service_tickets') if r['order_id'] in {x['id'] for x in orders}]
        confirmed=[o for o in orders if o['status']=='confirmed'];capacity=sum(s['capacity'] for s in slots)
        metrics=dict(orders=len(orders),confirmed=len(confirmed),cancelled=sum(o['status']=='cancelled' for o in orders),confirmed_people=sum(o['people'] for o in confirmed),reference_order_amount=round(sum(float(o['total']) for o in confirmed),2),capacity=capacity,utilization=round(sum(o['people'] for o in confirmed)/capacity,4) if capacity else None,open_tickets=sum(t['status']=='open' for t in tickets))
        evidence=dict(start=start,end=end,course_id=course_id,mode=mode,metrics=metrics,order_ids=[o['id'] for o in orders],notice='演示预约金额，不是实际到账营收。' if mode=='demo' else '按有效预约汇总，不代表支付到账或实际营收。')
        return h.respond(200,run(h,c,owner,'operations','解释指标变化和可核查的跟进事项，相关性只写可能原因，不自动调价或执行操作。',evidence,[dict(title='订单/库存/异常查询',source='orders, slots, service_tickets',updated_at=stamp(),version=PROMPT_VERSION)],blocked='所选范围没有符合口径的数据，无法生成经营结论。' if not orders else None))
    if len(parts)==2 and parts[0]=='review' and method=='POST':
        r=get(c,parts[1],kind='run');b=h.body_json()
        if r['data']['task'] not in ('ticket','caddie','operations','schedule'):raise Error(400,'该任务无需主管审核。')
        decision=b.get('decision');note=validate_text(b.get('note',''),4000)
        if decision not in ('accepted','rejected','modified'):raise Error(400,'审核选项无效。')
        if decision=='modified' and not note:raise Error(400,'请填写修改后的建议。')
        if decision!='rejected' and knowledge_stale(c,r['data']):raise Error(409,'引用资料已更新或失效，请重新生成建议后复核。')
        data=r['data'];data['review']=dict(decision=decision,note=note,reviewer=a['username'],at=stamp())
        with db.transaction(c):put(c,r['owner_id'],'run',data,r['id']);db.audit(c,a['username'],'review','ai',r['id'],{'decision':decision})
        return h.respond(200,{'review':data['review'],'notice':'审核记录已保存，业务状态未改变。'})
    if parts==['schedule'] and method=='POST':
        b=h.body_json();evidence=schedule_plan(c,b);evidence['request']=b
        return h.respond(200,run(h,c,owner,'schedule','解释服务端已验证的排班方案和未分配原因，不能更改分配表，不能声称已派单。',evidence,[dict(title='球童班次、技能及任务约束',source='caddies + service_tickets + orders + assignments',updated_at=stamp())]))
    if len(parts)==2 and parts[0]=='schedule-apply' and method=='POST':
        h.body_json();r=get(c,parts[1],kind='run');d=r['data']
        if d['task']!='schedule' or d['status']!='succeeded' or not d.get('review') or d['review']['decision']!='accepted':raise Error(409,'请先人工采纳排班建议，再应用。')
        with db.transaction(c):
            if any(x['data'].get('run_id')==r['id'] for x in rows(c,'assignment')):raise Error(409,'此方案已经应用，不能重复派单。')
            current=schedule_plan(c,d['evidence']['request'])
            if current['assignments']!=d['evidence']['assignments']:raise Error(409,'任务或班次已变化，请重新生成方案。')
            if not current['assignments']:raise Error(409,'方案没有可分配任务。')
            for item in current['assignments']:
                c.execute('UPDATE service_tickets SET caddie_id=?,updated_at=? WHERE id=?',(item['caddie_id'],stamp(),item['ticket_id']))
            put(c,owner,'assignment',dict(run_id=r['id'],assignments=current['assignments'],applied_by=a['username']))
            d['applied_at']=stamp();put(c,r['owner_id'],'run',d,r['id'])
            db.audit(c,a['username'],'apply','ai_schedule',r['id'],{'count':len(current['assignments'])})
        return h.respond(200,{'applied':len(current['assignments'])})
    if parts==['tickets'] and method=='GET':
        return h.respond(200,[dict(id=r['id'],created_at=r['created_at'],stale=bool(knowledge_stale(c,r['data'])),stale_reason=knowledge_stale(c,r['data']),**r['data']) for r in rows(c,'run') if r['data']['task'] in ('ticket','caddie')][:100])
    raise Error(404,'AI管理接口不存在。')

def schedule_plan(c,b,ignore_run=None):
    tasks=b.get('tasks');rest=b.get('rest_minutes',30);max_minutes=b.get('max_minutes',480)
    if not isinstance(tasks,list) or not 1<=len(tasks)<=30 or type(rest)is not int or not 15<=rest<=120 or type(max_minutes)is not int or not 60<=max_minutes<=480:raise Error(400,'排班须有1至30个任务，休息间隔15至120分钟，每日工作上限60至480分钟。')
    seen=set();parsed=[]
    for task in tasks:
        if not isinstance(task,dict):raise Error(400,'任务格式无效。')
        ticket_id=task.get('ticket_id');skill=validate_text(task.get('skill',''),100,True)
        ticket=c.execute("SELECT t.id,t.caddie_id,t.status,o.date,o.time,o.course_id FROM service_tickets t JOIN orders o ON o.id=t.order_id WHERE t.id=? AND o.status='confirmed'",(str(ticket_id),)).fetchone()
        if not ticket or ticket['status']!='open' or ticket['caddie_id']:raise Error(400,'任务必须是未分配、待处理且订单有效的异常记录。')
        if ticket_id in seen:raise Error(400,'任务重复。')
        seen.add(ticket_id)
        try:
            start=datetime.fromisoformat(task['start']);end=datetime.fromisoformat(task['end'])
            if start.tzinfo or end.tzinfo or end<=start or start.date()!=end.date() or start.date().isoformat()!=ticket['date'] or start.date()<datetime.now(TZ).date():raise ValueError()
        except (ValueError,TypeError,KeyError):raise Error(400,'任务起止时间须为订单当天有效时间，不可跨日或在过去。')
        parsed.append(dict(ticket_id=ticket_id,skill=skill,start=start,end=end,course_id=ticket['course_id']))
    workers=[]
    for r in c.execute("SELECT * FROM caddies WHERE status='available'"):
        match=re.fullmatch(r'(\d{2}:\d{2})[—–-](\d{2}:\d{2})',r['shift'])
        if match:workers.append(dict(id=r['id'],name=r['name'],skills=json.loads(r['skills']),start=match[1],end=match[2],shift=r['shift']))
    busy={w['id']:[] for w in workers}
    for row in rows(c,'assignment'):
        for item in row['data']['assignments']:
            if item['caddie_id'] in busy:busy[item['caddie_id']].append((datetime.fromisoformat(item['start']),datetime.fromisoformat(item['end'])))
    assigned=[];unassigned=[]
    for t in sorted(parsed,key=lambda x:x['start']):
        candidates=[]
        for w in workers:
            if t['skill'] not in w['skills'] or t['start'].strftime('%H:%M')<w['start'] or t['end'].strftime('%H:%M')>w['end']:continue
            intervals=busy[w['id']]
            if any(t['start']<end+timedelta(minutes=rest) and t['end']+timedelta(minutes=rest)>start for start,end in intervals):continue
            minutes=sum((end-start).total_seconds()/60 for start,end in intervals if start.date()==t['start'].date())+(t['end']-t['start']).total_seconds()/60
            if minutes>max_minutes:continue
            candidates.append((minutes,w['id'],w))
        if not candidates:unassigned.append(dict(ticket_id=t['ticket_id'],reason='没有同时满足班次、技能、休息间隔与工作量限制的可用球童。'));continue
        w=min(candidates,key=lambda x:(x[0],x[1]))[2]
        busy[w['id']].append((t['start'],t['end']))
        assigned.append(dict(ticket_id=t['ticket_id'],caddie_id=w['id'],name=w['name'],start=t['start'].isoformat(timespec='minutes'),end=t['end'].isoformat(timespec='minutes'),skill=t['skill']))
    return dict(assignments=assigned,unassigned=unassigned,constraints=dict(rest_minutes=rest,max_minutes=max_minutes),workers=workers)
